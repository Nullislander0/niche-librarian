"""vBulletin 5: board listings by last activity, thread pages, posts with author panels."""
from engine.config import SourceConfig
from engine.parsers.base import CrawlContext
from engine.parsers.vbulletin5 import Parser

BASE = "https://forum.example/"
BOARD = BASE + "forum/talk/open"


class DictState(dict):
    def set(self, k, v):
        self[k] = v


def topic(node, last, slug, sticky=False):
    return (f'<li class="topic-item{" sticky" if sticky else ""} js-topic-item" data-lastcontent="{last}" '
            f'data-node-id="{node}"><a href="{BOARD}/{node}-{slug}" class="topic-title">{slug}</a></li>')


def post(node, when, author, mid, body, first=False):
    return (f'<li data-node-id="{node}" data-node-publishdate="{when}" class="b-post js-post{" b-post--first" if first else ""}">'
            f'<div class="author h-text-size--14"> <strong><a href="{BASE}member/{mid}-{author.lower()}" >{author}</a></strong></div>'
            f'<div class="usertitle">{"Moderator" if author == "Mod" else "Member"}</div>'
            f'<li><label>Join Date:</label> <span>Feb 2006</span></li><li><label>Posts:</label> <span>1,126</span></li>'
            f'<div class="js-post__content-text restore h-wordwrap" itemprop="text">{body}</div></li>')


class Site:
    network_requests = 0

    def __init__(self):
        self.fetched = []

    def get_text(self, url, params=None, ttl=None):
        self.fetched.append(url)
        if url == BOARD:
            return (topic(1, 1600000000, "old-sticky", sticky=True) + topic(30, 1790000000, "branda-question")
                    + topic(20, 1700000000, "too-old"))
        if url.startswith(BOARD + "/page"):
            return ""
        if url == f"{BOARD}/30-branda-question":
            return ("<title>Branda question - Example Forums</title><h1>Announcement</h1>"
                    + post(30, 1789990000, "Ann", 5, "<p>Branda costs $59 cash?</p>", first=True)
                    + f'<a href="{BOARD}/30-branda-question/page2">2</a>')
        if url == f"{BOARD}/30-branda-question/page2":
            return post(31, 1790000000, "Mod", 6, "<p>Yes, through MailRx.</p>")
        raise AssertionError(url)


def test_board_since_thread_pages_and_posts():
    src = SourceConfig(id="dez", parser="vbulletin5", source_type="forum", options={
        "base_url": BASE, "since": "2026-01-01", "staff_title": "Moderator",
        "channels": [{"path": "forum/talk/open", "name": "Open"}]})
    site, state = Site(), DictState()
    docs = list(Parser(CrawlContext(source=src, fetcher=site, state=state)).crawl())
    assert [d.doc_id for d in docs] == ["dez:p:30", "dez:p:31"]
    q, r = docs
    assert q.kind == "question" and q.title == "Branda question" and q.author == "Ann" and q.author_reputation == 1126
    assert r.parent_id == "dez:p:30" and r.is_vendor_staff and "MailRx" in r.content_md and r.url.endswith("#post31")
    assert q.author_created is not None and q.extra["channel"] == "Open"
    assert not any("too-old" in u or "old-sticky" in u for u in site.fetched)
    # Unchanged since the last run: not re-read.
    site2 = Site()
    assert list(Parser(CrawlContext(source=src, fetcher=site2, state=state)).crawl()) == []
