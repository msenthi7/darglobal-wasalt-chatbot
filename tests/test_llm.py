import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.llm import LLMError, ModelRegistry, OpenRouterClient
from app.ratelimit import SlidingWindowLimiter

FREE = {"prompt": "0", "completion": "0", "request": "0"}
LISTING = {"data": [{"id": "a/one:free", "pricing": FREE}, {"id": "b/two:free", "pricing": FREE},
                    {"id": "c/paid", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
KEY = "sk-or-test-1234567890abcdef"


def ok(text: str, model: str = "a/one:free", finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": text}, "finish_reason": finish}]})


def run(responses, *, models=("a/one:free", "b/two:free"), key=KEY, budget=None):
    seq = iter(responses)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=LISTING)
        calls.append(json.loads(request.content)["model"])
        assert request.headers["authorization"] == f"Bearer {key}"
        return next(seq)

    async def go():
        settings = Settings(openrouter_api_key=key, models=models)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            registry = ModelRegistry(models)
            await registry.refresh(http, settings.openrouter_base_url)
            client = OpenRouterClient(settings, http, registry, budget, sleep=lambda s: asyncio.sleep(0))
            return await client.complete([{"role": "user", "content": "hi"}], request_id="t")

    try:
        return asyncio.run(go()), calls
    except LLMError as exc:
        exc.calls = calls  # type: ignore[attr-defined]
        raise


def test_registry_keeps_only_listed_free_models():
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=LISTING))) as http:
            reg = ModelRegistry(("a/one:free", "c/paid", "z/missing:free"))
            await reg.refresh(http, "https://openrouter.ai/api/v1")
            return reg
    reg = asyncio.run(go())
    assert reg.available == ["a/one:free"] and reg.verified
    assert reg.rejected == {"c/paid": "not free", "z/missing:free": "not listed by OpenRouter"}


def test_rate_limited_then_success_retries_same_model():
    result, calls = run([httpx.Response(429, headers={"retry-after": "1"}), ok("hello")])
    assert result.text == "hello" and result.attempts == 2 and calls == ["a/one:free", "a/one:free"]
    assert not result.fallback_used


def test_empty_content_falls_back_to_next_model_and_discloses_it():
    result, calls = run([ok(""), ok("answer", model="b/two:free")])
    assert result.model == "b/two:free" and result.fallback_used and calls == ["a/one:free", "b/two:free"]


def test_auth_failure_stops_immediately():
    with pytest.raises(LLMError) as info:
        run([httpx.Response(401, json={"error": {"message": "bad key"}})])
    assert info.value.code == "upstream_auth" and not info.value.retryable
    assert info.value.calls == ["a/one:free"]  # type: ignore[attr-defined]


def test_exhausted_retries_raise_typed_retryable_error():
    with pytest.raises(LLMError) as info:
        run([httpx.Response(500)] * 4)
    assert info.value.code == "upstream_unavailable" and info.value.retryable
    assert len(info.value.calls) == 4  # type: ignore[attr-defined]  # 2 attempts x 2 models, never more


def test_private_reasoning_is_stripped_and_truncation_flagged():
    result, _ = run([ok("<think>chain of thought</think>Final answer [1]", finish="length")])
    assert result.text == "Final answer [1]" and result.truncated


def test_missing_key_never_calls_upstream():
    with pytest.raises(LLMError) as info:
        run([], key="")
    assert info.value.code == "not_configured"
    assert info.value.calls == []  # type: ignore[attr-defined]


def test_daily_upstream_budget_is_enforced_across_retries():
    with pytest.raises(LLMError) as info:
        run([httpx.Response(500), ok("never reached")], budget=SlidingWindowLimiter(1, 86_400))
    assert info.value.code == "daily_quota_reached"


def test_redaction_removes_configured_key_and_key_shapes():
    client = OpenRouterClient(Settings(openrouter_api_key=KEY), httpx.AsyncClient(), ModelRegistry(("a/one:free",)))
    text = client.redact(f"Authorization: Bearer {KEY} failed; other sk-or-v1-abcdefabcdef0000")
    assert KEY not in text and "sk-or-v1" not in text and "[REDACTED]" in text
