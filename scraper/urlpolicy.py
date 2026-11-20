"""URL policy: parsed-host allowlist (never substring matching) and page keys for archive matching."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

from . import config


def host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.username or parts.password or port not in (None, 443):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    return host or None


def allowed_site(url: str) -> str | None:
    """Return the allowlist label for an https URL whose parsed host is allowed, else None.

    "https://evil.example/?darglobal.co.uk", "https://darglobal.co.uk.evil.example/" and
    "https://darglobal.co.uk@evil.example/" are all rejected.
    """
    try:
        if urlsplit(url).scheme != "https":
            return None
    except ValueError:
        return None
    host = host_of(url)
    if not host:
        return None
    for allowed, label in config.ALLOWED_HOSTS.items():
        if host == allowed or host.endswith("." + allowed):
            return label
    return None


def page_key(url: str) -> str:
    """Normalise a page URL (drop scheme, www., query, trailing slash; lowercase) for matching."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parts.path).rstrip("/") or "/"
    return f"{host}{path}".lower()


def iri_to_uri(url: str) -> str:
    """Percent-encode non-ASCII path characters (Arabic slugs) so the URL is valid on the wire."""
    parts = urlsplit(url)
    path = quote(unquote(parts.path), safe="/-._~")
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{path}{query}"
