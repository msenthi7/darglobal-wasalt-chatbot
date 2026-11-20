"""Scan raw HTML for AI-directed traps in places a human visitor never sees.

Locations checked: HTML comments, hidden elements, meta tags, text attributes
(alt/title/aria-*/data-*), embedded JSON (JSON-LD, __NEXT_DATA__) and the visible text.
Findings are reported; high-severity visible lines are also quarantined by the extractor.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup, Comment

from app.textguard import scan_text

HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])|"
    r"color\s*:\s*transparent|clip\s*:\s*rect\(\s*0|left\s*:\s*-\d{3,}px",
    re.IGNORECASE,
)
HIDDEN_CLASS = re.compile(r"\b(?:sr-only|visually-hidden|screen-reader-text|d-none|invisible)\b", re.IGNORECASE)
TEXT_ATTRS = ("alt", "title", "aria-label", "aria-description", "placeholder")
MAX_JSON_BYTES = 6 * 1024 * 1024
MAX_JSON_STRINGS = 100_000


def is_hidden(el) -> bool:
    if el.has_attr("hidden") or el.get("aria-hidden") == "true":
        return True
    style = el.get("style") or ""
    classes = " ".join(el.get("class") or [])
    return bool(HIDDEN_STYLE.search(style) or HIDDEN_CLASS.search(classes))


def json_strings(raw: str) -> list[str]:
    """All string values (and keys) inside a JSON document, iteratively and with bounds."""
    if not raw or len(raw) > MAX_JSON_BYTES:
        return []
    try:
        root = json.loads(raw)
    except ValueError:
        return []
    out: list[str] = []
    stack = [root]
    while stack and len(out) < MAX_JSON_STRINGS:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            out.extend(k for k in node if isinstance(k, str))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def scan_html(html: str, soup: BeautifulSoup | None = None) -> list[dict]:
    soup = soup or BeautifulSoup(html, "lxml")
    blobs: dict[str, list[str]] = {
        "html_comment": [], "hidden_element": [], "meta": [], "attribute": [], "embedded_json": [], "visible_text": [],
        "title": [soup.title.get_text(" ", strip=True)] if soup.title else [],
    }
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        blobs["html_comment"].append(str(comment))
    for el in soup.find_all(True):
        if el.name == "meta":
            blobs["meta"].extend(str(v) for k, v in el.attrs.items() if k in ("content", "name", "property"))
            continue
        if el.name == "script":
            if el.get("id") == "__NEXT_DATA__" or (el.get("type") or "").lower() == "application/ld+json":
                blobs["embedded_json"].extend(json_strings(el.string or ""))
            continue
        if is_hidden(el):
            blobs["hidden_element"].append(el.get_text(" ", strip=True))
        for attr, value in el.attrs.items():
            if (attr in TEXT_ATTRS or attr.startswith("data-")) and isinstance(value, str) and len(value) > 3:
                blobs["attribute"].append(value)
    body = soup.body or soup
    blobs["visible_text"].append(body.get_text("\n", strip=True))

    findings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for location, parts in blobs.items():
        text = "\n".join(p for p in parts if p)
        for f in scan_text(text):
            key = (location, f.pattern, f.excerpt)
            if key not in seen:
                seen.add(key)
                findings.append({"location": location, "pattern": f.pattern, "severity": f.severity, "excerpt": f.excerpt})
    return findings
