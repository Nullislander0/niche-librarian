"""Parser -> index -> search -> fetch, against synthetic fixtures and a fake embedder."""
import hashlib
import json
from pathlib import Path

import pytest

from engine.config import load_pack
from engine.index.store import Store, fts_query
from engine.indexer import index_document
from engine.parsers.base import CrawlContext, StopCrawl
from engine.parsers.stackexchange import Parser
from engine.search import Searcher
from engine.versions import VersionTagger

FIX = Path(__file__).parent / "fixtures"
DIM = 16


class FakeFetcher:
    def __init__(self):
        self.network_requests = 0
        self.calls = []

    def get_json(self, url, params=None, ttl=None):
        self.network_requests += 1
        self.calls.append((url, params))
        if url.endswith("/filters/create"):
            return {"items": [{"filter": "!fake"}], "quota_remaining": 299}
        if "/users/" in url:
            ids = url.rsplit("/", 1)[1].split(";")
            return {"items": [{"user_id": int(i), "creation_date": 1400000000, "reputation": 1} for i in ids],
                    "quota_remaining": 280}
        return json.loads((FIX / "se_questions.json").read_text())

    def pause_host(self, url, seconds):
        pass


class DictState(dict):
    def set(self, k, v):
        self[k] = v


class FakeEmbedder:
    def available(self):
        return True

    def _vec(self, text):
        h = hashlib.sha256(text.lower().encode()).digest()
        return [b / 255 for b in h[:DIM]]

    def embed_query(self, text):
        return self._vec(text)

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]


@pytest.fixture
def pack():
    return load_pack("esri_enterprise")


def crawl_fixture(pack, store, max_requests=None):
    src = pack.source("gis_se")
    src.options["tag_tiers"] = [{"tier": 1, "tags": ["arcgis-enterprise"]}]
    ctx = CrawlContext(source=src, fetcher=FakeFetcher(), state=DictState(), max_requests=max_requests)
    tagger = VersionTagger(pack.versions)
    docs = list(Parser(ctx).crawl())
    for d in docs:
        index_document(store, tagger, d)
    return docs, ctx


def test_parser_normalises_thread(pack):
    store = Store(":memory:", DIM)
    docs, ctx = crawl_fixture(pack, store)
    q, a1, a2 = docs
    assert q.doc_id == "gis_se:q:1001" and q.kind == "question"
    assert "&amp;" not in q.content_md and "& a token" in q.content_md
    assert a1.is_accepted and a1.is_top_voted and a1.parent_id == q.doc_id
    assert a1.author_reputation == 15000 and a1.author_created == 1400000000
    assert a2.author == "anon" and a2.author_id is None
    assert ctx.state["tag:gis:arcgis-enterprise"]["phase"] == "incremental"


def test_budget_stops_cleanly(pack):
    store = Store(":memory:", DIM)
    with pytest.raises(StopCrawl):
        crawl_fixture(pack, store, max_requests=1)


def test_answers_inherit_thread_versions(pack):
    store = Store(":memory:", DIM)
    crawl_fixture(pack, store)
    assert store.get_version_tags("gis_se:q:1001") == ["10.9.1", "11.1"]
    assert store.get_version_tags("gis_se:a:2001") == ["10.9.1", "11.1"]


def test_hybrid_search_ranks_accepted_answer_first(pack):
    store = Store(":memory:", DIM)
    crawl_fixture(pack, store)
    rows = store.chunks_missing_embeddings()
    emb = FakeEmbedder()
    store.put_embeddings([(r["id"], v) for r, v in zip(rows, emb.embed_documents([r["text"] for r in rows]))])
    res = Searcher(pack, store=store, embedder=emb).search("federation certificate 11.1", explain=True)
    assert res["mode"] == "hybrid" and res["query_versions"] == ["11.1"]
    top = res["results"][0]
    assert top["chunk_id"] == "gis_se:a:2001#c0"
    assert "accepted" in top["flags"] and top["explain"]["version_exact"] == 1.0


def test_filters(pack):
    store = Store(":memory:", DIM)
    crawl_fixture(pack, store)
    s = Searcher(pack, store=store, embedder=FakeEmbedder())
    assert s.search("federation", version="11.2", version_mode="filter")["results"] == []
    assert s.search("federation", version="11", version_mode="filter")["results"]
    assert s.search("federation", date_from="2024-01-01")["results"] == []
    assert s.search("federation", source_type="docs")["results"] == []


def test_fetch_scopes(pack):
    store = Store(":memory:", DIM)
    crawl_fixture(pack, store)
    s = Searcher(pack, store=store, embedder=FakeEmbedder())
    doc = s.fetch("gis_se:a:2001#c0")
    assert "cite: gis_se:a:2001#c0" in doc and "accepted answer" in doc
    thread = s.fetch("gis_se:a:2001#c0", scope="thread")
    assert thread.index("gis_se:q:1001") < thread.index("gis_se:a:2001") < thread.index("gis_se:a:2002")


def test_reindex_unchanged_keeps_chunks(pack):
    store = Store(":memory:", DIM)
    docs, _ = crawl_fixture(pack, store)
    before = store.get_chunk("gis_se:q:1001#c0")["rowid"]
    assert not store.upsert(docs[0], ["11.1"], ["x"])
    assert store.get_chunk("gis_se:q:1001#c0")["rowid"] == before


def test_fts_query_is_safe():
    assert fts_query('what is "11.1" AND (portal)') == '"11 1" OR "portal"'


def test_backfill_walks_a_date_cursor(pack):
    """Two pages: the second request starts from the first page's last creation date."""
    pages = [
        {"items": [{"question_id": 1, "title": "a", "creation_date": 100, "last_activity_date": 150},
                   {"question_id": 2, "title": "b", "creation_date": 200, "last_activity_date": 900}],
         "has_more": True, "quota_remaining": 200},
        {"items": [{"question_id": 3, "title": "c", "creation_date": 300, "last_activity_date": 300}],
         "has_more": False, "quota_remaining": 199},
        {"items": [], "has_more": False, "quota_remaining": 198},  # next run: incremental
    ]

    class PagedFetcher(FakeFetcher):
        def get_json(self, url, params=None, ttl=None):
            if url.endswith("/questions"):
                self.network_requests += 1
                self.calls.append((url, params))
                return pages.pop(0)
            return super().get_json(url, params, ttl)

    src = pack.source("gis_se")
    src.options["tag_tiers"] = [{"tier": 1, "tags": ["t"]}]
    src.options["fetch_author_profiles"] = False
    fetcher, state = PagedFetcher(), DictState()
    docs = list(Parser(CrawlContext(source=src, fetcher=fetcher, state=state)).crawl())
    assert state["tag:gis:t"] == {"phase": "incremental", "cursor": 900, "watermark": 900}
    list(Parser(CrawlContext(source=src, fetcher=fetcher, state=state)).crawl())
    q = [p for u, p in fetcher.calls if u.endswith("/questions")]
    assert [d.doc_id for d in docs] == ["gis_se:q:1", "gis_se:q:2", "gis_se:q:3"]
    assert q[0]["min"] is None and q[1]["min"] == 200 and "page" not in q[1]
    assert q[2]["sort"] == "activity" and q[2]["min"] == 900
    assert state["tag:gis:t"] == {"phase": "incremental", "cursor": 900, "watermark": 900}
