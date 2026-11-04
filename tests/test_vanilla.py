"""Vanilla parser + HTML conversion against synthetic API responses."""
from engine.config import load_pack
from engine.index.store import Store
from engine.indexer import index_document
from engine.parsers.base import CrawlContext
from engine.parsers.html_md import html_to_markdown
from engine.parsers.vanilla import Parser
from engine.search import Searcher
from engine.versions import VersionTagger

from test_pipeline import DictState, FakeEmbedder

DISCUSSIONS = [{
    "discussionID": 501, "type": "question", "name": "Data Store validation fails on 11.3",
    "body": "<p>Our <strong>ArcGIS Data Store 11.3</strong> fails validation:</p><pre>ERROR 9999: relational store not ready</pre>",
    "dateInserted": "2025-03-01T10:00:00+00:00", "dateLastComment": "2025-03-02T10:00:00+00:00",
    "canonicalUrl": "https://forum.example/discussion/501/x", "url": "https://forum.example/discussion/501/x",
    "categoryID": 1, "countViews": 50, "countComments": 2, "score": None, "labelCodes": ["Accepted"],
    "attributes": {"question": {"status": "accepted"}}, "tags": [{"name": "Administration"}],
    "insertUser": {"userID": 1, "name": "asker", "label": "New Member"},
}]
COMMENTS = [
    {"commentID": 901, "discussionID": 501, "parentRecordType": "discussion", "name": "Re: Data Store validation fails on 11.3",
     "body": '<p>Restart the store, <a href="https://forum.example/home/leaving?target=https%3A%2F%2Fdocs.example%2Fds">see docs</a>. This fixed it for us.</p>',
     "dateInserted": "2025-03-01T12:00:00+00:00", "score": 3, "url": "https://forum.example/discussion/comment/901",
     "attributes": {"answer": {"status": "accepted"}},
     "insertUser": {"userID": 2, "name": "staffer", "label": " Esri Notable"}},
    {"commentID": 902, "discussionID": 501, "parentRecordType": "discussion", "name": "Re: Data Store validation fails on 11.3",
     "body": "<p>Same here.</p>", "dateInserted": "2025-03-02T10:00:00+00:00", "score": 1,
     "url": "https://forum.example/discussion/comment/902", "attributes": {"answer": {"status": "pending"}},
     "insertUser": {"userID": 3, "name": "mvp", "label": "MVP Regular"}},
]


class VanillaFetcher:
    def __init__(self):
        self.network_requests = 0
        self.calls = []

    def get_json(self, url, params=None, ttl=None):
        self.network_requests += 1
        self.calls.append((url, params))
        return DISCUSSIONS if url.endswith("/discussions") else COMMENTS

    def pause_host(self, url, seconds):
        pass


def crawl(store):
    pack = load_pack("esri_enterprise")
    src = pack.source("esri_community")
    src.options = {**src.options, "base_url": "https://forum.example",
                   "category_tiers": [{"tier": 1, "categories": [1]}]}
    fetcher, state = VanillaFetcher(), DictState()
    ctx = CrawlContext(source=src, fetcher=fetcher, state=state, lookup=store.get_document)
    tagger = VersionTagger(pack.versions)
    docs = []
    for d in Parser(ctx).crawl():
        index_document(store, tagger, d)
        docs.append(d)
    store.refresh_top_voted(src.id)
    return pack, docs, fetcher, state


def test_vanilla_thread():
    store = Store(":memory:", 16)
    pack, docs, fetcher, state = crawl(store)
    d, c1, c2 = docs
    assert d.doc_id == "esri_community:d:501" and d.kind == "question"
    assert "```\nERROR 9999: relational store not ready\n```" in d.content_md
    assert c1.parent_id == d.doc_id and c1.title == d.title and c1.kind == "answer"
    assert c1.is_accepted and c1.is_vendor_staff and not c1.extra["recognized_expert"]
    assert "[see docs](https://docs.example/ds)" in c1.content_md
    assert c2.extra["recognized_expert"] and not c2.is_vendor_staff
    assert store.get_version_tags(c1.doc_id) == ["11.3"]
    assert store.get_document(c1.doc_id)["is_top_voted"] == 1
    assert store.get_document(c2.doc_id)["is_top_voted"] == 0
    params = [p for _, p in fetcher.calls]
    assert params[0]["dateInserted"] == ">=1970-01-01T00:00:00Z" and params[0]["categoryID"] == 1
    assert state["cat:1:comments"]["complete"] is True


def test_vanilla_search_flags_staff():
    store = Store(":memory:", 16)
    pack, *_ = crawl(store)
    res = Searcher(pack, store=store, embedder=FakeEmbedder()).search("data store validation 11.3")
    top = res["results"][0]
    assert top["chunk_id"] == "esri_community:c:901#c0"
    assert {"accepted", "vendor_staff", "top_voted"} <= set(top["flags"])


def test_html_to_markdown():
    html = ('<p>A <em>b</em> <code>c</code><br>d</p><blockquote><p>q1</p><p>q2</p></blockquote>'
            '<ol><li>one</li><li>two</li></ol><script>x()</script>'
            '<span class="js-embed" data-embedjson="{&quot;url&quot;:&quot;https://e.x/p&quot;,&quot;name&quot;:&quot;Patch&quot;}"><a>ignored</a></span>')
    assert html_to_markdown(html) == "A *b* `c`  \nd\n\n> q1\n>\n> q2\n\n1. one\n2. two\n\n[Patch](https://e.x/p)"


def test_vanilla_skips_item_the_api_cannot_serialise():
    """A bad comment at t=1000s (with nothing readable before it) is found and stepped over."""
    from engine.crawl.fetcher import FetchError
    from engine.parsers.vanilla import _ts

    good = {"commentID": 950, "discussionID": 501, "parentRecordType": "discussion", "name": "Re: x",
            "body": "<p>after the bad one</p>", "dateInserted": "1970-01-01T00:20:00+00:00", "score": 0,
            "url": "u", "insertUser": {"userID": 9, "name": "n", "label": ""}}

    class Flaky(VanillaFetcher):
        def get_json(self, url, params=None, ttl=None):
            self.network_requests += 1
            self.calls.append((url, params))
            if url.endswith("/discussions"):
                return DISCUSSIONS
            since = _ts(params["dateInserted"][2:].replace("Z", "+00:00"))
            if since <= 1000:
                raise FetchError("HTTP 400", 400, '{"message": "Validation failed."}')
            return [good]

    pack = load_pack("esri_enterprise")
    src = pack.source("esri_community")
    src.options = {**src.options, "base_url": "https://forum.example",
                   "category_tiers": [{"tier": 1, "categories": [1]}]}
    f = Flaky()
    docs = list(Parser(CrawlContext(source=src, fetcher=f, state=DictState())).crawl())
    comment_calls = [p for u, p in f.calls if u.endswith("/comments")]
    assert [p["limit"] for p in comment_calls[:5]] == [500, 100, 20, 4, 1]
    assert comment_calls[-1]["dateInserted"] == ">=1970-01-01T00:16:41Z"  # resumes at 1001s
    assert len(comment_calls) < 40
    assert [d.doc_id for d in docs] == ["esri_community:d:501", "esri_community:c:950"]


def test_malformed_links_do_not_crash_conversion():
    out = html_to_markdown('<p>see <a href="http://[broken">here</a> and <img src="http://[x/y.png" alt="pic"></p>',
                           base_url="https://forum.example/f/")
    assert "here" in out and "http://[broken" not in out
