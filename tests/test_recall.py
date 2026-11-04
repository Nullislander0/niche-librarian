"""Synonym expansion, per-thread caps, token counts, outline, offline replay."""
import pytest

from engine.config import load_pack
from engine.crawl.fetcher import CacheMiss, Fetcher
from engine.index.store import Store, fts_query
from engine.rank.rerank import Ranker
from engine.search import Searcher
from engine.synonyms import Synonyms

from test_pipeline import FakeEmbedder, crawl_fixture


def test_synonyms_expand_whole_words_only():
    s = Synonyms([["arcgis server", "ags"], ["web adaptor", "webadaptor"], "portal for arcgis -> arcgis enterprise portal"])
    assert s.expand("AGS won't start") == ["arcgis server"]
    assert s.expand("configure webadaptor on IIS") == ["web adaptor"]
    assert s.expand("bags of data") == []
    assert s.expand("portal for arcgis login") == ["arcgis enterprise portal"]
    assert s.expand("arcgis enterprise portal login") == []  # one-way


def test_expansions_become_fts_phrases():
    assert fts_query("ags crash", ["arcgis server"]) == '"ags" OR "crash" OR "arcgis server"'


def test_pack_synonyms_load():
    assert any("webadaptor" in g for g in load_pack("esri_enterprise").synonyms)


def test_per_thread_cap():
    r = Ranker({"max_per_thread": 2}, {})
    cands = [{"rowid": i, "doc_id": f"a{i}", "parent_id": "q1", "source": "s"} for i in range(4)]
    out = r.rank(cands, {i: 1.0 - i / 10 for i in range(4)}, "x", [], limit=10)
    assert [c["doc_id"] for c in out] == ["a0", "a1"]


def test_search_reports_tokens_and_expansion():
    pack = load_pack("esri_enterprise")
    store = Store(":memory:", 16)
    crawl_fixture(pack, store)
    res = Searcher(pack, store=store, embedder=FakeEmbedder()).search("portal federation")
    assert res["expanded_with"] == []  # "portal" alone no longer expands
    assert res["results"][0]["tokens"]["chunk"] > 0
    out = Searcher(pack, store=store, embedder=FakeEmbedder()).outline()
    assert "gis_se" in out and "11.1" not in out.split("Version coverage")[0][-1:]


def test_offline_fetcher_never_touches_network(tmp_path):
    f = Fetcher("ua", tmp_path, offline=True)
    with pytest.raises(CacheMiss):
        f.get_json("https://example.invalid/x", {"a": 1})
    f._cache_write("https://example.invalid/x?a=1", '{"ok": true}')
    assert f.get_json("https://example.invalid/x", {"a": 1}) == {"ok": True}
    assert f.network_requests == 0


def test_fetch_token_budget_keeps_priority_order():
    pack = load_pack("esri_enterprise")
    store = Store(":memory:", 16)
    crawl_fixture(pack, store)
    s = Searcher(pack, store=store, embedder=FakeEmbedder())
    full = s.fetch("gis_se:q:1001#c0", scope="thread")
    tight = s.fetch("gis_se:q:1001#c0", scope="thread", max_tokens=1)
    assert "showing 3" in full and "showing 1" in tight
    assert "2 more replies not shown" in tight and "gis_se:a:2001#c0" in tight
