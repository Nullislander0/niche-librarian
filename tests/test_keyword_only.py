"""Python builds that can't load SQLite extensions still get a working, keyword-only index."""
from engine.config import load_pack
from engine.index import store as store_mod
from engine.index.store import Store
from engine.indexer import embed_missing
from engine.search import Searcher

from test_pipeline import FakeEmbedder, crawl_fixture


def test_keyword_only_fallback(monkeypatch):
    monkeypatch.setattr(store_mod, "_load_sqlite_vec", lambda db: False)
    pack = load_pack("esri_enterprise")
    store = Store(":memory:", 16)
    assert not store.vec_ok
    crawl_fixture(pack, store)
    store.put_embeddings([(1, [0.0] * 16)])  # no-op, no error
    assert "skipped" in str(embed_missing(store))
    res = Searcher(pack, store=store, embedder=FakeEmbedder()).search("federation certificate 11.1")
    assert res["mode"] == "keyword" and res["results"][0]["chunk_id"] == "gis_se:a:2001#c0"
