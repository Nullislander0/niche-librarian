"""Discourse and MediaWiki parsers against synthetic APIs; access-verdict enforcement."""
import json

from engine.config import load_pack
from engine.parsers.base import CrawlContext
from engine.parsers.discourse import Parser as Discourse
from engine.parsers.mediawiki import Parser as MediaWiki

from test_pipeline import DictState


def post(pid, n, user, html, **kw):
    return {"id": pid, "post_number": n, "username": user, "user_id": pid, "cooked": html, "post_type": 1,
            "created_at": "2025-01-%02dT10:00:00Z" % min(n, 28), "trust_level": kw.get("tl", 1),
            "accepted_answer": kw.get("accepted", False), "staff": kw.get("staff", False),
            "primary_group_name": kw.get("group"), "actions_summary": [{"id": 2, "count": kw.get("likes", 0)}]}


class DiscourseSite:
    def __init__(self, topic_posts=3):
        self.network_requests = 0
        self.paths = []
        self.n = topic_posts

    def get_json(self, url, params=None, ttl=None):
        self.network_requests += 1
        path = url.split("example.org", 1)[1]
        self.paths.append(path)
        if path.startswith("/categories.json"):
            return {"category_list": {"categories": [{"id": 4, "slug": "general", "subcategory_list": []}]}}
        if path.startswith("/c/general/4/l/latest.json"):
            page = int(path.rsplit("page=", 1)[1])
            topics = [{"id": 100, "bumped_at": "2025-01-03T00:00:00Z", "highest_post_number": self.n},
                      {"id": 99, "bumped_at": "2020-01-01T00:00:00Z", "highest_post_number": 2}] if page == 0 else []
            return {"topic_list": {"topics": topics, "more_topics_url": "/next" if page == 0 else None}}
        if path == "/t/100.json":
            posts = [post(1, 1, "asker", "<p>Pool degraded after TrueNAS SCALE 24.10 upgrade</p>"),
                     post(2, 2, "ixstaff", "<p>Replace the disk, then <code>zpool replace</code>.</p>",
                          accepted=True, group="TrueNAS_Staff", likes=5)]
            return {"title": "Pool degraded", "slug": "pool-degraded", "tags": [{"name": "zfs"}], "category_id": 4,
                    "post_stream": {"posts": posts, "stream": [1, 2, 3][: self.n]}}
        if path.startswith("/t/100/posts.json"):
            assert "post_ids[]=3" in path
            return {"post_stream": {"posts": [post(3, 3, "leader", "<p>Same here.</p>", tl=4)]}}
        raise AssertionError(path)


def discourse_ctx(site, state):
    src = load_pack("homelab").source("truenas_forums")
    src.options = {"base_url": "https://forum.example.org", "since": "2024-01-01",
                   "vendor_groups": "TrueNAS_Staff", "categories": [{"id": 4}]}
    return CrawlContext(source=src, fetcher=site, state=state)


def test_discourse_topic_posts_and_signals():
    site, state = DiscourseSite(), DictState()
    docs = list(Discourse(discourse_ctx(site, state)).crawl())
    q, a, r = docs
    assert q.doc_id == "truenas_forums:t:100" and q.kind == "question" and q.tags == ["zfs"]
    assert a.kind == "answer" and a.is_accepted and a.is_vendor_staff and a.score == 5 and a.parent_id == q.doc_id
    assert "`zpool replace`" in a.content_md
    assert r.extra["recognized_expert"] and not r.is_vendor_staff
    assert not any(p.startswith("/t/99") for p in site.paths)       # older than `since`: not fetched
    assert state["cat:4"]["watermark"] > 0


def test_discourse_skips_unchanged_topics_next_run():
    state = DictState()
    list(Discourse(discourse_ctx(DiscourseSite(), state)).crawl())
    site = DiscourseSite()
    assert list(Discourse(discourse_ctx(site, state)).crawl()) == []
    assert "/t/100.json" not in site.paths


class Wiki:
    network_requests = 0

    def __init__(self, rev=7):
        self.rev = rev
        self.parsed = []

    def get_json(self, url, params=None, ttl=None):
        if params["action"] == "query":
            pages = [{"pageid": 1, "title": "ZFS", "lastrevid": self.rev, "touched": "2026-10-01T00:00:00Z"},
                     {"pageid": 2, "title": "ZFS (Español)", "lastrevid": 3, "touched": "2026-10-01T00:00:00Z"}]
            return {"query": {"pages": pages}}
        self.parsed.append(params["pageid"])
        html = ('<div class="mw-heading"><h2>Scrub</h2><span class="mw-editsection"><span>[</span>'
                '<a href="/edit">edit</a><span>]</span></span></div><p>Run <code>zpool scrub</code> monthly.</p>'
                '<div id="toc" class="toc"><div>Contents</div></div>')
        return {"parse": {"title": "ZFS", "displaytitle": "<span>ZFS</span>", "revid": self.rev, "text": html}}


def test_mediawiki_pages_cleaned_translations_skipped_and_incremental():
    src = load_pack("homelab").source("arch_wiki")
    state = DictState()
    wiki = Wiki()
    docs = list(MediaWiki(CrawlContext(source=src, fetcher=wiki, state=state)).crawl())
    assert [d.title for d in docs] == ["ZFS"] and wiki.parsed == [1]   # "(Español)" excluded
    d = docs[0]
    assert d.url == "https://wiki.archlinux.org/title/ZFS"
    assert "`zpool scrub`" in d.content_md and "edit" not in d.content_md and "Contents" not in d.content_md
    again = Wiki()
    assert list(MediaWiki(CrawlContext(source=src, fetcher=again, state=state)).crawl()) == [] and again.parsed == []
    changed = Wiki(rev=8)
    assert len(list(MediaWiki(CrawlContext(source=src, fetcher=changed, state=state)).crawl())) == 1


def test_blocked_access_verdict_is_never_crawled(tmp_path, monkeypatch):
    from engine import config, indexer
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    pack = load_pack("homelab")
    for s in pack.sources:
        s.enabled = s.id == "linuxserver"
    pack.source("linuxserver").access["verdict"] = "blocked"
    monkeypatch.setattr(type(pack), "db_path", property(lambda self: tmp_path / "t.db"))
    report = indexer.crawl(pack, embed=False)
    assert report["linuxserver"]["status"].startswith("skipped: access verdict 'blocked'")


def test_discourse_huge_topic_keeps_opening_post_and_newest_replies():
    class Big(DiscourseSite):
        def get_json(self, url, params=None, ttl=None):
            path = url.split("example.org", 1)[1]
            if path == "/t/100.json":
                self.paths.append(path)
                return {"title": "Mega", "slug": "mega", "post_stream": {
                    "posts": [post(i, i, f"u{i}", f"<p>p{i}</p>") for i in range(1, 21)],
                    "stream": list(range(1, 1001))}}
            if path.startswith("/t/100/posts.json"):
                self.paths.append(path)
                got = [int(x) for x in path.split("post_ids[]=")[1:] for x in [x.split("&")[0]]]
                return {"post_stream": {"posts": [post(i, i, f"u{i}", f"<p>p{i}</p>") for i in got]}}
            return super().get_json(url, params, ttl)

    src = load_pack("homelab").source("truenas_forums")
    src.options = {"base_url": "https://forum.example.org", "since": "2024-01-01", "categories": [{"id": 4}],
                   "max_posts_per_topic": 50}
    site = Big()
    docs = list(Discourse(CrawlContext(source=src, fetcher=site, state=DictState())).crawl())
    nums = [int(d.content_md[1:]) for d in docs]
    assert len(docs) == 50 and nums[0] == 1 and nums[1] == 952 and nums[-1] == 1000
    assert docs[0].extra["truncated_to"] == 50
    assert sum(p.startswith("/t/100/posts.json") for p in site.paths) == 3   # 49 newest replies, 20 per request
