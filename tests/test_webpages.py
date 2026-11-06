"""Single web pages (read / propose / approve) and the web_search quota guard."""
import time

from engine import webpages, websearch
from engine.config import load_pack
from engine.crawl.fetcher import BlockedError
from engine.index.store import Store
from engine.indexer import index_document
from engine.mcp_server.server import format_web_results
from engine.parsers.base import CrawlContext, Document
from engine.parsers.web_pages import Parser
from engine.versions import VersionTagger

PAGE = """<html><head><title>Migrating Smallworld to the Utility Network | Some Blog</title>
<meta property="article:published_time" content="2026-10-02T09:00:00Z">
<meta name="description" content="Notes from a migration."></head>
<body><nav><a href="/">Home</a> | <a href="/blog">Blog</a></nav>
<header><h1>Site header</h1></header>
<article><h1>Migrating Smallworld</h1><p>We used FME to map Smallworld electric objects to the
Utility Network asset package. """ + "Domain mapping took the longest. " * 20 + """</p>
<aside>Related posts</aside></article>
<footer>Copyright</footer></body></html>"""


class FakeFetcher:
    network_requests = 0

    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url, params=None, ttl=None, **kw):
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


def test_extract_page_keeps_article_drops_chrome():
    p = webpages.extract_page(PAGE, "https://blog.example.com/sw")
    assert p["title"].startswith("Migrating Smallworld to the Utility Network")
    assert "Utility Network asset package" in p["content_md"]
    for chrome in ("Home", "Site header", "Related posts", "Copyright"):
        assert chrome not in p["content_md"]
    assert time.gmtime(p["post_date"]).tm_year == 2026 and p["description"] == "Notes from a migration."


def test_classify_and_read_url():
    pack = load_pack("esri_enterprise")
    store = Store(":memory:", 16)
    store.upsert(Document(doc_id="gis_se:q:1", source="gis_se", source_type="forum", kind="question",
                          url="https://blog.example.com/known/", title="t", content_md="x"), [], ["x"])
    assert webpages.classify(pack, "https://blog.example.com/known", store) == ("indexed", "gis_se:q:1")
    assert webpages.classify(pack, "https://gis.stackexchange.com/questions/9/x") == ("source", "gis_se")
    assert webpages.classify(pack, "https://blog.example.com/sw") == ("ok", None)

    f = FakeFetcher({"https://blog.example.com/sw": PAGE,
                     "https://blocked.example.com/": BlockedError("robots.txt disallows it")})
    out = webpages.read_url(pack, "https://blog.example.com/sw", f, max_tokens=6000)
    assert "NOT in the index" in out and "Untrusted content" in out and "asset package" in out
    assert "robots.txt disallows" in webpages.read_url(pack, "https://blocked.example.com/", f)


def test_blocked_hosts_from_pack_config():
    import copy
    pack = copy.deepcopy(load_pack("homelab"))
    pack.web = {"blocked_hosts": ["drriders.com"]}
    assert webpages.classify(pack, "https://www.drriders.com/forum/thread/1") == ("blocked", "drriders.com")
    assert "blocked" in webpages.read_url(pack, "https://drriders.com/x", FakeFetcher({}))


def test_queue_then_approve_then_index(tmp_path, monkeypatch):
    monkeypatch.setattr(webpages, "DATA_DIR", tmp_path)
    pack = load_pack("esri_enterprise")
    url = "https://blog.example.com/sw"
    assert "Queued" in webpages.propose(pack, url, "migration notes")
    assert "Already waiting" in webpages.propose(pack, url)
    assert "crawled by source 'gis_se'" in webpages.propose(pack, "https://gis.stackexchange.com/questions/9/x")
    assert "Queued" in webpages.propose(pack, "https://other.example.com/a")
    assert webpages.reject(pack, ["https://other.example.com/a"]) == ["https://other.example.com/a"]
    assert "rejected earlier" in webpages.propose(pack, "https://other.example.com/a")

    assert webpages.approve(pack) == [url]
    store = Store(":memory:", 16)
    src = pack.source("web_pages")
    ctx = CrawlContext(source=src, fetcher=FakeFetcher({url: PAGE}), state=store.state_for(src.id))
    docs = list(Parser(ctx).crawl())
    assert [d.doc_id for d in docs] == [webpages.doc_id(url)] and docs[0].extra["note"] == "migration notes"
    index_document(store, VersionTagger(pack.versions), docs[0])
    assert list(Parser(ctx).crawl()) == []                 # indexed once, not refetched
    assert store.find_urls([url]) == {url: webpages.doc_id(url)}

    assert webpages.remove(pack, url, store)
    assert store.find_urls([url]) == {} and webpages.load(pack.id)["approved"] == {}


def test_web_results_are_annotated():
    pack = load_pack("esri_enterprise")
    store = Store(":memory:", 16)
    store.upsert(Document(doc_id="esri_community:t:5", source="esri_community", source_type="forum",
                          kind="question", url="https://community.esri.com/t5/x/td-p/5", title="t",
                          content_md="x"), [], ["x"])
    hits = [{"kind": "web", "url": u, "title": "T", "snippet": "s", "age": ""} for u in
            ("https://community.esri.com/t5/x/td-p/5", "https://community.esri.com/t5/y/td-p/6",
             "https://www.reddit.com/r/gis/comments/abc")]
    out = format_web_results("q", hits, pack, store)
    assert "[indexed: fetch esri_community:t:5#c0]" in out
    assert "[host crawled by source 'esri_community']" in out
    assert "shown, not stored" in out


def test_agent_budget_keeps_a_quarter_for_watches(tmp_path, monkeypatch):
    monkeypatch.setattr(websearch, "_usage_path", lambda: tmp_path / "usage.json")
    assert websearch.agent_budget()[0]                     # nothing known yet
    now = time.time()
    hdr = {"X-RateLimit-Limit": "1, 2000", "X-RateLimit-Remaining": "1, 501", "X-RateLimit-Reset": "1, 86400"}
    websearch.record_usage("brave", hdr, now)
    assert websearch.agent_budget(now=now)[0]              # 501 > 500 kept back
    websearch.record_usage("brave", {**hdr, "X-RateLimit-Remaining": "0, 500"}, now)
    ok, why = websearch.agent_budget(now=now)
    assert not ok and "500 kept for watches" in why
    assert websearch.agent_budget(now=now + 90000)[0]      # new month


def test_agent_urls_cannot_reach_private_or_local_addresses(monkeypatch):
    import socket
    from engine.crawl import fetcher as fm
    for url in ("http://127.0.0.1:11434/api/tags", "http://10.0.0.5/", "http://192.168.1.1/admin",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://localhost:8080/"):
        try:
            fm.check_public(url)
            assert False, url
        except fm.BlockedError as e:
            assert "private or local" in str(e)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(None, None, None, "", ("93.184.216.34", 0))])
    fm.check_public("https://example.com/page")                      # a public address passes
    # A public page that redirects onto the local network is refused too.
    handler = fm._PublicRedirects()
    try:
        handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:11434/")
        assert False
    except fm.BlockedError:
        pass
    # read_url relays the refusal instead of fetching.
    pack = load_pack("esri_enterprise")
    f = fm.Fetcher("ua", "/tmp/unused-cache", min_interval=0)
    assert "private or local" in webpages.read_url(pack, "http://192.168.1.1/", f)
