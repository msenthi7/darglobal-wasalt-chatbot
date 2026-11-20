"""Sitemap parsing with defusedxml (no DTD entities / external fetches) and bounded gzip expansion."""

from __future__ import annotations

import gzip
import io
import zlib
from dataclasses import dataclass

import defusedxml.ElementTree as DET
from defusedxml import DefusedXmlException

from . import config


class SitemapError(ValueError):
    pass


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str | None


def maybe_gunzip(body: bytes, limit: int = config.MAX_SITEMAP_BYTES) -> bytes:
    """Decompress gzip payloads (some '.xml.gz' files are actually plain XML) without exceeding limit."""
    if body[:2] != b"\x1f\x8b":
        return body
    out = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            while chunk := gz.read(1024 * 1024):
                out.extend(chunk)
                if len(out) > limit:
                    raise SitemapError("decompressed sitemap exceeds size limit")
    except (OSError, EOFError, zlib.error) as exc:
        raise SitemapError(f"corrupt or truncated gzip sitemap: {type(exc).__name__}") from exc
    return bytes(out)


def parse_sitemap(body: bytes) -> tuple[str, list[SitemapEntry]]:
    """Return ('urlset'|'sitemapindex', entries). Namespace-agnostic, streaming."""
    data = maybe_gunzip(body)
    kind = ""
    entries: list[SitemapEntry] = []
    try:
        for event, el in DET.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
            if event == "start":
                if not kind and tag in ("urlset", "sitemapindex"):
                    kind = tag
                continue
            if tag in ("url", "sitemap"):
                loc = lastmod = None
                for child in el:
                    ctag = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
                    if ctag == "loc" and child.text:
                        loc = child.text.strip()
                    elif ctag == "lastmod" and child.text:
                        lastmod = child.text.strip()[:32]
                if loc:
                    entries.append(SitemapEntry(loc, lastmod))
                el.clear()
    except DET.ParseError as exc:  # type: ignore[attr-defined]
        raise SitemapError(f"invalid sitemap XML: {exc}") from exc
    except DefusedXmlException as exc:  # entity expansion / external references: refused, not parsed
        raise SitemapError(f"forbidden XML construct: {type(exc).__name__}") from exc
    if not kind:
        raise SitemapError("not a sitemap (no urlset/sitemapindex root)")
    return kind, entries
