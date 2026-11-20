"""Turn Wasalt's public sitemaps into dated, derived documents (coverage + inventory snapshots).

Counts are computed from sitemap URLs only; they are labelled as such because a sitemap
entry is not a verified, still-available listing.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import unquote, urlsplit

from .sitemaps import SitemapEntry

CATEGORY_RX = re.compile(r"^/en/(?P<type>[a-z0-9-]+?)-for-(?P<purpose>sale|rent|daily-rental|monthly-rental|yearly-rental|[a-z-]*?rental)-in-(?P<rest>[a-z0-9-]+)$")
PDP_RX = re.compile(r"^/en/property/(?P<purpose>sale|rent|[a-z-]+)/(?P<slug>[a-z0-9,-]+?)-(?P<id>\d+)$")
DAILY_RX = re.compile(r"^/en/dailyrental/(?P<slug>[a-z0-9,-]+?)-(?P<id>\d+)$")  # e.g. chalet-for-booking-5582
PURPOSE_LABEL = {"sale": "for sale", "rent": "for rent", "daily-rental": "for daily rental (booking)"}
TYPE_STOP = re.compile(r"-(?:with|for|in|on|facing|\d)")


def titleize(slug: str) -> str:
    words = [w for w in slug.split("-") if w]
    articles = ("al", "as", "ar", "an", "ad", "at", "az", "ash", "adh")
    return " ".join(w.capitalize() if i == 0 or w not in articles else w for i, w in enumerate(words))


def parse_category(url: str) -> dict | None:
    m = CATEGORY_RX.match(unquote(urlsplit(url).path).rstrip("/"))
    return m.groupdict() if m else None


def parse_listing(url: str) -> dict | None:
    path = unquote(urlsplit(url).path).rstrip("/")
    m = PDP_RX.match(path)
    if m:
        purpose, slug, listing_id = m["purpose"], m["slug"], m["id"]
    else:
        d = DAILY_RX.match(path)
        if not d:
            return None
        purpose, slug, listing_id = "daily-rental", d["slug"], d["id"]
    slug = slug.replace(",", "")
    stop = TYPE_STOP.search(slug)
    ptype = slug[: stop.start()] if stop else slug
    beds = re.search(r"(\d+)-bedrooms?", slug)
    sqm = re.search(r"(\d+)-sqm", slug)
    return {"purpose": purpose, "type": ptype.replace("-", " ") or "unspecified", "id": listing_id,
            "bedrooms": int(beds.group(1)) if beds else None, "sqm": int(sqm.group(1)) if sqm else None}


def split_city_district(rests: set[str]) -> dict[str, tuple[str, str]]:
    """A rest that prefixes others ('riyadh' vs 'riyadh-al-malqa') is a city; the remainder is a district.

    Only minimal prefixes count: 'jeddah-um-hableen' prefixes 'jeddah-um-hableen-2' but is itself
    under 'jeddah', so it stays a district.
    """
    prefixes = {r for r in rests if any(o.startswith(r + "-") for o in rests)}
    cities = {r for r in prefixes if not any(r.startswith(c + "-") for c in prefixes if c != r)}
    out: dict[str, tuple[str, str]] = {}
    for rest in rests:
        city = max((c for c in cities if rest == c or rest.startswith(c + "-")), key=len, default=rest)
        out[rest] = (city, rest[len(city) + 1:] if rest != city else "")
    return out


def _bucket(sqm: int) -> str:
    for upper, label in ((150, "under 150 sqm"), (300, "150-299 sqm"), (500, "300-499 sqm"), (1000, "500-999 sqm")):
        if sqm < upper:
            return label
    return "1,000 sqm or more"


def coverage_docs(category_entries: list[SitemapEntry], rental_entries: list[SitemapEntry], *, fetched: str,
                  sources: dict[str, str]) -> list[dict]:
    """One coverage document per city (sale + rental category pages), plus a rental-type overview."""
    parsed: list[tuple[str, dict]] = []
    for kind, entries in (("sale", category_entries), ("rental", rental_entries)):
        for e in entries:
            p = parse_category(e.loc)
            if p:
                parsed.append((kind, p))
    mapping = split_city_district({p["rest"] for _, p in parsed})
    per_city: dict[str, dict[str, set]] = defaultdict(lambda: {"sale": set(), "rental": set(), "types": set()})
    rental_types: Counter[str] = Counter()
    for kind, p in parsed:
        city, district = mapping[p["rest"]]
        bucket = per_city[city]
        if district:
            bucket["sale" if kind == "sale" else "rental"].add(district)
        bucket["types"].add(f"{p['type'].replace('-', ' ')} for {p['purpose'].replace('-', ' ')}")
        if kind == "rental":
            rental_types[f"{p['type'].replace('-', ' ')} for {p['purpose'].replace('-', ' ')}"] += 1
    docs = []
    ranked = sorted(per_city.items(), key=lambda kv: (-(len(kv[1]["sale"]) + len(kv[1]["rental"])), kv[0]))
    for city, data in ranked[:60]:
        name = titleize(city)
        sale = sorted(titleize(d) for d in data["sale"])
        rent = sorted(titleize(d) for d in data["rental"])
        text = [f"Wasalt coverage for {name}, Saudi Arabia (derived from Wasalt's public sitemaps, fetched {fetched}).",
                f"Districts with properties-for-sale search pages ({len(sale)}): " + (", ".join(sale) or "none listed") + ".",
                f"Districts with rental search pages ({len(rent)}): " + (", ".join(rent) or "none listed") + ".",
                "Search page types available: " + ", ".join(sorted(data["types"])) + ".",
                "Note: a search page existing in the sitemap does not guarantee current listings in that district."]
        docs.append({"key": f"coverage-{city}", "title": f"Wasalt coverage in {name}: districts with property listings for sale and rent",
                     "url": f"https://wasalt.sa/en/properties-for-sale-in-{city}", "text": "\n".join(text),
                     "retrieved_from": sources["category"]})
    top = ", ".join(f"{t} ({n} search pages)" for t, n in rental_types.most_common(25))
    cities = ", ".join(titleize(c) for c, _ in ranked[:40])
    docs.append({"key": "coverage-overview", "title": "Wasalt coverage overview: cities and rental types",
                 "url": "https://wasalt.sa/en/",
                 "text": (f"Wasalt is a Saudi real-estate platform for sale and rental listings. Its public sitemaps "
                          f"(fetched {fetched}) list search pages for {len(per_city)} cities/areas. Cities with the most "
                          f"district search pages: {cities}.\nRental search page types: {top}."),
                 "retrieved_from": sources["rental"]})
    return docs


def inventory_doc(listing_entries: dict[str, list[SitemapEntry]], *, fetched: str, sources: list[str]) -> dict:
    """Aggregate listing counts by purpose/type/bedrooms/size from listing-page URLs."""
    by_purpose: Counter[str] = Counter()
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    beds: dict[str, Counter[int]] = defaultdict(Counter)
    sizes: dict[str, Counter[str]] = defaultdict(Counter)
    lastmods: list[str] = []
    total = seen = 0
    unparsed: list[str] = []
    capped = [n for n, entries in listing_entries.items() if len(entries) >= 10_000 and len(entries) % 10_000 == 0]
    for _name, entries in sorted(listing_entries.items()):
        for e in entries:
            seen += 1
            p = parse_listing(e.loc)
            if not p:
                unparsed.append(urlsplit(e.loc).path)
                continue
            total += 1
            purpose = PURPOSE_LABEL.get(p["purpose"], f"for {p['purpose']}")
            by_purpose[purpose] += 1
            by_type[purpose][p["type"]] += 1
            if p["bedrooms"] is not None:
                beds[purpose][p["bedrooms"]] += 1
            if p["sqm"] is not None:
                sizes[purpose][_bucket(p["sqm"])] += 1
            if e.lastmod:
                lastmods.append(e.lastmod[:10])
    lines = [f"Wasalt listing inventory snapshot, derived from {total:,} of {seen:,} listing URLs in Wasalt's public "
             f"sitemaps (fetched {fetched}; listing pages last modified between {min(lastmods, default='?')} and "
             f"{max(lastmods, default='?')})."]
    if unparsed:
        lines.append(f"{len(unparsed):,} sitemap entries did not match a listing URL pattern and are excluded "
                     f"(e.g. {', '.join(sorted(set(unparsed))[:2])}).")
    if capped:
        lines.append("The main listing sitemap holds a round number of entries (" + ", ".join(
            f"{len(listing_entries[n]):,} in {n}" for n in sorted(capped)) + "), which may be a cap rather than "
            "Wasalt's full inventory.")
    for purpose in sorted(by_purpose):
        n = by_purpose[purpose]
        types = ", ".join(f"{t} {c:,}" for t, c in by_type[purpose].most_common(12))
        lines.append(f"Listings {purpose}: {n:,}. By property type: {types}.")
        if beds[purpose]:
            lines.append(f"Bedrooms where stated in the listing URL ({purpose}): " +
                         ", ".join(f"{b} bedrooms {c:,}" for b, c in sorted(beds[purpose].items()) if c >= 5) + ".")
        if sizes[purpose]:
            lines.append(f"Plot/unit size where stated in the listing URL ({purpose}): " +
                         ", ".join(f"{k} {v:,}" for k, v in sizes[purpose].most_common()) + ".")
    lines.append("Caveat: counts come from sitemap URLs, not verified listing pages; listings may be sold, expired or "
                 "duplicated, and URLs do not include the city.")
    lines.append(f"بالعربية: عدد إعلانات العقارات في وصلت للبيع ({by_purpose.get('for sale', 0):,}) وللإيجار "
                 f"({by_purpose.get('for rent', 0):,}) حسب نوع العقار مثل الشقق والفلل والأراضي والأدوار، "
                 "محسوب من روابط خرائط الموقع العامة وليس من صفحات الإعلانات نفسها.")
    return {"key": "inventory", "title": "Wasalt listing inventory: number of properties listed for sale and for rent",
            "url": "https://wasalt.sa/en/", "text": "\n".join(lines), "retrieved_from": ", ".join(sources)}


def static_pages_doc(static_entries: list[SitemapEntry], *, fetched: str, source: str) -> dict:
    names = sorted({titleize(urlsplit(e.loc).path.rstrip("/").rsplit("/", 1)[-1]) for e in static_entries
                    if "/s/" in e.loc})
    return {"key": "static-pages", "title": "Wasalt information pages",
            "url": "https://wasalt.sa/en/",
            "text": (f"Wasalt's public information pages listed in its sitemap (fetched {fetched}): "
                     + ", ".join(names) + ". The sitemap includes separate terms pages for an e-wallet and for "
                     "auctions, plus 'Wasalt Business' and 'Masterplan' pages."),
            "retrieved_from": source}
