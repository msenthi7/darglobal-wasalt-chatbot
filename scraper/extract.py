"""HTML -> clean text + structured facts. Embedded JSON is parsed as data, never executed."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.textguard import line_is_suspicious, strip_invisible

from . import config
from .traps import MAX_JSON_BYTES, is_hidden

_WS = re.compile(r"[ \t\r\f\v ]+")
_SURROGATE = re.compile("[\ud800-\udfff]")  # lone surrogates cannot be encoded as UTF-8
_URLISH = re.compile(r"^(?:https?://|/[\w-]+/|www\.)|\.(?:jpe?g|png|webp|svg|gif|mp4|pdf)$", re.IGNORECASE)
_HAS_LETTER = re.compile(r"[A-Za-z؀-ۿ]")

REMOVE_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "form", "header", "nav", "footer",
               "button", "select", "option", "video", "audio", "canvas", "picture")
TEXT_KEYS = {"title", "heading", "description", "richdescription", "name", "value", "label", "subheading",
             "subtitle", "content", "text", "question", "answer", "location", "distance", "time", "body",
             "paragraph", "caption_text", "startingprice", "status", "date", "summary"}
SKIP_KEYS = {"image", "images", "herobanner", "photogallery", "formats", "url", "hash", "mime", "provider", "ext",
             "size", "width", "height", "id", "createdat", "updatedat", "publishedat", "locale", "vuid", "href",
             "link", "icon", "video", "view360", "livefeed", "seo", "localizations", "versions", "thumbnail",
             "media", "logo", "alternativetext", "provider_metadata", "previewurl", "lat", "long", "slug",
             "brochure", "downloadbrochure", "bookunit", "event", "target"}


@dataclass
class Extracted:
    title: str = ""
    description: str = ""
    lang: str = "en"
    visible_lines: list[str] = field(default_factory=list)
    fact_lines: list[str] = field(default_factory=list)
    next_data: dict | None = None
    quarantined: list[str] = field(default_factory=list)


def clean_line(text: str) -> str:
    text = strip_invisible(_SURROGATE.sub("", str(text)))
    text = re.sub(r"<[^>]{1,200}>", " ", text)  # stray markup inside CMS rich text
    return _WS.sub(" ", text).strip()[: config.MAX_LINE_CHARS]


def parse_next_data(soup: BeautifulSoup) -> dict | None:
    tag = soup.find("script", id="__NEXT_DATA__")
    raw = tag.string if tag else None
    if not raw or len(raw) > MAX_JSON_BYTES:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def collect_texts(node, limit: int = 200) -> list[str]:
    """Human-readable strings under text-like keys, skipping media/ids/URLs. Order-preserving, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    stack = [node]
    while stack and len(out) < limit:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                k = str(key).lower()
                if k in SKIP_KEYS:
                    continue
                if isinstance(value, str):
                    if k in TEXT_KEYS:
                        line = clean_line(value)
                        if len(line) >= 2 and _HAS_LETTER.search(line) and not _URLISH.search(line) and line not in seen:
                            seen.add(line)
                            out.append(line)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _page_props(next_data: dict | None) -> dict:
    if not next_data:
        return {}
    props = next_data.get("props")
    pp = props.get("pageProps") if isinstance(props, dict) else None
    return pp if isinstance(pp, dict) else {}


def darglobal_facts(next_data: dict | None) -> list[str]:
    """Structured project facts from DarGlobal's Strapi-backed __NEXT_DATA__ (project pages only)."""
    pdd = (_page_props(next_data).get("projectDetailsData") or {})
    attrs = pdd.get("attributes") if isinstance(pdd, dict) else None
    if not isinstance(attrs, dict):
        return []
    details = attrs.get("ProjectDetails") if isinstance(attrs.get("ProjectDetails"), dict) else {}
    lines: list[str] = []
    banner = details.get("ProjectBanner") if isinstance(details.get("ProjectBanner"), dict) else {}
    name = banner.get("Heading") or attrs.get("title")
    if name:
        lines.append(f"Project: {clean_line(name)}")
    for key, label in (("Location", "Location"), ("subheading", "Tagline"), ("StartingPrice", "Starting price")):
        if banner.get(key):
            lines.append(f"{label}: {clean_line(banner[key])}")
    about = details.get("AboutProject") if isinstance(details.get("AboutProject"), dict) else {}
    for item in about.get("AboutDetailsType") or []:
        if isinstance(item, dict) and item.get("name") and item.get("value"):
            lines.append(f"{clean_line(item['name'])}: {clean_line(item['value'])}")
    if about.get("description"):
        lines.append(f"About: {clean_line(about['description'])}")
    loc = details.get("loaction") or details.get("location") or {}
    if isinstance(loc, dict):
        if loc.get("description"):
            lines.append(f"Location details: {clean_line(loc['description'])}")
        nearby = collect_texts(loc.get("nearByLandmark"), limit=30)
        if nearby:
            lines.append("Nearby: " + "; ".join(nearby))
    amenities = details.get("Amenities") if isinstance(details.get("Amenities"), dict) else {}
    names = [clean_line(a["name"]) for a in amenities.get("amenitiesList") or [] if isinstance(a, dict) and a.get("name")]
    if names:
        lines.append("Amenities: " + ", ".join(names))
    for key, label in (("KeyFeatures", "Key features"), ("whyInvest", "Why invest"), ("PaymentMode", "Payment plan"),
                       ("ProgressUpdate", "Construction progress")):
        texts = collect_texts(details.get(key), limit=25)
        if texts:
            lines.append(f"{label}: " + " | ".join(texts))
    for faq in attrs.get("faqContent") or []:
        texts = collect_texts(faq, limit=10)
        if texts:
            lines.append("FAQ: " + " | ".join(texts))
    return [l for l in lines if l]


def darglobal_catalog(next_data: dict | None) -> dict | None:
    """Project list + navigation menu (country -> projects) from DarGlobal's global page data."""
    gd = _page_props(next_data).get("globalData")
    attrs = gd.get("attributes") if isinstance(gd, dict) else None
    if not isinstance(attrs, dict):
        return None
    projects = [
        {"name": clean_line(p.get("name", "")), "country": clean_line(p.get("country", "")),
         "code": clean_line(p.get("projectCode", ""))}
        for p in attrs.get("ProjectList") or [] if isinstance(p, dict) and p.get("name")
    ]
    menu: list[dict] = []
    for top in attrs.get("DesktopMenu") or []:
        if not isinstance(top, dict):
            continue
        for inner in top.get("innerMenu") or []:
            if not isinstance(inner, dict):
                continue
            for item in inner.get("InnerSubMenu") or []:
                if isinstance(item, dict) and item.get("name"):
                    menu.append({"section": clean_line(top.get("label", "")), "group": clean_line(inner.get("label", "")),
                                 "name": clean_line(item["name"]), "location": clean_line(item.get("location") or ""),
                                 "href": str(item.get("href") or "")[:300]})
    if not projects and not menu:
        return None
    return {"projects": projects, "menu": menu, "updated_at": str(attrs.get("updatedAt") or "")}


def extract(html: str, soup: BeautifulSoup | None = None, *, site: str) -> Extracted:
    soup = soup or BeautifulSoup(html, "lxml")
    out = Extracted()
    if soup.title and soup.title.string:
        out.title = clean_line(soup.title.string)
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        out.description = clean_line(meta["content"])
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") if html_tag else "") or ""
    out.next_data = parse_next_data(soup)
    if out.next_data and isinstance(out.next_data.get("locale"), str):
        lang = out.next_data["locale"]
    out.lang = "ar" if str(lang).lower().startswith("ar") else "en"
    for attr in ("title", "description"):  # these reach the prompt too, so they get the same quarantine
        value = getattr(out, attr)
        if value and line_is_suspicious(value):
            out.quarantined.append(value[:300])
            setattr(out, attr, "")
    if site == "darglobal":
        out.fact_lines = darglobal_facts(out.next_data)

    for tag in soup.find_all(list(REMOVE_TAGS)):
        tag.decompose()
    for el in soup.find_all(True):
        if not el.decomposed and is_hidden(el):  # hidden text is scanned for traps but never indexed
            el.decompose()
    body = soup.body or soup
    seen: set[str] = set()
    for raw in body.get_text("\n").splitlines():
        line = clean_line(raw)
        if len(line) < 3 or line in seen or not _HAS_LETTER.search(line):
            continue  # repeated blocks (hero banners, sliders) are kept once
        seen.add(line)
        out.visible_lines.append(line)

    for bucket in (out.visible_lines, out.fact_lines):
        kept = []
        for line in bucket:
            if line_is_suspicious(line):
                out.quarantined.append(line[:300])
            else:
                kept.append(line)
        bucket[:] = kept
    return out
