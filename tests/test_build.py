import stat

from scraper.build import atomic_write


def test_atomic_write_replaces_content_and_is_readable_by_other_users(tmp_path):
    # Regression: mkstemp's 0600 mode made corpus.jsonl unreadable for the container's UID 1000 user.
    target = tmp_path / "corpus.jsonl"
    target.write_text("old")
    atomic_write(target, "new\n")
    assert target.read_text() == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["corpus.jsonl"]  # no temp files left behind


class _NoNetworkClient:
    log: list = []
    robots_status: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_build(stage_status: str):
    from scraper import build

    class FakeBuild(build.Build):
        def probe_live(self):
            self.stage("live_probe", "ok")

        def darglobal(self, max_pages, use_archive):
            self.stage("darglobal_archive", stage_status, error="simulated network failure")

        def darglobal_catalog_docs(self):
            pass

        def wasalt_sitemaps(self):
            self.docs.append({"site": "wasalt", "url": "https://wasalt.sa/en/", "title": "Wasalt", "description": "",
                              "lang": "en", "visible_lines": [], "fact_lines": ["Wasalt coverage document " * 8],
                              "source_type": "live_sitemap_derived", "retrieved_from": "sitemap",
                              "captured_at": "2026-11-20"})
            self.stage("wasalt_sitemaps", "ok")

        def wasalt_archive(self):
            pass

        def manual_intake(self):
            pass

    return FakeBuild


def test_incomplete_run_keeps_last_complete_corpus(tmp_path, monkeypatch):
    # Regression: a flaky archive.org query once replaced the 449-document corpus with an 82-document partial one.
    from scraper import build

    monkeypatch.setattr(build, "DATA", tmp_path)
    monkeypatch.setattr(build, "PoliteClient", _NoNetworkClient)
    monkeypatch.setattr(build, "Build", _fake_build("failed"))
    (tmp_path / "corpus.jsonl").write_text("GOOD\n")
    assert build.main([]) == 3
    assert (tmp_path / "corpus.jsonl").read_text() == "GOOD\n"
    assert (tmp_path / "corpus.partial.jsonl").exists() and (tmp_path / "manifest.partial.json").exists()


def test_complete_run_replaces_corpus(tmp_path, monkeypatch):
    from scraper import build

    monkeypatch.setattr(build, "DATA", tmp_path)
    monkeypatch.setattr(build, "PoliteClient", _NoNetworkClient)
    monkeypatch.setattr(build, "Build", _fake_build("ok"))
    (tmp_path / "corpus.jsonl").write_text("OLD\n")
    assert build.main([]) == 0
    assert "Wasalt coverage document" in (tmp_path / "corpus.jsonl").read_text()
    assert not (tmp_path / "corpus.partial.jsonl").exists()


def test_jsonl_lines_survive_splitlines():
    import json

    from scraper.build import jsonl_line

    record = {"text": "Line one line two para\u0085end", "title": "عقارات"}
    line = jsonl_line(record)
    assert len(line.splitlines()) == 1 and line.endswith("\n")
    assert json.loads(line) == record and "عقارات" in line  # Arabic stays readable
