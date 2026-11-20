"""Polite, bounded HTTP client (blueprint C03/C04/C05).

* parsed-host allowlist checked before every request and every redirect hop
* robots.txt (RFC 9309 via Protego, wildcard-aware) for every host, including the archive
* time-aware per-host pacing (a lock alone would not space requests), robots Crawl-delay honoured
* bounded retries for transient failures only, Retry-After honoured, full jitter
* streamed bodies with a hard byte cap (also bounds gzip expansion)
* bot-challenge pages are reported as ``blocked_bot_challenge`` and never retried or bypassed
"""

from __future__ import annotations

import email.utils
import random
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego

from . import config
from .urlpolicy import allowed_site, host_of

TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
_CHALLENGE_TITLES = (b"<title>Just a moment...</title>", b"Attention Required! | Cloudflare")


def is_bot_challenge(body: bytes) -> bool:
    """Recognise Imperva/Incapsula and Cloudflare interstitials (not real page content)."""
    head = body[:40_000]
    if b"_Incapsula_Resource" in head and len(body) < 8_000:
        return True
    if any(title in head for title in _CHALLENGE_TITLES):
        return True
    return b"cf-browser-verification" in head or b"cf_chl_opt" in head


@dataclass
class FetchResult:
    url: str
    status: str  # ok | blocked_robots | blocked_bot_challenge | out_of_scope | http_error | too_large |
    #              bad_content_type | timeout | network_error | redirect_limit
    http_status: int | None = None
    final_url: str | None = None
    content_type: str = ""
    body: bytes = b""
    detail: str = ""
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class PoliteClient:
    def __init__(self, *, user_agent: str = config.USER_AGENT, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, clock=time.monotonic, rng=random.random) -> None:
        self._ua = user_agent
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
                "Accept-Language": "en,ar;q=0.8",
            },
            timeout=httpx.Timeout(config.READ_TIMEOUT_S, connect=config.CONNECT_TIMEOUT_S),
            follow_redirects=False,
            transport=transport,
        )
        self._sleep, self._clock, self._rng = sleep, clock, rng
        self._last: dict[str, float] = {}
        self._robots: dict[str, Protego | str] = {}
        self.robots_raw: dict[str, str] = {}
        self.log: list[dict] = []

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ robots
    def robots_allows(self, url: str) -> tuple[bool, float]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(origin)
        if rp is None:
            res = self._request(origin + "/robots.txt", check_robots=False, max_bytes=512 * 1024)
            if res.ok:
                text = res.body.decode("utf-8", "replace")
                self.robots_raw[origin] = text
                rp = Protego.parse(text)
            elif res.http_status is not None and 400 <= res.http_status < 500 and res.http_status != 429:
                rp = "ALLOW_ALL"  # RFC 9309 2.3.1.3: robots.txt unavailable -> no restrictions
            else:
                rp = "DISALLOW_ALL"  # RFC 9309 2.3.1.4: unreachable -> assume complete disallow
            self._robots[origin] = rp
        if rp == "ALLOW_ALL":
            return True, 0.0
        if rp == "DISALLOW_ALL":
            return False, 0.0
        assert isinstance(rp, Protego)
        delay = rp.crawl_delay(self._ua) or 0.0
        return bool(rp.can_fetch(url, self._ua)), float(delay)

    def sitemaps_for(self, origin: str) -> list[str]:
        rp = self._robots.get(origin)
        return list(rp.sitemaps) if isinstance(rp, Protego) else []

    # ----------------------------------------------------------------- fetching
    def get(self, url: str, *, max_bytes: int = config.MAX_HTML_BYTES,
            accept_types: tuple[str, ...] | None = None) -> FetchResult:
        return self._request(url, check_robots=True, max_bytes=max_bytes, accept_types=accept_types)

    def _request(self, url: str, *, check_robots: bool, max_bytes: int,
                 accept_types: tuple[str, ...] | None = None) -> FetchResult:
        current = url
        for _hop in range(config.MAX_REDIRECTS + 1):
            if not allowed_site(current):
                return self._record(FetchResult(url, "out_of_scope", final_url=current,
                                                detail="host not in allowlist or not https"))
            crawl_delay = 0.0
            if check_robots:
                allowed, crawl_delay = self.robots_allows(current)
                if not allowed:
                    return self._record(FetchResult(url, "blocked_robots", final_url=current))
            res = self._fetch_with_retries(current, max_bytes, crawl_delay)
            if res.status == "redirect":
                current = urljoin(current, res.detail)
                continue
            res.url, res.final_url = url, current
            if res.ok:
                if is_bot_challenge(res.body):
                    res.status = "blocked_bot_challenge"
                elif accept_types and not any(t in res.content_type for t in accept_types):
                    res.status, res.body = "bad_content_type", b""
            return self._record(res)
        return self._record(FetchResult(url, "redirect_limit", final_url=current))

    def _fetch_with_retries(self, url: str, max_bytes: int, crawl_delay: float) -> FetchResult:
        host = host_of(url) or ""
        attempt = 0
        while True:
            attempt += 1
            self._pace(host, crawl_delay)
            retry_after: str | None = None
            status: int | None = None
            try:
                with self._client.stream("GET", url) as resp:
                    status = resp.status_code
                    ctype = resp.headers.get("content-type", "").lower()
                    retry_after = resp.headers.get("retry-after")
                    if 300 <= status < 400 and resp.headers.get("location"):
                        return FetchResult(url, "redirect", status, detail=resp.headers["location"], attempts=attempt)
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        return FetchResult(url, "too_large", status, content_type=ctype, attempts=attempt,
                                           detail=f"declared {declared} bytes")
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            return FetchResult(url, "too_large", status, content_type=ctype, attempts=attempt)
                    body = bytes(buf)
            except httpx.TimeoutException as exc:
                outcome = FetchResult(url, "timeout", attempts=attempt, detail=type(exc).__name__)
            except httpx.TransportError as exc:
                outcome = FetchResult(url, "network_error", attempts=attempt, detail=type(exc).__name__)
            else:
                if 200 <= status < 300:
                    return FetchResult(url, "ok", status, content_type=ctype, body=body, attempts=attempt)
                outcome = FetchResult(url, "http_error", status, content_type=ctype, body=body[:65_536], attempts=attempt)
                if is_bot_challenge(body):
                    outcome.status = "blocked_bot_challenge"  # never retried, never bypassed
                    return outcome
            transient = status is None or status in TRANSIENT_HTTP
            if not transient or attempt >= config.MAX_ATTEMPTS:
                return outcome
            self._sleep(self._backoff(attempt, retry_after))

    def _pace(self, host: str, crawl_delay: float) -> None:
        interval = max(config.MIN_INTERVAL_S.get(host, config.DEFAULT_MIN_INTERVAL_S), crawl_delay)
        last = self._last.get(host)
        if last is not None:
            wait = interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            seconds: float | None = None
            if retry_after.strip().isdigit():
                seconds = float(retry_after.strip())
            else:
                try:
                    when = email.utils.parsedate_to_datetime(retry_after)
                    seconds = max(0.0, when.timestamp() - time.time())
                except (TypeError, ValueError):
                    seconds = None
            if seconds is not None:
                return min(config.BACKOFF_CAP_S, seconds)
        return self._rng() * min(config.BACKOFF_CAP_S, config.BACKOFF_BASE_S * 2 ** (attempt - 1))

    def _record(self, res: FetchResult) -> FetchResult:
        self.log.append({"url": res.url, "status": res.status, "http_status": res.http_status,
                         "attempts": res.attempts})
        return res
