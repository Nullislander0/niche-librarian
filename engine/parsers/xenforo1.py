"""XenForo 1.x forums (server-rendered HTML; no API). One Document per post.

For forums without an API. The fetcher paces requests; keep the interval generous. Discovery
uses only public listing pages:

Options (sources.yaml):
  base_url: https://forum.example.com/f/
  tag_pages: [tags/dr650/]                    # tag listings -> thread URLs
  forum_scan:                                 # thread listings filtered by title
    - {forum: forums/thumpers.15/, title: '(?i)\\bdr\\s?-?650\\b', max_pages: 2000}
  threads: [threads/some-thread.123/]         # always crawl these
  mega_threads:                               # huge threads: newest pages first, in slices
    - {thread: threads/the-dr650-thread.135295/, pages_per_run: 2000}
  rescan_pages: 5                             # listing pages re-read on incremental runs
  post_count_labels: [Messages]               # some forums rename it (ADVrider: Oddometer)

Each post is a Document with author, date, likes (as score), the author's join date
(account age) and message count (as reputation). Ordinary threads get one synthetic root
document; each mega-thread page is treated as its own conversation, so `fetch(thread)`
returns that page's posts and one giant thread can't flood the results.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown
from engine.parsers.sitemap_html import extract_element

_THREAD_ID = re.compile(r"threads/[^/]*?\.(\d+)/")
_POST_START = re.compile(r'<li id="post-(\d+)" class="message[^"]*"[^>]*data-author="([^"]*)"', re.I)


def _thread_id(url: str) -> str:
    m = _THREAD_ID.search(url)
    return m.group(1) if m else url


def _ts(s: str | None) -> int | None:
    if not s:
        return None
    s = html.unescape(s).strip()
    for fmt in ("%b %d, %Y at %I:%M %p", "%b %d, %Y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _int(s: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def page_count(page: str) -> int:
    m = re.search(r"Page \d+ of ([\d,]+)", page)
    return _int(m.group(1)) if m else 1


def parse_posts(page: str, count_labels: tuple[str, ...] = ("Messages",)) -> list[dict]:
    """Posts on a XenForo 1 thread page. count_labels: the post-count field's name(s)."""
    starts = list(_POST_START.finditer(page))
    posts = []
    for i, m in enumerate(starts):
        seg = page[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        body = extract_element(seg, "blockquote", cls="messageText") or ""
        body = re.sub(r'<div class="messageTextEndMarker">.*', "", body, flags=re.S)
        body = re.sub(r'<a[^>]+class="AttributionLink"[^>]*>.*?</a>', "", body, flags=re.S)  # "↑" quote links
        t = re.search(r'class="DateTime"[^>]*data-time="(\d+)"', seg)
        when = int(t.group(1)) if t else _ts((re.search(r'class="DateTime" title="([^"]+)"', seg) or [None, None])[1])
        likes = 0
        lt = re.search(r'<span class="LikeText">(.*?)</span>', seg, re.S)
        if lt:
            likes = len(re.findall(r'class="username"', lt.group(1)))
            others = re.search(r"([\d,]+) others?", lt.group(1))
            likes += _int(others.group(1)) if others else 0
        info = dict(re.findall(r"<dt>\s*([^<:]+):\s*</dt>\s*<dd>\s*(?:<a[^>]*>)?\s*([^<]+?)\s*(?:</a>)?\s*</dd>", seg))
        member = re.search(r'href="members/[^"/]*?\.(\d+)/"', seg)
        title = re.search(r'class="userTitle"[^>]*>([^<]*)<', seg)
        posts.append({
            "id": m.group(1), "author": html.unescape(m.group(2)), "author_id": member.group(1) if member else None,
            "date": when, "likes": likes, "body_html": body,
            "joined": _ts(info.get("Joined")),
            "messages": next((_int(info[k]) for k in count_labels if info.get(k)), None),
            "user_title": html.unescape(title.group(1)).strip() if title else None,
        })
    return posts


class Parser:
    POSITION_KEY_PREFIXES = ("thread:", "mega:", "scan:")

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.base = o["base_url"].rstrip("/") + "/"
        self.tag_pages = o.get("tag_pages", [])
        self.scans = o.get("forum_scan", [])
        self.threads = o.get("threads", [])
        self.megas = o.get("mega_threads", [])
        self.rescan = int(o.get("rescan_pages", 5))
        self.count_labels = tuple(o.get("post_count_labels", ["Messages"]))

    def _get(self, path: str, ttl: int | None = None) -> str:
        self.ctx.check_budget()
        return self.ctx.fetcher.get_text(self.base + path.lstrip("/"), ttl=ttl)

    # -- crawl ------------------------------------------------------------

    def crawl(self) -> Iterator[Document]:
        mega_ids = {_thread_id(m["thread"]) for m in self.megas}
        for m in self.megas:
            yield from self._mega(m)
        found = list(dict.fromkeys(self.threads + self._discover()))
        self.ctx.log.info("%d threads to check", len(found))
        for t in found:
            if _thread_id(t) not in mega_ids:
                yield from self._thread(t)

    def _discover(self) -> list[str]:
        urls: list[str] = []
        for tag in self.tag_pages:
            page, n = 1, 1
            while page <= n:
                body = self._get(f"{tag.rstrip('/')}/page-{page}" if page > 1 else tag, ttl=86400)
                n = max(n, max([int(x) for x in re.findall(r"/page-(\d+)", body)] or [1]))
                urls += re.findall(r'href="(threads/[^"/]+\.\d+/)"', body)
                page += 1
        for scan in self.scans:
            urls += self._scan_forum(scan)
        return list(dict.fromkeys(urls))

    def _scan_forum(self, scan: dict) -> list[str]:
        """Thread listings, newest activity first; full scan once, then the first few pages."""
        key = f"scan:{scan['forum']}"
        st = self.ctx.state.get(key) or {"complete": False, "next": 1, "found": []}
        pat = re.compile(scan["title"])
        limit = int(scan.get("max_pages", 1000))
        found = set(st["found"])
        pages = range(1, self.rescan + 1) if st["complete"] else range(st["next"], limit + 1)
        last = limit
        for p in pages:
            body = self._get(f"{scan['forum'].rstrip('/')}/page-{p}" if p > 1 else scan["forum"], ttl=0 if st["complete"] else None)
            last = max([int(x) for x in re.findall(r"/page-(\d+)", body)] or [1])
            for url, title in re.findall(r'<h3 class="title">.*?<a href="(threads/[^"]+\.\d+/)"[^>]*>(.*?)</a>', body, re.S):
                if pat.search(html.unescape(re.sub(r"<[^>]+>", "", title))):
                    found.add(url)
            if not st["complete"]:
                st.update(next=p + 1, found=sorted(found))
                self.ctx.state.set(key, st)
            if p >= last:
                break
        st.update(complete=True, found=sorted(found))
        self.ctx.state.set(key, st)
        self.ctx.log.info("forum scan %s: %d matching threads", scan["forum"], len(found))
        return sorted(found)

    def _thread(self, url: str) -> Iterator[Document]:
        tid = _thread_id(url)
        key = f"thread:{tid}"
        st = self.ctx.state.get(key) or {"pages_done": 0}
        first = self._get(url, ttl=0 if st["pages_done"] else None)
        n = page_count(first)
        title = self._title(first)
        root = f"{self.ctx.source.id}:t:{tid}"
        yield self._root(root, title, self.base + url, n)
        for p in range(max(1, st["pages_done"]), n + 1):  # re-read the last page: it may have grown
            body = first if p == 1 else self._get(f"{url}page-{p}", ttl=0 if p >= st["pages_done"] else None)
            yield from self._posts(body, root, title, f"{self.base}{url}page-{p}" if p > 1 else self.base + url, p)
            st["pages_done"] = p
            self.ctx.state.set(key, st)

    def _mega(self, m: dict) -> Iterator[Document]:
        url, per_run = m["thread"], int(m.get("pages_per_run", 1000))
        tid = _thread_id(url)
        key = f"mega:{tid}"
        st = self.ctx.state.get(key) or {"top": 0, "next_down": None}
        first = self._get(url, ttl=0)
        n, title = page_count(first), self._title(first)
        todo = list(range(max(1, st["top"]), n + 1)) if st["top"] else []  # new pages since last run
        down = st["next_down"] if st["next_down"] is not None else n
        st["top"] = st["top"] or n
        done = 0
        for p in todo + list(range(down, 0, -1)):
            if done >= per_run:
                break
            body = first if p == 1 else self._get(f"{url}page-{p}", ttl=0 if p >= st["top"] else None)
            root = f"{self.ctx.source.id}:t:{tid}:p{p}"
            yield self._root(root, f"{title} (page {p} of {n})", f"{self.base}{url}page-{p}", 1)
            yield from self._posts(body, root, title, f"{self.base}{url}page-{p}", p)
            done += 1
            if p in todo:
                st["top"] = max(st["top"], p)
            else:
                st["next_down"] = p - 1
            self.ctx.state.set(key, st)
        self.ctx.log.info("mega-thread %s: %d pages this run, oldest done %s of %d",
                          tid, done, (st["next_down"] or 0) + 1, n)

    # -- normalise --------------------------------------------------------

    @staticmethod
    def _title(page: str) -> str:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S) or re.search(r"<title>(.*?)(?:\|[^<]*)?</title>", page, re.S)
        return html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip() if m else ""

    def _root(self, doc_id: str, title: str, url: str, pages: int) -> Document:
        return Document(doc_id=doc_id, source=self.ctx.source.id, source_type=self.ctx.source.source_type,
                        kind="thread", url=url, title=title, content_md=f"Thread: {title} ({pages} pages)",
                        extra={"pages": pages})

    def _posts(self, page: str, root: str, title: str, page_url: str, page_no: int) -> Iterator[Document]:
        for p in parse_posts(page, self.count_labels):
            yield Document(
                doc_id=f"{self.ctx.source.id}:p:{p['id']}", source=self.ctx.source.id,
                source_type=self.ctx.source.source_type, kind="post",
                url=f"{page_url}#post-{p['id']}", title=title,
                content_md=html_to_markdown(p["body_html"], base_url=self.base),
                post_date=p["date"], updated_date=p["date"], author=p["author"], author_id=p["author_id"],
                author_reputation=p["messages"], author_created=p["joined"], score=p["likes"],
                parent_id=root, extra={"page": page_no, "author_label": p["user_title"]},
            )


__all__ = ["Parser", "parse_posts", "page_count", "FetchError"]
