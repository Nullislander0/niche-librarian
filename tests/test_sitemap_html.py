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
