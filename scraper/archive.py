"""Internet Archive (Wayback Machine) lookups: CDX index queries and capture selection."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from . import config
from .http import PoliteClient
from .urlpolicy import iri_to_uri, page_key

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capture:
    timestamp: str  # YYYYMMDDhhmmss (UTC)
    original: str
    length: int

    @property
    def has_query(self) -> bool:
        return bool(urlsplit(self.original).query)

    @property
    def raw_url(self) -> str:
        # "id_" returns the archived bytes without the Wayback toolbar/rewriting.
        return f"https://web.archive.org/web/{self.timestamp}id_/{iri_to_uri(self.original)}"

    @property
    def iso_date(self) -> str:
        t = self.timestamp
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]}"


def _cdx_url(params: list[tuple[str, str]]) -> str:
    return f"{CDX_ENDPOINT}?{urlencode(params)}"


def cdx_captures(client: PoliteClient, url_pattern: str, *, max_pages: int = 10) -> list[Capture]:
    """All 200/text-html captures since ARCHIVE_FROM for a URL or prefix pattern (paged with resume keys)."""
    captures: list[Capture] = []
    resume: str | None = None
    for _ in range(max_pages):
        params = [("url", url_pattern), ("output", "json"), ("fl", "timestamp,original,statuscode,mimetype,length"),
                  ("filter", "statuscode:200"), ("filter", "mimetype:text/html"), ("from", config.ARCHIVE_FROM),
                  ("limit", "10000"), ("showResumeKey", "true")]
        if resume:
            params.append(("resumeKey", resume))
        res = client.get(_cdx_url(params), max_bytes=config.MAX_CDX_BYTES)
        if not res.ok:
            raise ArchiveError(f"CDX query failed: {res.status} {res.http_status}")
        text = res.body.decode("utf-8", "replace").strip()
        rows = json.loads(text) if text else []
        resume = None
        if rows and rows[-1] and len(rows[-1]) == 1 and len(rows) >= 2 and rows[-2] == []:
            resume = rows[-1][0]
            rows = rows[:-2]
        for row in rows[1:]:  # first row is the header
            if len(row) >= 5 and row[0].isdigit():
                length = int(row[4]) if str(row[4]).isdigit() else 0
                captures.append(Capture(row[0], row[1], length))
        if not resume:
            break
    return captures


def index_by_page(captures: list[Capture]) -> dict[str, list[Capture]]:
    """Group captures by normalised page key; best candidates first (no query string, newest, real size)."""
    grouped: dict[str, list[Capture]] = defaultdict(list)
    for cap in captures:
        grouped[page_key(cap.original)].append(cap)
    for caps in grouped.values():
        caps.sort(key=lambda c: (c.has_query, c.length < config.MIN_CAPTURE_LENGTH, -int(c.timestamp)))
    return dict(grouped)
