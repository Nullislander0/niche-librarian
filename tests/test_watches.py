"""Saved searches: only documents that are new since the last run are reported."""
import time

from engine import watches
from engine.config import load_pack
from engine.index.store import Store
from engine.indexer import index_document
from engine.parsers.base import Document
from engine.search import Searcher
from engine.versions import VersionTagger

from test_pipeline import FakeEmbedder


def doc(i, title, body):
    return Document(doc_id=f"gis_se:q:{i}", source="gis_se", source_type="forum", kind="question",
                    url=f"https://x/{i}", title=title, content_md=body, post_date=1700000000)


def test_first_seen_survives_recrawl():
    store = Store(":memory:", 16)
    store.upsert(doc(1, "Smallworld migration", "notes"), [], ["notes"])
    first = store.get_document("gis_se:q:1")["first_seen"]
    time.sleep(1.1)
    store.upsert(doc(1, "Smallworld migration", "notes, edited"), [], ["notes, edited"])
    d = store.get_document("gis_se:q:1")
    assert d["first_seen"] == first and d["indexed_at"] > first


def test_watch_reports_only_new_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(watches, "DATA_DIR", tmp_path)
    pack = load_pack("esri_enterprise")
    store = Store(":memory:", 16)
    tagger = VersionTagger(pack.versions)
    index_document(store, tagger, doc(1, "Smallworld to utility network migration", "FME workspace for Smallworld"))
    s = Searcher(pack, store=store, embedder=FakeEmbedder())
    watches.add(pack, "sw", "Smallworld migration")

    first = watches.run(pack, searcher=s, now=time.time())
    assert [r["chunk_id"] for r in first["results"]["sw"]] == ["gis_se:q:1#c0"]   # within the lookback
    assert first["report"] and "Smallworld to utility network migration" in open(first["report"]).read()

    time.sleep(1.1)
    assert watches.run(pack, searcher=s, now=time.time())["results"]["sw"] == []    # nothing new since

    time.sleep(1.1)
    index_document(store, tagger, doc(2, "Smallworld electric data model mapping", "Smallworld cable to UN"))
    new = watches.run(pack, searcher=s, now=time.time())["results"]["sw"]
    assert [r["chunk_id"] for r in new] == ["gis_se:q:2#c0"]
    assert watches.load(pack)["sw"]["last_run"] is not None


def test_watch_add_list_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(watches, "DATA_DIR", tmp_path)
    pack = load_pack("esri_enterprise")
    watches.add(pack, "a", "query a", min_score=0.5)
    assert list(watches.load(pack)) == ["a"] and watches.load(pack)["a"]["min_score"] == 0.5
    assert watches.remove(pack, "a") and not watches.remove(pack, "a")


class FakeWeb:
    def __init__(self, urls):
        self.urls = urls
        self.calls = []

    def search(self, query, count=20, freshness=None, site=None):
        self.calls.append((query, freshness, site))
        return [{"kind": "discussion", "url": u, "title": f"T {u}", "snippet": "s", "age": "1 day ago",
                 "backend": "fake"} for u in self.urls]


def test_web_watch_dedupes_and_never_writes_results(tmp_path, monkeypatch):
    monkeypatch.setattr(watches, "DATA_DIR", tmp_path)
    pack = load_pack("esri_enterprise")
    watches.add(pack, "w", "smallworld migration", web=True, site="community.safe.com")
    web = FakeWeb(["https://a/1", "https://a/2"])
    first = watches.run(pack, web_client=web, local=True)
    assert [h["url"] for h in first["web"]["w"]] == ["https://a/1", "https://a/2"]
    assert web.calls == [("smallworld migration", "pw", "community.safe.com")]
    assert first["report"] is None                                   # web results are never written
    stored = (tmp_path / "watches" / "esri_enterprise.json").read_text()
    assert "https://a/1" not in stored and "T https://a/1" not in stored   # only fingerprints kept
    web.urls = ["https://a/2", "https://a/3"]
    assert [h["url"] for h in watches.run(pack, web_client=web)["web"]["w"]] == ["https://a/3"]


def test_web_watch_without_key_is_reported_not_fatal(tmp_path, monkeypatch):
    from engine.websearch import BraveSearch
    monkeypatch.setattr(watches, "DATA_DIR", tmp_path)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    pack = load_pack("esri_enterprise")
    watches.add(pack, "w", "x", web=True)
    out = watches.run(pack, web_client=BraveSearch(key=None))
    assert "BRAVE_API_KEY" in out["errors"]["w"] and out["web"] == {}


class FakeNews:
    name = "brave-news"

    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def search(self, query, count=20, freshness=None, site=None):
        self.calls.append((query, freshness))
        return self.hits


def test_release_watches_from_pack_config(tmp_path, monkeypatch):
    monkeypatch.setattr(watches, "DATA_DIR", tmp_path)
    pack = load_pack("homelab")
    added = watches.add_releases(pack)
    assert "release-proxmox-ve" in added and "release-truenas" in added
    w = added["release-proxmox-ve"]
    assert w["backend"] == "brave-news" and w["query"].startswith('"Proxmox VE" release OR')

    hit = lambda url, title: {"kind": "news", "url": url, "title": title, "snippet": "", "age": ""}
    news = FakeNews([hit("https://www.proxmox.com/en/news/press-releases/ve-9-1/", "Proxmox VE 9.1 released"),
                     hit("https://news.example.com/patch-tuesday", "Microsoft Patch Tuesday fixes 1,411 flaws")])
    out = watches.run(pack, ["release-proxmox-ve"], web_client=news, local=False)
    assert [h["title"] for h in out["web"]["release-proxmox-ve"]] == ["Proxmox VE 9.1 released"]

    # Same page again under another spelling of its URL: not new. Re-adding keeps the history.
    news.hits = [hit("https://proxmox.com/en/news/press-releases/ve-9-1", "Proxmox VE 9.1 released")]
    watches.add_releases(pack)
    assert watches.run(pack, ["release-proxmox-ve"], web_client=news, local=False)["web"] == {
        "release-proxmox-ve": []}


def test_news_backend_parses_results(monkeypatch):
    from engine import websearch
    nb = websearch.backend("brave-news")
    nb.key = "test"
    monkeypatch.setattr(nb, "_get", lambda params: {"type": "news", "results": [
        {"title": "<strong>FME</strong> 2026.2 released", "url": "https://safe.com/x", "description": "New &amp; improved",
         "age": "2 days ago"}]})
    assert nb.search("FME release") == [{"kind": "news", "url": "https://safe.com/x", "title": "FME 2026.2 released",
                                          "snippet": "New & improved", "age": "2 days ago", "backend": "brave-news"}]
