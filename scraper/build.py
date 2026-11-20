"""Build the chatbot corpus.

    python -m scraper.build [--max-darglobal N] [--no-darglobal-archive] [--no-wasalt-archive]

Pipeline: live probe (documents bot protection) -> robots.txt/llms.txt trap scan -> sitemaps
-> Internet Archive captures for blocked pages -> extraction + trap scan + quarantine
-> boilerplate removal -> data/corpus.jsonl, data/manifest.json, data/trap_report.json.

Exit codes: 0 complete, 3 written but incomplete (see manifest stages), 1 fatal (nothing written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.textguard import scan_text

from . import config, wasalt
from .archive import ArchiveError, Capture, cdx_captures, index_by_page
from .extract import darglobal_catalog, extract
from .http import PoliteClient, is_bot_challenge
from .sitemaps import SitemapEntry, SitemapError, parse_sitemap
from .traps import scan_html
from .urlpolicy import allowed_site, iri_to_uri, page_key

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "raw" / "archive"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; shipped data must be readable by the container's app user
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_LINE_SEPARATORS = {"\u2028": "\\u2028", "\u2029": "\\u2029", "\u0085": "\\u0085"}


def jsonl_line(record: dict) -> str:
    """One JSON object per line. Unicode line separators are escaped so tools that split on them
    (str.splitlines, older JavaScript parsers) still see exactly one record per line."""
    text = json.dumps(record, ensure_ascii=False, sort_keys=True)
    for raw, escaped in _LINE_SEPARATORS.items():
        text = text.replace(raw, escaped)
    return text + "\n"


def dg_priority(url: str) -> int:
    path = urlsplit(url).path.lower()
    if path.startswith("/blog/"):
        return 3
    if path.startswith(("/press/", "/insights/")):
        return 2
    return 1


class Build:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client
        self.items: list[dict] = []
        self.traps: list[dict] = []
        self.docs: list[dict] = []
        self.stages: dict[str, dict] = {}
        self.catalog: tuple[str, str, dict] | None = None  # (timestamp, raw_url, catalog)
        self.fetched_date = datetime.now(timezone.utc).date().isoformat()

    # ---------------------------------------------------------------- helpers
    def stage(self, name: str, status: str, **info: object) -> None:
        self.stages[name] = {"status": status, **info}
        print(f"[stage] {name}: {status} {info if info else ''}", flush=True)

    def add_traps(self, source: str, url: str, findings: list[dict]) -> None:
        for f in findings:
            self.traps.append({"source": source, "url": url, **f})

    def scan_plain(self, source: str, url: str, text: str, location: str) -> None:
        self.add_traps(source, url, [{"location": location, "pattern": f.pattern, "severity": f.severity,
                                      "excerpt": f.excerpt} for f in scan_text(text)])

    def fetch_sitemap(self, url: str) -> list[SitemapEntry] | None:
        res = self.client.get(url, max_bytes=config.MAX_SITEMAP_BYTES)
        if not res.ok:
            self.items.append({"stage": "sitemap", "url": url, "status": res.status, "http_status": res.http_status,
                               "detail": res.detail[:120]})
            return None
        try:
            kind, entries = parse_sitemap(res.body)
        except SitemapError as exc:
            self.items.append({"stage": "sitemap", "url": url, "status": "parse_error", "detail": str(exc)[:200]})
            return None
        self.scan_plain("sitemap", url, res.body[:2_000_000].decode("utf-8", "replace"), "sitemap_xml")
        if kind == "sitemapindex":
            children: list[SitemapEntry] = []
            for child in entries[:20]:
                children.extend(self.fetch_sitemap(child.loc) or [])
            entries = children
        self.items.append({"stage": "sitemap", "url": url, "status": "ok", "entries": len(entries)})
        return entries

    def cdx(self, pattern: str, *, max_pages: int = 10) -> tuple[list[Capture], str]:
        """Live CDX query; on failure fall back to the last successful result (disclosed as 'cached_index')."""
        path = DATA / "raw" / "cdx" / f"{hashlib.sha1(pattern.encode()).hexdigest()[:16]}.json"
        try:
            caps = cdx_captures(self.client, pattern, max_pages=max_pages)
        except ArchiveError:
            if not path.exists():
                raise
            saved = json.loads(path.read_text("utf-8"))
            return [Capture(*row) for row in saved["captures"]], f"cached_index from {saved['fetched_at']}"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps({"pattern": pattern, "fetched_at": now_iso(),
                                       "captures": [[c.timestamp, c.original, c.length] for c in caps]}))
        return caps, "live_index"

    def fetch_capture(self, cap: Capture) -> tuple[str | None, str]:
        CACHE.mkdir(parents=True, exist_ok=True)
        path = CACHE / f"{cap.timestamp}_{hashlib.sha1(cap.original.encode()).hexdigest()[:16]}.html"
        if path.exists():  # archived captures are immutable, so the cache key (timestamp + URL) is complete
            body = path.read_bytes()
            if is_bot_challenge(body):
                return None, "archived_copy_is_bot_challenge"
            return body.decode("utf-8", "replace"), "ok"
        res = self.client.get(cap.raw_url, accept_types=("text/html",))
        if res.status == "blocked_bot_challenge":
            path.write_bytes(res.body)  # remember the stub so reruns skip it without a request
            return None, "archived_copy_is_bot_challenge"
        if not res.ok:
            return None, f"archive_{res.status}"
        path.write_bytes(res.body)
        return res.body.decode("utf-8", "replace"), "ok"

    def add_page(self, site: str, url: str, html: str, *, source_type: str, retrieved_from: str,
                 captured_at: str, timestamp: str = "") -> int:
        soup = BeautifulSoup(html, "lxml")
        self.add_traps(source_type, url, scan_html(html, soup))
        ex = extract(html, soup, site=site)
        if site == "darglobal" and ex.next_data:
            cat = darglobal_catalog(ex.next_data)
            if cat and (self.catalog is None or timestamp > self.catalog[0]):
                self.catalog = (timestamp, retrieved_from, cat)
        title = ex.title or urlsplit(url).path.strip("/").replace("-", " ").title() or site
        self.docs.append({"site": site, "url": url, "title": title, "description": ex.description, "lang": ex.lang,
                          "visible_lines": ex.visible_lines, "fact_lines": ex.fact_lines, "source_type": source_type,
                          "retrieved_from": retrieved_from, "captured_at": captured_at})
        return len(ex.quarantined)

    def archive_page(self, site: str, url: str, candidates: list[Capture]) -> None:
        if not candidates:
            self.items.append({"stage": "archive_page", "site": site, "url": url, "status": "archive_no_capture"})
            return
        last = "archive_no_capture"
        for cap in candidates[: config.MAX_CAPTURE_TRIES]:
            html, status = self.fetch_capture(cap)
            if html is None:
                last = status
                continue
            quarantined = self.add_page(site, url, html, source_type="archive_snapshot", retrieved_from=cap.raw_url,
                                        captured_at=cap.iso_date, timestamp=cap.timestamp)
            self.items.append({"stage": "archive_page", "site": site, "url": url, "status": "ok",
                               "capture": cap.timestamp, "quarantined_lines": quarantined})
            return
        self.items.append({"stage": "archive_page", "site": site, "url": url, "status": last})

    # ----------------------------------------------------------------- stages
    def probe_live(self) -> None:
        results = {}
        for site, origin, home in (("darglobal", "https://darglobal.co.uk", "https://darglobal.co.uk/"),
                                   ("wasalt", "https://wasalt.sa", "https://wasalt.sa/en/")):
            res = self.client.get(home)
            results[site] = res.status
            self.items.append({"stage": "live_probe", "site": site, "url": home, "status": res.status,
                               "http_status": res.http_status})
            llms = self.client.get(f"{origin}/llms.txt")
            self.items.append({"stage": "live_probe", "site": site, "url": f"{origin}/llms.txt", "status": llms.status,
                               "http_status": llms.http_status})
            if llms.ok and "text/plain" in llms.content_type:
                self.scan_plain("llms_txt", f"{origin}/llms.txt", llms.body.decode("utf-8", "replace"), "llms_txt")
        for origin, text in sorted(self.client.robots_raw.items()):
            self.scan_plain("robots_txt", f"{origin}/robots.txt", text, "robots_txt")
        self.stage("live_probe", "ok", homepage_status=results,
                   note="pages returning blocked_bot_challenge are not fetched directly; no bypass is attempted")

    def darglobal(self, max_pages: int, use_archive: bool) -> None:
        origin = "https://darglobal.co.uk"
        sitemaps = self.client.sitemaps_for(origin) or [f"{origin}/sitemap.xml"]
        entries: list[SitemapEntry] = []
        for sm in sitemaps:
            entries.extend(self.fetch_sitemap(sm) or [])
        urls: dict[str, str] = {}
        for e in entries:
            if allowed_site(e.loc) != "darglobal":
                continue
            path = urlsplit(e.loc).path.lower()
            if path.startswith(config.DARGLOBAL_SKIP_PREFIXES):
                self.items.append({"stage": "select", "site": "darglobal", "url": e.loc, "status": "skipped_transactional"})
                continue
            if not self.client.robots_allows(e.loc)[0]:
                self.items.append({"stage": "select", "site": "darglobal", "url": e.loc, "status": "blocked_robots"})
                continue
            urls.setdefault(page_key(e.loc), e.loc)
        ordered = sorted(urls.values(), key=lambda u: (dg_priority(u), u))
        dropped = ordered[max_pages:]
        for u in dropped:
            self.items.append({"stage": "select", "site": "darglobal", "url": u, "status": "skipped_page_cap"})
        self.stage("darglobal_sitemap", "ok" if entries else "failed", sitemap_urls=len(entries),
                   selected=min(len(ordered), max_pages), over_cap=len(dropped))
        if not use_archive or not entries:
            return
        try:
            captures, index_source = self.cdx("darglobal.co.uk/*")
            index = index_by_page(captures)
        except ArchiveError as exc:
            self.stage("darglobal_archive", "failed", error=str(exc))
            return
        started = time.monotonic()
        for i, url in enumerate(ordered[:max_pages], 1):
            self.archive_page("darglobal", url, index.get(page_key(url), []))
            if i % 25 == 0:
                print(f"  darglobal {i}/{min(len(ordered), max_pages)} ({time.monotonic() - started:.0f}s)", flush=True)
        ok = sum(1 for it in self.items if it.get("stage") == "archive_page" and it.get("site") == "darglobal"
                 and it["status"] == "ok")
        self.stage("darglobal_archive", "ok" if ok else "failed", pages_ok=ok, index_pages=len(index),
                   index_source=index_source)

    def darglobal_catalog_docs(self) -> None:
        if not self.catalog:
            self.stage("darglobal_catalog", "failed", error="no capture contained globalData")
            return
        ts, raw_url, cat = self.catalog
        captured = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        if cat["projects"]:
            lines = [f"DarGlobal project list: all {len(cat['projects'])} projects and their countries, from the site's "
                     f"global data (archived copy captured {captured}):"]
            lines += [f"- Project: {p['name']} — {p['country'] or 'country not stated'}" for p in cat["projects"]]
            self.docs.append({"site": "darglobal", "url": "https://darglobal.co.uk/projects",
                              "title": "DarGlobal project list: all projects and countries", "description": "",
                              "lang": "en", "visible_lines": [], "fact_lines": lines, "source_type": "archive_snapshot",
                              "retrieved_from": raw_url, "captured_at": captured})
        groups: dict[tuple[str, str], list[dict]] = {}
        for item in cat["menu"]:
            groups.setdefault((item["section"], item["group"]), []).append(item)
        for (section, group), items in sorted(groups.items()):
            lines = [f"DarGlobal projects in {group} — {section} (from the website menu; archived copy captured "
                     f"{captured}):"]
            for it in items:
                href = it["href"]
                link = f"https://darglobal.co.uk{href}" if href.startswith("/") else ""
                loc = f" ({it['location']})" if it["location"] else ""
                lines.append(f"- Project: {it['name']}{loc}{' — ' + link if link else ''}")
            self.docs.append({"site": "darglobal", "url": "https://darglobal.co.uk/projects",
                              "title": (f"DarGlobal projects in {group}" if section.lower().startswith("real estate")
                                        else f"DarGlobal projects in {group} ({section})"),
                              "description": "", "lang": "en",
                              "visible_lines": [], "fact_lines": lines, "source_type": "archive_snapshot",
                              "retrieved_from": raw_url, "captured_at": captured})
        self.stage("darglobal_catalog", "ok", projects=len(cat["projects"]), menu_items=len(cat["menu"]))

    def wasalt_sitemaps(self) -> None:
        sitemaps = [s for s in self.client.sitemaps_for("https://wasalt.sa") if "_en_" in s]
        got: dict[str, list[SitemapEntry]] = {}
        for sm in sitemaps:
            entries = self.fetch_sitemap(sm)
            if entries is not None:
                got[sm.rsplit("/", 1)[-1].split(".")[0]] = entries
        def pick(prefix: str) -> tuple[str, list[SitemapEntry]]:
            for name, entries in got.items():
                if name.startswith(prefix):
                    return name, entries
            return "", []
        derived: list[dict] = []
        cat_name, cat = pick("category_sitemap")
        srp_name, srp = pick("rental_srp_sitemap")
        if cat or srp:
            derived += wasalt.coverage_docs(cat, srp, fetched=self.fetched_date,
                                            sources={"category": cat_name or "n/a", "rental": srp_name or "n/a"})
        listings = {n: e for n, e in got.items() if n.startswith(("product_sitemap", "rental_pdp_sitemap"))}
        if listings:
            derived.append(wasalt.inventory_doc(listings, fetched=self.fetched_date, sources=sorted(listings)))
        st_name, st = pick("static_sitemap")
        if st:
            derived.append(wasalt.static_pages_doc(st, fetched=self.fetched_date, source=st_name))
        for d in derived:
            self.docs.append({"site": "wasalt", "url": d["url"], "title": d["title"], "description": "", "lang": "en",
                              "visible_lines": [], "fact_lines": d["text"].split("\n"),
                              "source_type": "live_sitemap_derived",
                              "retrieved_from": f"https://cdn.wasalt.sa/sitemap/ ({d['retrieved_from']})",
                              "captured_at": self.fetched_date})
        self.stage("wasalt_sitemaps", "ok" if len(got) == len(sitemaps) and got else ("partial" if got else "failed"),
                   fetched=sorted(got), expected=len(sitemaps), derived_docs=len(derived))

    def wasalt_archive(self) -> None:
        ok = 0
        for target in config.WASALT_ARCHIVE_TARGETS:
            pattern = iri_to_uri(target).split("://", 1)[1]
            try:
                captures, _source = self.cdx(pattern, max_pages=1)
                caps = index_by_page(captures).get(page_key(target), [])
            except ArchiveError as exc:
                self.items.append({"stage": "archive_page", "site": "wasalt", "url": target, "status": "cdx_failed",
                                   "detail": str(exc)[:200]})
                continue
            before = len(self.docs)
            self.archive_page("wasalt", target, caps)
            ok += len(self.docs) > before
        self.stage("wasalt_archive", "ok" if ok else "failed", pages_ok=ok, targets=len(config.WASALT_ARCHIVE_TARGETS))

    def manual_intake(self) -> None:
        folder = DATA / "manual"
        if not folder.is_dir():
            return
        count = 0
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in (".html", ".htm", ".json") or path.stat().st_size > config.MAX_HTML_BYTES:
                continue
            if path.suffix.lower() == ".json":
                try:
                    payload = json.loads(path.read_text("utf-8"))
                except ValueError:
                    self.items.append({"stage": "manual", "url": path.name, "status": "invalid_json"})
                    continue
                for item in (payload.get("items") or []) if isinstance(payload, dict) else []:
                    url, text = str(item.get("url", "")), str(item.get("text", ""))
                    site = allowed_site(url)
                    if site not in ("darglobal", "wasalt") or not text:
                        self.items.append({"stage": "manual", "url": url or path.name, "status": "out_of_scope"})
                        continue
                    self.scan_plain("manual", url, text, "visible_text")
                    lines = [l for l in (ln.strip() for ln in text.splitlines()) if len(l) >= 3]
                    self.docs.append({"site": site, "url": url, "title": str(item.get("title") or url)[:200],
                                      "description": "", "lang": "en", "visible_lines": lines, "fact_lines": [],
                                      "source_type": "manual", "retrieved_from": f"data/manual/{path.name}",
                                      "captured_at": self.fetched_date})
                    count += 1
            else:
                html = path.read_text("utf-8", errors="replace")
                soup = BeautifulSoup(html, "lxml")
                canon = soup.find("link", rel="canonical") or soup.find("meta", property="og:url")
                url = (canon.get("href") or canon.get("content") or "") if canon else ""
                site = allowed_site(url)
                if site not in ("darglobal", "wasalt"):
                    self.items.append({"stage": "manual", "url": path.name, "status": "no_allowlisted_canonical_url"})
                    continue
                self.add_page(site, url, html, source_type="manual", retrieved_from=f"data/manual/{path.name}",
                              captured_at=self.fetched_date)
                count += 1
        self.stage("manual_intake", "ok", documents=count)

    # --------------------------------------------------------------- finalise
    def finalise(self) -> list[dict]:
        records: list[dict] = []
        fetched_at = now_iso()
        for site in ("darglobal", "wasalt"):
            site_docs = [d for d in self.docs if d["site"] == site]
            freq = Counter(line for d in site_docs for line in set(d["visible_lines"]))
            threshold = max(4, int(0.25 * len(site_docs)))
            for d in site_docs:
                visible = [l for l in d["visible_lines"] if freq[l] <= threshold]
                parts = []
                if d["description"] and d["description"] not in visible:
                    parts.append(d["description"])
                parts += d["fact_lines"]
                parts += visible
                text = "\n".join(parts)[: config.MAX_DOC_CHARS].strip()
                if len(text) < 80:
                    self.items.append({"stage": "finalise", "site": site, "url": d["url"], "status": "too_little_text"})
                    continue
                key = f"{d['url']}|{d['title']}|{d['source_type']}"
                records.append({
                    "schema_version": config.CORPUS_SCHEMA_VERSION,
                    "doc_id": f"{site}:{hashlib.sha1(key.encode()).hexdigest()[:12]}",
                    "site": site, "url": d["url"], "title": d["title"][:300], "lang": d["lang"], "text": text,
                    "source_type": d["source_type"], "retrieved_from": d["retrieved_from"],
                    "captured_at": d["captured_at"], "fetched_at": fetched_at,
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                })
        seen_hash: set[str] = set()
        unique = []
        for r in sorted(records, key=lambda r: (r["site"], r["url"], r["doc_id"])):
            if r["content_sha256"] in seen_hash:
                self.items.append({"stage": "finalise", "site": r["site"], "url": r["url"], "status": "duplicate_content"})
                continue
            seen_hash.add(r["content_sha256"])
            unique.append(r)
        return sorted(unique, key=lambda r: r["doc_id"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-darglobal", type=int, default=config.MAX_DARGLOBAL_PAGES)
    ap.add_argument("--no-darglobal-archive", action="store_true")
    ap.add_argument("--no-wasalt-archive", action="store_true")
    ap.add_argument("--allow-partial", action="store_true",
                    help="write outputs even if a stage failed (default: keep the last complete corpus)")
    args = ap.parse_args(argv)
    if not 0 <= args.max_darglobal <= 2000:
        ap.error("--max-darglobal must be between 0 and 2000")
    started = now_iso()
    t0 = time.monotonic()
    with PoliteClient() as client:
        b = Build(client)
        b.probe_live()
        b.darglobal(args.max_darglobal, use_archive=not args.no_darglobal_archive)
        b.darglobal_catalog_docs()
        b.wasalt_sitemaps()
        if not args.no_wasalt_archive:
            b.wasalt_archive()
        b.manual_intake()
        records = b.finalise()
        requests = len(client.log)
        request_status = Counter(e["status"] for e in client.log)
    if not records:
        print("fatal: no documents produced", file=sys.stderr)
        return 1
    complete = all(s["status"] == "ok" for s in b.stages.values())
    status_counts: dict[str, dict[str, int]] = {}
    for it in b.items:
        status_counts.setdefault(f"{it.get('stage')}:{it.get('site', '-')}", Counter())[it["status"]] += 1
    manifest = {
        "schema_version": config.CORPUS_SCHEMA_VERSION, "scraper_version": config.SCRAPER_VERSION,
        "started_at": started, "finished_at": now_iso(), "duration_s": round(time.monotonic() - t0, 1),
        "complete": complete, "stages": b.stages,
        "documents": {"total": len(records), **Counter(f"{r['site']}:{r['source_type']}" for r in records)},
        "item_status_counts": {k: dict(v) for k, v in sorted(status_counts.items())},
        "requests": {"total": requests, **request_status},
        "config": {"user_agent": config.USER_AGENT, "archive_from": config.ARCHIVE_FROM,
                   "max_darglobal_pages": args.max_darglobal, "min_interval_s": config.MIN_INTERVAL_S,
                   "max_attempts": config.MAX_ATTEMPTS},
        "policy": ("Direct pages behind bot challenges are recorded, never bypassed. robots.txt rules are applied to "
                   "every host (including for archived copies of a site's pages). Archived copies come from the "
                   "Internet Archive and are dated per document."),
        "items": sorted(b.items, key=lambda i: (str(i.get("stage")), str(i.get("site", "")), str(i.get("url")))),
    }
    traps = sorted(b.traps, key=lambda t: (t["severity"] != "high", t["source"], t["url"], t["location"], t["pattern"]))
    trap_report = {
        "generated_at": now_iso(),
        "summary": {"total": len(traps), **Counter(f"{t['severity']}:{t['pattern']}" for t in traps)},
        "by_location": dict(Counter(t["location"] for t in traps)),
        "findings": traps,
    }
    suffix = "" if complete or args.allow_partial else ".partial"
    if suffix:
        print("incomplete run: keeping the last complete corpus; outputs written as *.partial.* for review",
              file=sys.stderr)
    atomic_write(DATA / f"corpus{suffix}.jsonl",
                 "".join(jsonl_line(r) for r in records))
    atomic_write(DATA / f"manifest{suffix}.json", json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=False))
    atomic_write(DATA / f"trap_report{suffix}.json", json.dumps(trap_report, ensure_ascii=False, indent=1))
    print(json.dumps({"documents": manifest["documents"], "complete": complete, "traps": trap_report["summary"]},
                     ensure_ascii=False))
    return 0 if complete else 3


if __name__ == "__main__":
    sys.exit(main())
