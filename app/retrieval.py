"""Corpus loading (validated + integrity-checked) and deterministic bilingual BM25 retrieval.

The corpus file is treated as untrusted input even though our own scraper wrote it
(blueprint C03/C07): schema version, fields, hosts, content hash and trap patterns are
re-checked at load, and bad records are rejected with a reason instead of silently used.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .textguard import has_high, scan_text, strip_invisible

SCHEMA_VERSION = 1
SITE_HOSTS = {"darglobal": "darglobal.co.uk", "wasalt": "wasalt.sa"}
SOURCE_TYPES = {"archive_snapshot", "live_sitemap_derived", "live_page", "manual"}
MAX_TEXT_CHARS = 25_000
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUIRED_STR = ("doc_id", "site", "url", "title", "lang", "text", "source_type", "retrieved_from", "captured_at",
                 "content_sha256")


class CorpusError(RuntimeError):
    pass


@dataclass(frozen=True)
class Document:
    doc_id: str
    site: str
    url: str
    title: str
    lang: str
    text: str
    source_type: str
    retrieved_from: str
    captured_at: str

    @property
    def archive_url(self) -> str | None:
        return self.retrieved_from if _host_is(self.retrieved_from, "web.archive.org") else None


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc: Document
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


def _host_is(url: str, base: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.username and (host == base or host.endswith("." + base))


def validate_record(rec: object) -> Document:
    if not isinstance(rec, dict):
        raise ValueError("record is not an object")
    if rec.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {rec.get('schema_version')!r}")
    for key in _REQUIRED_STR:
        if not isinstance(rec.get(key), str) or not rec[key]:
            raise ValueError(f"missing or non-string field {key}")
    site = rec["site"]
    if site not in SITE_HOSTS:
        raise ValueError(f"unknown site {site!r}")
    if not _host_is(rec["url"], SITE_HOSTS[site]):
        raise ValueError("url host does not match site allowlist")
    if rec["source_type"] not in SOURCE_TYPES:
        raise ValueError("unknown source_type")
    if rec["lang"] not in ("en", "ar"):
        raise ValueError("unknown lang")
    if not _DATE.match(rec["captured_at"]):
        raise ValueError("captured_at is not YYYY-MM-DD")
    text = rec["text"]
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("text too long")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != rec["content_sha256"]:
        raise ValueError("content hash mismatch")
    if has_high(scan_text(text)):
        raise ValueError("text contains instruction-like content aimed at AI systems")
    if has_high(scan_text(rec["title"])):
        raise ValueError("title contains instruction-like content aimed at AI systems")
    return Document(rec["doc_id"], site, rec["url"], rec["title"][:300], rec["lang"], text, rec["source_type"],
                    rec["retrieved_from"][:500], rec["captured_at"])


def load_corpus(path: Path) -> tuple[list[Document], list[str]]:
    if not path.is_file():
        raise CorpusError(f"corpus file not found: {path.name}")
    docs: list[Document] = []
    rejected: list[str] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                doc = validate_record(json.loads(line))
            except ValueError as exc:  # json.JSONDecodeError is a ValueError
                rejected.append(f"line {n}: {exc}")
                continue
            if doc.doc_id in seen:
                rejected.append(f"line {n}: duplicate doc_id")
                continue
            seen.add(doc.doc_id)
            docs.append(doc)
    if not docs:
        raise CorpusError("corpus contains no valid documents")
    return sorted(docs, key=lambda d: d.doc_id), rejected


# ------------------------------------------------------------------ chunking
def chunk_document(doc: Document, target: int = 1100) -> list[Chunk]:
    pieces: list[str] = []
    for line in doc.text.split("\n"):
        while len(line) > target * 1.5:  # very long paragraphs: split at a sentence end near target
            cut = max(line.rfind(". ", 0, target), line.rfind("。", 0, target), line.rfind(" ", 0, target))
            cut = cut + 1 if cut > target // 3 else target
            pieces.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            pieces.append(line)
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for piece in pieces:
        if cur and size + len(piece) > target:
            chunks.append(cur)
            cur = [cur[-1]] if len(cur[-1]) < target // 3 else []  # one short line of overlap for context
            size = sum(len(p) + 1 for p in cur)
        cur.append(piece)
        size += len(piece) + 1
    if cur:
        chunks.append(cur)
    return [Chunk(f"{doc.doc_id}#{i}", doc, "\n".join(c)) for i, c in enumerate(chunks)]


# -------------------------------------------------------------- tokenising
_AR_DIACRITICS = re.compile("[ً-ٰٟۖ-ۭـ]")
_TOKEN = re.compile(r"[a-z0-9]+|[ء-ي]+")
_AR_NORMAL = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي",
                            **{chr(0x0660 + i): str(i) for i in range(10)},   # Arabic-Indic digits
                            **{chr(0x06F0 + i): str(i) for i in range(10)}})  # Extended Arabic-Indic digits


def normalize(text: str) -> str:
    text = strip_invisible(text).lower()
    return _AR_DIACRITICS.sub("", text).translate(_AR_NORMAL)


_STOP_RAW = set(
    "a an the and or of to in on at for with by from is are was were be been it its this that these those as "
    "what which who whom whose when where why how do does did can could would should will shall may might "
    "i me my we our you your he she they them their there here about tell show give list please any some "
    "has have had not no yes also than then so if into over under more most much many very just".split()
) | set("في من على الى إلى عن مع هذا هذه ذلك التي الذي ما هل او أو ثم كل".split())
_STOP = {normalize(w) for w in _STOP_RAW}  # same normalisation as tokens, so "على" (-> "علي") is matched
SYNONYMS: dict[str, tuple[str, ...]] = {
    "uae": ("dubai", "emirate"), "emirate": ("uae",), "ksa": ("saudi",), "saudi": ("ksa",),
    "price": ("starting", "aed", "sar", "cost", "priced"), "cost": ("price",), "cheap": ("price", "starting"),
    "beach": ("sea", "waterfront", "coastal", "seafront", "beachfront"), "sea": ("beach", "waterfront"),
    "flat": ("apartment",), "apartment": ("residence", "flat"), "home": ("residence", "villa", "apartment"),
    "rent": ("rental", "lease"), "rental": ("rent",), "buy": ("sale",), "sale": ("buy",),
    "completion": ("handover", "complete", "delivery"), "handover": ("completion",), "ready": ("completion", "status"),
    "ceo": ("chief", "executive"), "listed": ("exchange", "stock"), "office": ("offices", "contact"),
    "riyadh": ("رياض",), "jeddah": ("جده",), "dammam": ("دمام",), "makkah": ("مكه", "mecca"), "mecca": ("makkah",),
    "cover": ("coverage",), "district": ("neighbourhood", "area"), "area": ("district",),
}
# DarGlobal's pages are English, so Arabic questions are bridged with a small domain glossary (query side only).
AR_TO_EN: dict[str, str] = {
    "مشاريع": "projects", "مشروع": "project", "جلوبال": "darglobal", "وصلت": "wasalt", "عمان": "oman",
    "مسقط": "muscat", "دبي": "dubai", "الامارات": "uae emirates", "الإمارات": "uae emirates", "السعودية": "saudi",
    "الرياض": "riyadh", "جدة": "jeddah", "الدمام": "dammam", "الخبر": "khobar", "مكة": "makkah", "قطر": "qatar",
    "الدوحة": "doha", "لندن": "london", "بريطانيا": "uk london", "إسبانيا": "spain", "اسبانيا": "spain",
    "المالديف": "maldives", "فيلا": "villa", "فلل": "villas", "شقة": "apartment", "شقق": "apartments", "أرض": "land",
    "للبيع": "sale", "بيع": "sale", "للإيجار": "rent", "للايجار": "rent", "إيجار": "rent", "سعر": "price",
    "أسعار": "prices", "مرافق": "amenities", "ترامب": "trump", "برج": "tower", "فندق": "hotel", "تسليم": "completion",
    "الرئيس": "ceo", "التنفيذي": "executive", "بورصة": "stock exchange", "غرف": "bedrooms", "حي": "district",
    "أحياء": "districts", "احياء": "districts", "كم": "inventory total", "عدد": "number inventory total",
}
_KEEP_ENDINGS = {"united", "parking", "during", "morning", "evening", "ceiling", "string", "nothing", "something"}
_COUNT_Q = re.compile(r"\b(?:how many|number of|count|total|كم|عدد)\b", re.IGNORECASE)


def _stem(tok: str) -> str:
    if tok.isascii():
        if tok in _KEEP_ENDINGS:
            return tok
        if len(tok) > 4 and tok.endswith("ies"):
            return tok[:-3] + "y"
        if len(tok) > 4 and tok.endswith(("ses", "xes", "zes", "ches", "shes")):
            return tok[:-2]
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
            tok = tok[:-1]
        if len(tok) > 5 and tok.endswith("ing"):
            return tok[:-3]  # listing/listings -> list
        if len(tok) > 5 and tok.endswith("ed"):
            return tok[:-2]  # listed -> list
        return tok
    if tok.startswith("وال") and len(tok) > 5:
        return tok[3:]
    if tok.startswith(("ال", "بال", "لل")) and len(tok) > 4:
        return tok[3:] if tok.startswith(("بال",)) else tok[2:]
    return tok


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(normalize(text)) if t not in _STOP and (len(t) > 1 or t.isdigit())]


_AR_EXPANSION: dict[str, list[str]] = {}


def _ar_expansion() -> dict[str, list[str]]:
    if not _AR_EXPANSION:  # built lazily so keys pass through the same normalisation as queries
        for ar, en in AR_TO_EN.items():
            for key in tokenize(ar):
                _AR_EXPANSION.setdefault(key, []).extend(tokenize(en))
    return _AR_EXPANSION


def _query_weights(query: str) -> dict[str, float]:
    weights: dict[str, float] = {}
    for tok in tokenize(query):
        weights[tok] = 1.0
    expansion = _ar_expansion()
    for tok in list(weights):
        for en in expansion.get(tok, ()):
            weights[en] = max(weights.get(en, 0.0), 0.9)
    for tok in list(weights):
        for syn in SYNONYMS.get(tok, ()):
            for s in tokenize(syn):
                weights.setdefault(s, 0.4)
    if _COUNT_Q.search(query):
        for s in ("inventory", "total", "list"):
            weights.setdefault(s, 0.6)
    return weights


# ------------------------------------------------------------------- BM25
class BM25Index:
    def __init__(self, chunks: list[Chunk], *, k1: float = 1.4, b: float = 0.72) -> None:
        self.chunks = sorted(chunks, key=lambda c: c.chunk_id)
        self.k1, self.b = k1, b
        self.doc_len: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, ch in enumerate(self.chunks):
            toks = tokenize(ch.doc.title) * 2 + tokenize(ch.text)  # title terms count double
            self.doc_len.append(len(toks))
            for tok, tf in Counter(toks).items():
                self.postings[tok].append((i, tf))
        n = len(self.chunks)
        self.avgdl = (sum(self.doc_len) / n) if n else 1.0
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def search(self, query: str, *, k: int = 6, max_per_doc: int = 2, min_relative: float = 0.22) -> list[Hit]:
        weights = _query_weights(query)
        scores: dict[int, float] = defaultdict(float)
        for tok, w in weights.items():
            idf = self.idf.get(tok)
            if idf is None:
                continue
            for i, tf in self.postings[tok]:
                dl = self.doc_len[i]
                scores[i] += w * idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        q = normalize(query)
        boost_site = ("wasalt" if ("wasalt" in q or "وصلت" in q) and "darglobal" not in q.replace(" ", "")
                      else "darglobal" if ("darglobal" in q.replace(" ", "") or "دار جلوبال" in q) else None)
        if boost_site:
            for i in scores:
                if self.chunks[i].doc.site == boost_site:
                    scores[i] *= 1.3
        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], self.chunks[kv[0]].chunk_id))
        top = ranked[0][1]
        hits: list[Hit] = []
        per_doc: Counter[str] = Counter()
        for i, score in ranked:
            if score < top * min_relative:
                break
            doc_id = self.chunks[i].doc.doc_id
            if per_doc[doc_id] >= max_per_doc:
                continue
            per_doc[doc_id] += 1
            hits.append(Hit(self.chunks[i], round(score, 4)))
            if len(hits) >= k:
                break
        return hits


def build_index(docs: list[Document]) -> BM25Index:
    return BM25Index([c for d in docs for c in chunk_document(d)])
