"""inSided/Gainsight parser against a synthetic community."""
import html
import json

from engine.config import SourceConfig
from engine.parsers.base import CrawlContext
from engine.parsers.insided import Parser, topic_props

from test_pipeline import DictState

SITEMAP = ("<sitemapindex><sitemap><loc>https://c.example/sitemap-structure.xml</loc></sitemap>"
           "<sitemap><loc>https://c.example/sitemap-topics-1.xml</loc></sitemap></sitemapindex>")
TOPICS = "".join(f"<url><loc>https://c.example/{p}</loc></url>" for p in [
    "data-7/smallworld-to-utility-network-migration-500", "data-7/writing-to-arcgis-portal-feature-service-420",
    "transformers-9/string-concatenation-tips-430", "ideas/arcgis-pro-reader-idea-440"])


def page(topic, replies, authors):
    props = {"topic": topic, "authors": authors, "currentPage": 1,
             "repliesProps": {"replies": replies, "totalPages": 1}}
    return f'<div data-props="{html.escape(json.dumps(props))}"></div>'


class Site:
    network_requests = 0

    def __init__(self):
        self.fetched = []

    def get_text(self, url, params=None, ttl=None):
        self.fetched.append(url)
        if url.endswith("sitemap.xml"):
            return SITEMAP
        if url.endswith("sitemap-topics-1.xml"):
            return TOPICS
        if url.endswith("structure.xml"):
            raise AssertionError("structure sitemap should be skipped")
        tid = url.rsplit("-", 1)[1]
        return page({"id": tid, "publicId": tid, "title": f"Topic {tid}", "content": "<p>How do I migrate?</p>",
                     "publishedAt": "2025-03-01T10:00:00+00:00", "authorId": "1", "likesCount": "2",
                     "tags": [{"name": "Smallworld"}]},
                    [{"id": "9", "publicReplyId": f"{tid}1", "content": "<p>Use the Smallworld reader, then the UN writer.</p>",
                      "repliedAt": "2025-03-02T10:00:00+00:00", "authorId": "2", "isBestAnswer": True, "likedBy": [1, 2, 3]}],
                    {"1": {"name": "asker", "userTitle": "Participant"},
                     "2": {"name": "saferdan", "userTitle": "Safer", "rank": {"name": "Safer"}, "likesReceived": 900}})


def test_topics_filtered_newest_first_and_parsed():
    src = SourceConfig(id="fme_community", parser="insided", source_type="forum", options={
        "base_url": "https://c.example", "sitemap": "https://c.example/sitemap.xml",
        "include_slugs": ["esri|arcgis|portal|feature-?service", "smallworld", "utility-?network", "migrat"],
        "exclude_categories": ["ideas"], "staff_titles": "Safer|Moderator", "expert_titles": "Influencer"})
    site = Site()
    docs = list(Parser(CrawlContext(source=src, fetcher=site, state=DictState())).crawl())
    topics = [u for u in site.fetched if "sitemap" not in u]
    assert topics == ["https://c.example/data-7/smallworld-to-utility-network-migration-500",
                      "https://c.example/data-7/writing-to-arcgis-portal-feature-service-420"]   # filtered, ideas excluded
    q, a = docs[0], docs[1]
    assert q.doc_id == "fme_community:t:500" and q.kind == "question" and q.tags == ["Smallworld"] and q.score == 2
    assert a.kind == "answer" and a.is_accepted and a.is_vendor_staff and a.score == 3
    assert a.author_reputation == 900 and a.url.endswith("#post5001") and "Smallworld reader" in a.content_md


def test_topic_props_ignores_other_widgets():
    assert topic_props('<div data-props="{&quot;config&quot;:1}"></div>') is None
