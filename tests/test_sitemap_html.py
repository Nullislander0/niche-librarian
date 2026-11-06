"""Sitemap-driven docs parser against a synthetic site."""
from engine.config import load_pack
from engine.index.chunk import chunk_markdown
from engine.index.store import Store
from engine.indexer import index_document
from engine.parsers.base import CrawlContext
from engine.parsers.sitemap_html import Parser, extract_element, meta_tags
from engine.search import Searcher
from engine.versions import VersionTagger

from test_pipeline import DictState, FakeEmbedder

PAGE = """<html><head><title>Configure HTTPS | Product documentation</title>
<meta name="version" content="12.1"><meta name="last-modified" content="2026-10-12">
<meta name="description" content="How to configure HTTPS."></head><body>
<nav><a href="/x">nav junk</a></nav>
<article id="main"><h1>Configure HTTPS</h1><div class="note"><p>Use a <a href="../certs/import.html">CA-signed certificate</a>.</p></div>
<ol><li><p>Open the Portal Administrator Directory.</p></li><li><p>Import the certificate.</p></li></ol></article>
<footer>footer junk</footer></body></html>"""

SITEMAP_INDEX = "<sitemapindex><sitemap><loc>https://docs.example/en/p/sitemap.xml</loc></sitemap></sitemapindex>"
SITEMAP = ("<urlset><url><loc>https://docs.example/en/p/admin/https.html</loc></url>"
           "<url><loc>https://docs.example/en/other/skip.html</loc></url></urlset>")


class SiteFetcher:
    network_requests = 0

    def get_text(self, url, params=None, ttl=None):
        self.network_requests += 1
        return {"https://docs.example/sitemap.xml": SITEMAP_INDEX,
                "https://docs.example/en/p/sitemap.xml": SITEMAP}.get(url, PAGE)


def test_extract_and_meta():
    assert "nav junk" not in extract_element(PAGE, "article", id="main")
    assert extract_element("<div id=a><div>x</div>y</div>z", "div", id="a") == "<div>x</div>y"
    assert meta_tags(PAGE)["version"] == "12.1"


def test_docs_page_indexed_as_official():
    pack = load_pack("esri_enterprise")
    src = pack.source("esri_docs")
    src.options = {**src.options, "sitemaps": ["https://docs.example/sitemap.xml"],
                   "include": ["^https://docs\\.example/en/p/"]}
    store = Store(":memory:", 16)
    state = DictState()
    docs = list(Parser(CrawlContext(source=src, fetcher=SiteFetcher(), state=state)).crawl())
    assert len(docs) == 1
    d = docs[0]
    assert d.title == "Configure HTTPS" and d.extra["versions"] == ["12.1"]
    assert "[CA-signed certificate](https://docs.example/en/p/certs/import.html)" in d.content_md
    assert "1. Open the Portal Administrator Directory.\n2. Import the certificate." in d.content_md
    assert "footer" not in d.content_md
    index_document(store, VersionTagger(pack.versions), d)
    assert store.get_version_tags(d.doc_id) == ["12.1"]
    res = Searcher(pack, store=store, embedder=FakeEmbedder()).search("import certificate https", explain=True)
    assert res["results"][0]["flags"][0] == "official"
    assert res["results"][0]["explain"]["authoritative"] == 1.0
    # second run within refresh_days fetches nothing new
    assert list(Parser(CrawlContext(source=src, fetcher=SiteFetcher(), state=state)).crawl()) == []


def test_heading_trail_prefix_skips_admonitions():
    md = "# Upgrade\n\n" + "intro " * 150 + "\n\n## Back up\n\n##### Note:\n\n" + "text " * 300 + "\n\n" + "more " * 300
    chunks = chunk_markdown(md, target=600, hard_max=1200)
    assert any(c.startswith("[Upgrade > Back up]") for c in chunks)
    assert not any("Note:]" in c for c in chunks)


def test_meta_filter_and_us_dates():
    from engine.parsers.sitemap_html import _date
    assert _date("10/03/2025") == _date("2025-10-03")
    pack = load_pack("esri_enterprise")
    src = pack.source("esri_docs")
    src.options = {**src.options, "sitemaps": ["https://docs.example/sitemap.xml"],
                   "include": ["^https://docs\\.example/en/p/"], "meta_filter": {"version": "^11\\."}}
    p = Parser(CrawlContext(source=src, fetcher=SiteFetcher(), state=DictState()))
    assert list(p.crawl()) == [] and p.skipped == 1  # page is 12.1, filter wants 11.x


BUG_PAGE = """<html><head><title>BUG-000170001 for Portal for ArcGIS</title>
<meta name="product" content="Portal for ArcGIS"><meta name="version" content="11.3">
<meta name="status" content="Fixed"><meta name="last-modified" content="08/07/2025"></head><body>
<h1>Federation fails when the server URL has a trailing slash.</h1>
<div class="item leftcontent"><table><tr><td>Version found</td><td>11.3</td></tr>
<tr><td>Version Fixed</td><td>11.5</td></tr><tr><td>Status</td><td>Fixed</td></tr></table>
<h2>Workaround</h2><p>Remove the trailing slash.</p></div></body></html>"""


def test_bug_page_fields_and_newest_first():
    pack = load_pack("esri_enterprise")
    src = pack.source("esri_bugs")

    class BugFetcher:
        network_requests = 0
        fetched = []

        def get_text(self, url, params=None, ttl=None):
            if url.endswith(".xml"):
                return ("<urlset>" + "".join(
                    f"<url><loc>https://support.esri.com/en-us/bug/{s}</loc></url>" for s in (
                        "portal-federation-fails-bug-000170001", "portal-other-thing-bug-000180002",
                        "pro-crashes-on-start-bug-000180003", "portal-old-thing-bug-000110004")) + "</urlset>")
            self.fetched.append(url)
            return BUG_PAGE

    src.options = {**src.options, "sitemaps": ["https://support.esri.com/x.xml"]}
    f = BugFetcher()
    docs = list(Parser(CrawlContext(source=src, fetcher=f, state=DictState())).crawl())
    assert [u.rsplit("-", 1)[-1] for u in f.fetched] == ["000180002", "000170001"]  # filtered, newest first
    d = docs[0]
    assert d.title == "BUG-000170001: Federation fails when the server URL has a trailing slash."
    assert d.extra["details"] == {"product": "Portal for ArcGIS", "status": "Fixed", "version": "11.3",
                                  "version_fixed": "11.5"}
    assert d.extra["versions"] == ["11.3", "11.5"]


def test_removed_pages_are_skipped_not_fatal():
    from engine.crawl.fetcher import FetchError
    pack = load_pack("esri_enterprise")
    src = pack.source("esri_docs")
    src.options = {**src.options, "sitemaps": ["https://docs.example/sitemap.xml"], "include": ["^https://docs\\.example/"]}

    class Gone(SiteFetcher):
        def get_text(self, url, params=None, ttl=None):
            if url.endswith("skip.html"):
                raise FetchError("HTTP 404", 404)
            return super().get_text(url, params, ttl)

    docs = list(Parser(CrawlContext(source=src, fetcher=Gone(), state=DictState())).crawl())
    assert [d.url for d in docs] == ["https://docs.example/en/p/admin/https.html"]


def test_fetcher_retries_timeouts(tmp_path, monkeypatch):
    import urllib.request
    from engine.crawl import fetcher as fm
    calls = []

    class Resp:
        headers = {"Content-Type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok": 1}'

    def flaky(req, timeout=0):
        if req.full_url.endswith("/robots.txt"):
            raise urllib.error.HTTPError(req.full_url, 404, "none", {}, None)
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr(fm.time, "sleep", lambda s: None)
    f = fm.Fetcher("ua", tmp_path, min_interval=0, respect_robots=False)
    assert f.get_json("https://x.example/a") == {"ok": 1} and len(calls) == 2


def test_ignoring_robots_still_honours_crawl_delay(tmp_path, monkeypatch):
    import urllib.request
    from engine.crawl import fetcher as fm

    class Resp:
        def __init__(self, body): self.body, self.headers = body, {"Content-Type": "text/plain"}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.body

    robots = b"User-agent: *\nCrawl-delay: 7\nDisallow: /\n"
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: Resp(robots if req.full_url.endswith("robots.txt") else b"page"))
    obey = fm.Fetcher("ua", tmp_path / "a", min_interval=0, respect_robots=True)
    try:
        obey.get_text("https://x.example/p")
        assert False, "should have been refused"
    except fm.BlockedError as e:
        assert "robots.txt disallows" in str(e)
    ignore = fm.Fetcher("ua", tmp_path / "b", min_interval=0)          # the default
    assert ignore.get_text("https://x.example/p") == "page"
    assert ignore._host_interval["x.example"] == 7.0          # pacing kept
    try:                                                      # EU/EEA hosts: Disallow enforced
        ignore.get_text("https://forum.example.de/p")
        assert False, "should have been refused"
    except fm.BlockedError as e:
        assert "EU/EEA host" in str(e)
    assert fm.Fetcher("ua", tmp_path / "c", min_interval=0, robots_eu=False).get_text("https://forum.example.de/p") == "page"
    assert fm.is_eu_host("www.example.co.uk") is False and fm.is_eu_host("example.fr:443")


def test_cache_ignores_and_never_stores_api_keys(tmp_path):
    from engine.crawl import fetcher as fm
    f = fm.Fetcher("ua", tmp_path, min_interval=0)
    f._cache_write("https://api.example.com/q?site=gis&key=OLDKEY123&page=2", "body")
    assert f._cache_read("https://api.example.com/q?site=gis&key=NEWKEY456&page=2", 10**9) == "body"
    assert "OLDKEY123" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert fm.cache_key("https://x.example/a?b=1") == "https://x.example/a?b=1"      # untouched without keys


def test_index_pages_followed_several_hops_within_the_pattern():
    from engine.config import SourceConfig
    from engine.parsers.base import CrawlContext
    from engine.parsers.sitemap_html import Parser
    site = {"https://d.example/v1/a.htm": '<a href="b.htm">b</a><a href="/v2/x.htm">other version</a>',
            "https://d.example/v1/b.htm": '<a href="c.htm">c</a><a href="a.htm">a</a>',
            "https://d.example/v1/c.htm": '<a href="d.htm">d</a>'}

    class F:
        network_requests = 0
        def get_text(self, url, params=None, ttl=None, **kw):
            return site.get(url, "<main><h1>T</h1><p>body text here for the page</p></main>")
    src = SourceConfig(id="d", parser="sitemap_html", source_type="docs", options={
        "index_pages": ["https://d.example/v1/a.htm"], "index_link_pattern": r"/v1/[\w-]+\.htm$",
        "follow_depth": 3, "max_pages": 10, "content": {"tag": "main"}})
    urls = Parser(CrawlContext(source=src, fetcher=F(), state={}))._urls()
    assert urls == ["https://d.example/v1/a.htm", "https://d.example/v1/b.htm", "https://d.example/v1/c.htm",
                    "https://d.example/v1/d.htm"]                       # never the other version
