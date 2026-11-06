"""XenForo 2.x forums (server-rendered HTML; no public API). One Document per post.

Options (sources.yaml):
  base_url: https://forum.example.com/            # forum root (with index.php? for non-friendly URLs)
  forums: [{path: forums/general.16/, name: General}]   # paths relative to base_url
  since: "2024-01-01"                             # first run: threads active since then
  revisit_days: 14
  max_threads_per_run: 2000
  max_pages_per_thread: 10                        # huge threads: page 1 plus the newest pages
  staff_title: 'Staff Member'                     # user title regex -> is_vendor_staff (plus the
                                                  #   built-in username--staff marker)
  expert_title: 'Famous Member|Distinguished'     # user title regex -> recognized expert
  post_count_labels: [Messages]

Each thread's first post becomes its root ("question"); the rest are replies. Join date,
message count and likes come from the post's user panel and microdata. A "[SOLVED]"-style
title label is kept as extra.solved and stripped from the title. The fetcher paces requests;
keep the interval generous.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator

from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown
from engine.parsers.sitemap_html import extract_element

_POST = re.compile(r'<article class="message[^"]*message--post[^"]*"[^>]*data-author="([^"]*)"[^>]*'
                   r'data-content="post-(\d+)"', re.S)
_ITEM = re.compile(r'js-threadListItem-(\d+)"', re.S)


def _int(s: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def _date(s: str | None) -> int | None:
    s = (s or "").strip()
    for fmt in ("%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>|\s+", " ", fragment)).strip()


def page_count(page: str) -> int:
    nums = [int(n) for n in re.findall(r'class="pageNav-page[^"]*"[^>]*>\s*<a[^>]*>\s*(\d+)\s*<', page)]
    return max(nums) if nums else 1


def parse_thread_list(page: str) -> list[dict]:
    """Threads on a forum listing page: id, href, title, sticky, last activity, pages."""
    starts = [m for m in _ITEM.finditer(page)]
    out = []
    for i, m in enumerate(starts):
        seg = page[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        title = re.search(r'<div class="structItem-title"[^>]*>(.*?)</div>', seg, re.S)
        href = re.search(r'href="([^"]*threads/[^"]+?\.\d+/)"', title.group(1) if title else seg)
        stamps = [int(x) for x in re.findall(r'data-timestamp="(\d+)"', seg)]
        jumps = [int(x) for x in re.findall(r'threads/[^"]+?\.\d+/page-(\d+)"', seg)]
        out.append({"id": m.group(1), "href": html.unescape(href.group(1)) if href else None,
                    "title": _text(title.group(1)) if title else "",
                    "sticky": "structItem-status--sticky" in seg,
                    "last": max(stamps) if stamps else None, "pages": max(jumps) if jumps else 1})
    return [t for t in out if t["href"]]


def parse_posts(page: str, count_labels: tuple[str, ...] = ("Messages",)) -> list[dict]:
    starts = list(_POST.finditer(page))
    posts = []
    for i, m in enumerate(starts):
        seg = page[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        body = extract_element(seg, "div", cls="bbWrapper") or ""
        ts = re.search(r'<time[^>]+class="u-dt"[^>]*data-timestamp="(\d+)"', seg)
        info = {}
        for dt, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", seg, re.S):
            label = re.search(r'title="([^"]+)"', dt)          # icon with a tooltip, or plain text
            info[(label.group(1) if label else _text(dt)).rstrip(":")] = dd
        title = re.search(r'class="userTitle[^"]*"[^>]*>(.*?)</', seg, re.S)
        likes = re.search(r'itemprop="userInteractionCount"\s+content="(\d+)"', seg)
        member = re.search(r'href="[^"]*members/[^"/]*?\.(\d+)/"', seg)
        posts.append({
            "id": m.group(2), "author": html.unescape(m.group(1)), "author_id": member.group(1) if member else None,
            "date": int(ts.group(1)) if ts else None, "body_html": body,
            "joined": _date(_text(info.get("Joined", ""))),
            "messages": next((_int(_text(info[k])) for k in count_labels if info.get(k)), None),
            "likes": int(likes.group(1)) if likes else 0,
            "user_title": _text(title.group(1)) if title else None,
            "staff": "username--staff" in seg[:seg.find('class="message-cell message-cell--main"')]
            if 'message-cell--main' in seg else "username--staff" in seg,
        })
    return posts


class Parser:
    POSITION_KEY_PREFIXES = ("forum:", "thread:")

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.base = o["base_url"]
        self.forums = o.get("forums", [])
        since = str(o.get("since", "2000-01-01"))
        self.since = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp())
        self.revisit = int(o.get("revisit_days", 14)) * 86400
        self.max_threads = int(o.get("max_threads_per_run", 2000))
        self.max_pages = int(o.get("max_pages_per_thread", 10))
        self.staff_title = re.compile(o["staff_title"], re.I) if o.get("staff_title") else None
        self.expert_title = re.compile(o["expert_title"], re.I) if o.get("expert_title") else None
        self.count_labels = tuple(o.get("post_count_labels", ["Messages"]))
        self.done = 0

    def _url(self, path: str) -> str:
        return urllib.parse.urljoin(self.base, path)

    def _get(self, path: str, ttl: int | None = 0) -> str:
        self.ctx.check_budget()
        return self.ctx.fetcher.get_text(self._url(path), ttl=ttl)

    def crawl(self) -> Iterator[Document]:
        for f in self.forums:
            path = f["path"] if isinstance(f, dict) else f
            self.ctx.log.info("forum %s", path)
            yield from self._forum(path)
            if self.done >= self.max_threads:
                self.ctx.log.info("max_threads_per_run (%d) reached", self.max_threads)
                return

    def _forum(self, path: str) -> Iterator[Document]:
        key = f"forum:{path}"
        st = self.ctx.state.get(key) or {"watermark": 0}
        floor = st["watermark"] - self.revisit if st["watermark"] else self.since
        newest, page = st["watermark"], 1
        while True:
            body = self._get(path if page == 1 else f"{path.rstrip('/')}/page-{page}")
            threads = parse_thread_list(body)
            stop = not threads
            for t in threads:
                last = t["last"] or 0
                if last < floor:
                    if t["sticky"]:
                        continue
                    stop = True
                    break
                tkey = f"thread:{t['id']}"
                seen = self.ctx.state.get(tkey) or {}
                if seen.get("last") != last:
                    yield from self._thread(t, seen)
                    self.ctx.state.set(tkey, {"last": last, "pages": t["pages"]})
                    self.done += 1
                newest = max(newest, last)
                if self.done >= self.max_threads:
                    return
            if stop or page >= page_count(body):
                break
            page += 1
        st["watermark"] = newest
        self.ctx.state.set(key, st)

    def _thread(self, t: dict, seen: dict) -> Iterator[Document]:
        first = self._get(t["href"], ttl=None if not seen else 0)
        n = max(page_count(first), t["pages"])
        h1 = re.search(r'<h1 class="p-title-value"[^>]*>(.*?)</h1>', first, re.S)
        labels = re.findall(r'<span class="label[^"]*"[^>]*>(.*?)</span>', h1.group(1)) if h1 else []
        title = _text(re.sub(r'<span class="label[^"]*"[^>]*>.*?</span>', "", h1.group(1))) if h1 else t["title"]
        solved = any("solved" in _text(lb).lower() for lb in labels)
        if seen.get("pages"):
            pages = list(range(max(2, seen["pages"]), n + 1))         # what's new since last time
        else:
            pages = list(range(2, n + 1))
            if len(pages) > self.max_pages - 1:                      # page 1 + the newest pages
                pages = pages[-(self.max_pages - 1):]
        root = f"{self.ctx.source.id}:t:{t['id']}"
        yield from self._posts(first, t, root, title, solved, 1)
        for p in pages:
            body = self._get(f"{t['href'].rstrip('/')}/page-{p}", ttl=0 if p >= n - 1 else None)
            yield from self._posts(body, t, root, title, solved, p)

    def _posts(self, page: str, t: dict, root: str, title: str, solved: bool, page_no: int) -> Iterator[Document]:
        for i, p in enumerate(parse_posts(page, self.count_labels)):
            is_root = page_no == 1 and i == 0
            label = p["user_title"] or ""
            yield Document(
                doc_id=root if is_root else f"{self.ctx.source.id}:p:{p['id']}",
                source=self.ctx.source.id, source_type=self.ctx.source.source_type,
                kind="question" if is_root else "reply",
                url=self._url(f"{t['href'].rstrip('/')}/post-{p['id']}"), title=title,
                content_md=html_to_markdown(p["body_html"], base_url=self._url("")),
                post_date=p["date"], updated_date=p["date"], author=p["author"], author_id=p["author_id"],
                author_reputation=p["messages"], author_created=p["joined"], score=p["likes"],
                is_vendor_staff=bool(p["staff"] or (self.staff_title and self.staff_title.search(label))),
                parent_id=None if is_root else root,
                extra={"author_label": label or None, "page": page_no, "solved_thread": solved,
                       "recognized_expert": bool(self.expert_title and self.expert_title.search(label))},
            )
