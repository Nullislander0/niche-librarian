"""XenForo 2 parser against a synthetic forum."""
from engine.config import load_pack
from engine.parsers.base import CrawlContext
from engine.parsers.xenforo2 import Parser, page_count, parse_posts, parse_thread_list

from test_pipeline import DictState


def listing(items, pages=1):
    nav = "".join(f'<li class="pageNav-page "><a href="/forums/g.1/page-{p}">{p}</a></li>' for p in range(1, pages + 1))
    rows = "".join(
        f'<div class="structItem structItem--thread js-threadListItem-{tid}" data-author="a">'
        f'{"<i class=\"structItem-status structItem-status--sticky\"></i>" if sticky else ""}'
        f'<div class="structItem-title" uix-href="/threads/t.{tid}/"><a href="/threads/t.{tid}/">Thread {tid}</a></div>'
        + "".join(f'<a href="/threads/t.{tid}/page-{p}">{p}</a>' for p in range(2, tpages + 1))
        + f'<time class="u-dt" data-timestamp="{last}">x</time></div>'
        for tid, last, sticky, tpages in items)
    return f"<html>{rows}<ul class='pageNav-main'>{nav}</ul></html>"


def post(pid, author, body, staff=False, title="Member", likes=0, ts=1790000000):
    name = f'<span class="username--staff username--moderator">{author}</span>' if staff else author
    return (f'<article class="message message--post js-post" data-author="{author}" data-content="post-{pid}" id="js-post-{pid}">'
            f'<div class="message-cell message-cell--user"><h4 class="message-name"><a href="/members/{author}.{pid}/" class="username">{name}</a></h4>'
            f'<h5 class="userTitle message-userTitle">{title}</h5>'
            f'<dl class="pairs"><dt><span title="Joined"><i></i></span></dt><dd>Apr 24, 2010</dd></dl>'
            f'<dl class="pairs"><dt><span title="Messages"><i></i></span></dt><dd>1,234</dd></dl></div>'
            f'<div class="message-cell message-cell--main"><time class="u-dt" datetime="x" data-timestamp="{ts}">d</time>'
            f'<article class="message-body"><div class="bbWrapper">{body}</div></article>'
            f'<meta itemprop="userInteractionCount" content="{likes}" /></div></article>')


def thread_page(title, pages, posts, solved=False):
    label = '<span class="label label--primary">[SOLVED]</span><span class="label-append">&nbsp;</span>' if solved else ""
    nav = "".join(f'<li class="pageNav-page "><a href="/threads/t.1/page-{p}">{p}</a></li>' for p in range(1, pages + 1)) if pages > 1 else ""
    return f'<h1 class="p-title-value">{label}{title}</h1>{nav}{"".join(posts)}'


def test_parse_listing_and_posts():
    lst = parse_thread_list(listing([(10, 1790000500, True, 13), (11, 1790000400, False, 1)], pages=3))
    assert [(t["id"], t["sticky"], t["pages"]) for t in lst] == [("10", True, 13), ("11", False, 1)]
    ps = parse_posts(thread_page("Q", 1, [post(1, "asker", "<p>Cluster lost quorum</p>"),
                                           post(2, "fabian", "<p>Check corosync</p>", staff=True,
                                                title="Proxmox Staff Member", likes=4)]))
    assert ps[0]["joined"] is not None and ps[0]["messages"] == 1234 and not ps[0]["staff"]
    assert ps[1]["staff"] and ps[1]["likes"] == 4 and ps[1]["date"] == 1790000000


class Site:
    def __init__(self, thread_pages=1):
        self.network_requests = 0
        self.paths = []
        self.thread_pages = thread_pages

    def get_text(self, url, params=None, ttl=None):
        self.network_requests += 1
        path = url.split("forum.example", 1)[1]
        self.paths.append(path)
        if path.startswith("/forums/g.1"):
            return listing([(1, 1790000900, False, self.thread_pages), (2, 1500000000, False, 1)])
        if path.startswith("/threads/t.1/"):
            p = int(path.rsplit("page-", 1)[1].strip("/")) if "page-" in path else 1
            posts = ([post(100, "asker", "<p>Quorum lost after update</p>")] if p == 1 else []) + \
                    [post(100 + p * 10, "fabian", f"<p>answer on page {p}</p>", staff=p == 1, title="Proxmox Staff Member")]
            return thread_page("Quorum failure", self.thread_pages, posts, solved=True)
        raise AssertionError(path)


def crawl(site, state, max_pages=10):
    src = load_pack("homelab").source("proxmox_forum")
    src.options = {**src.options, "base_url": "https://forum.example/", "since": "2025-01-01",
                   "forums": [{"path": "forums/g.1/"}], "max_pages_per_thread": max_pages}
    return list(Parser(CrawlContext(source=src, fetcher=site, state=state)).crawl())


def test_thread_documents_signals_and_floor():
    site = Site()
    docs = crawl(site, DictState())
    q, a = docs
    assert q.doc_id == "proxmox_forum:t:1" and q.kind == "question" and q.title == "Quorum failure"
    assert q.extra["solved_thread"] and a.parent_id == q.doc_id and a.is_vendor_staff
    assert a.url == "https://forum.example/threads/t.1/post-110"
    assert not any(p.startswith("/threads/t.2") for p in site.paths)        # older than `since`


def test_huge_thread_capped_and_unchanged_threads_skipped():
    site, state = Site(thread_pages=40), DictState()
    docs = crawl(site, state, max_pages=5)
    pages = sorted({d.extra["page"] for d in docs})
    assert pages == [1, 37, 38, 39, 40]                                        # page 1 + newest 4
    again = Site(thread_pages=40)
    assert crawl(again, state, max_pages=5) == [] and not any("/threads/" in p for p in again.paths)
