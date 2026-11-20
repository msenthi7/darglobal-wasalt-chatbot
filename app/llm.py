"""OpenRouter adapter (blueprint A01/C04/C05/C08).

* Only models verified as $0 against OpenRouter's public /models pricing are ever called.
* One retry policy lives here: SDK retries are not used (plain httpx), so attempts cannot multiply.
* Transient failures (timeouts, 408/429/5xx) retry with backoff + jitter inside a total deadline,
  then fall back to the next free model; the model that answered is always reported.
* Errors become typed ``LLMError``s with safe messages; upstream text is redacted before logging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx

from .config import Settings
from .ratelimit import SlidingWindowLimiter

log = logging.getLogger("app.llm")
_THINK = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)
_KEYISH = re.compile(r"sk-or-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}")


class LLMError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool, http_status: int = 502,
                 retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.http_status, self.retry_after_s = http_status, retry_after_s


@dataclass
class LLMResult:
    text: str
    model: str
    attempts: int
    fallback_used: bool
    truncated: bool


@dataclass
class ModelRegistry:
    configured: tuple[str, ...]
    available: list[str] = field(default_factory=list)
    verified: bool = False
    checked_at: float | None = None
    rejected: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _is_free(pricing: object) -> bool:
        if not isinstance(pricing, dict):
            return False
        for key in ("prompt", "completion", "request"):
            try:
                if Decimal(str(pricing.get(key, "0"))) != 0:
                    return False
            except InvalidOperation:
                return False
        return True

    async def refresh(self, http: httpx.AsyncClient, base_url: str) -> None:
        try:
            resp = await http.get(f"{base_url}/models", timeout=15.0)
            resp.raise_for_status()
            listing = {m.get("id"): m for m in resp.json().get("data", []) if isinstance(m, dict)}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.warning("model list refresh failed: %s", type(exc).__name__)
            if not self.verified:  # disclosed via /api/meta: unverified fallback, :free ids only
                self.available = [m for m in self.configured if m.endswith(":free") or m == "openrouter/free"]
            return
        available, rejected = [], {}
        for model in self.configured:
            info = listing.get(model)
            if info is None:
                rejected[model] = "not listed by OpenRouter"
            elif not self._is_free(info.get("pricing")):
                rejected[model] = "not free"
            else:
                available.append(model)
        self.available, self.rejected = available, rejected
        self.verified, self.checked_at = True, time.time()
        log.info("free models available: %s; rejected: %s", available, rejected)


class _Retryable(Exception):
    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


class _NextModel(Exception):
    pass


class _AuthFailure(Exception):
    """The key itself was rejected: every model would fail the same way, so stop at once."""


class OpenRouterClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, registry: ModelRegistry,
                 upstream_budget: SlidingWindowLimiter | None = None, *, clock=time.monotonic,
                 sleep=asyncio.sleep, rng=random.random) -> None:
        self.s, self.http, self.registry, self.budget = settings, http, registry, upstream_budget
        self._clock, self._sleep, self._rng = clock, sleep, rng
        self._sem = asyncio.Semaphore(settings.max_concurrent_upstream)

    def redact(self, text: str) -> str:
        if self.s.openrouter_api_key:
            text = text.replace(self.s.openrouter_api_key, "[REDACTED]")
        return _KEYISH.sub("[REDACTED]", text)

    async def complete(self, messages: list[dict], *, request_id: str) -> LLMResult:
        if not self.s.llm_configured:
            raise LLMError("not_configured", "The assistant's model access is not configured yet.",
                           retryable=False, http_status=503)
        models = list(self.registry.available)
        if not models:
            raise LLMError("no_model_available", "No free model is currently available. Please try again later.",
                           retryable=True, http_status=503)
        deadline = self._clock() + self.s.total_deadline_s
        attempts = 0
        last_reason = "unknown"
        for index, model in enumerate(models):
            for attempt in range(1, self.s.max_attempts_per_model + 1):
                remaining = deadline - self._clock()
                if remaining < 4:
                    raise LLMError("upstream_timeout", "The model took too long to respond. Please try again.",
                                   retryable=True, http_status=504)
                if self.budget is not None and self.budget.check("upstream") is not None:
                    raise LLMError("daily_quota_reached", "Today's free model quota for this demo is used up. "
                                   "Please try again tomorrow.", retryable=True, http_status=503)
                attempts += 1
                started = self._clock()
                try:
                    text, truncated, used = await self._call(model, messages, timeout=min(self.s.request_timeout_s, remaining - 1))
                except _Retryable as exc:
                    last_reason = exc.reason
                    log.info(json.dumps({"event": "llm_retryable", "request_id": request_id, "model": model,
                                         "attempt": attempt, "reason": exc.reason}))
                    if attempt < self.s.max_attempts_per_model:
                        wait = exc.retry_after if exc.retry_after is not None else self._rng() * min(8.0, 1.5 * 2 ** (attempt - 1))
                        if wait < deadline - self._clock() - 5:
                            await self._sleep(wait)
                            continue
                    break  # next model
                except _AuthFailure as exc:
                    raise LLMError("upstream_auth", "The assistant's model access is misconfigured.",
                                   retryable=False, http_status=503) from exc
                except _NextModel as exc:
                    last_reason = str(exc)
                    log.info(json.dumps({"event": "llm_next_model", "request_id": request_id, "model": model,
                                         "reason": str(exc)}))
                    break
                log.info(json.dumps({"event": "llm_ok", "request_id": request_id, "model": used, "attempts": attempts,
                                     "latency_ms": int((self._clock() - started) * 1000), "truncated": truncated}))
                return LLMResult(text=text, model=used, attempts=attempts, fallback_used=index > 0, truncated=truncated)
        log.warning(json.dumps({"event": "llm_exhausted", "request_id": request_id, "reason": last_reason}))
        raise LLMError("upstream_unavailable", "The free models are busy or unavailable right now. Please try again "
                       "in a minute.", retryable=True, http_status=502)

    async def _call(self, model: str, messages: list[dict], *, timeout: float) -> tuple[str, bool, str]:
        headers = {"Authorization": f"Bearer {self.s.openrouter_api_key}", "Content-Type": "application/json",
                   "X-Title": "DarGlobal & Wasalt public-data assistant (demo)"}
        if self.s.public_url:
            headers["HTTP-Referer"] = self.s.public_url
        body = {"model": model, "messages": messages, "max_tokens": self.s.max_output_tokens,
                "temperature": self.s.temperature, "reasoning": {"effort": "low", "exclude": True}}
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise _Retryable("local_concurrency_wait") from exc
        try:
            resp = await self.http.post(f"{self.s.openrouter_base_url}/chat/completions", json=body, headers=headers,
                                        timeout=httpx.Timeout(timeout, connect=self.s.connect_timeout_s))
        except httpx.TimeoutException as exc:
            raise _Retryable("timeout") from exc
        except httpx.TransportError as exc:
            raise _Retryable(f"transport:{type(exc).__name__}") from exc
        finally:
            self._sem.release()
        return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> tuple[str, bool, str]:
        status = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = None
        err = data.get("error") if isinstance(data, dict) else None
        if status in (401, 403):
            log.error("upstream auth failure: %s", self.redact(str(err)[:300]))
            raise _AuthFailure()
        if status in (408, 429) or status >= 500 or (status == 200 and isinstance(err, dict)
                                                      and str(err.get("code")) in ("429", "502", "503")):
            ra = resp.headers.get("retry-after", "")
            raise _Retryable(f"http_{status}", float(ra) if ra.isdigit() and int(ra) <= 30 else None)
        if status != 200 or not isinstance(data, dict) or err:
            msg = self.redact(json.dumps(err)[:300]) if err else f"http_{status}"
            if "data policy" in msg.lower():
                log.error("OpenRouter account privacy settings block free endpoints: %s", msg)
            raise _NextModel(f"rejected:{status}:{msg[:120]}")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _NextModel("malformed:no_choices")
        message = choices[0].get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = _THINK.sub("", content or "").strip()  # never show private reasoning (A01)
        if not text:
            raise _NextModel("malformed:empty_content")
        used = str(data.get("model") or "")[:100] or "unknown"
        return text[:6000], choices[0].get("finish_reason") == "length", used
