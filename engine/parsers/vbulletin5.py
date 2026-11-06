"""vBulletin 5 forums (server-rendered HTML). One Document per post.

Options (sources.yaml):
  base_url: https://forum.example.com/
  channels:                                    # boards, paths relative to base_url
    - {path: forum/talk/open-forum, name: Open forum}
  since: "2024-01-01"                          # threads with activity since then
  max_pages_per_channel: 20                    # listing pages read per board and run
  max_pages_per_thread: 10                     # long threads: page 1 plus the newest pages
  staff_title: 'Moderator|Administrator'       # user title regex -> is_vendor_staff
  expert_title: 'Doctor|Optometrist'           # -> recognized_expert

Boards list topics by last activity, each with its timestamp (data-lastcontent), so a run
stops paging at the first topic older than `since` (sticky topics aside) and re-reads only
threads that changed since the last run. A thread's first post is its root ("question");
the rest are replies.
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

_TOPIC = re.compile(r'<(?:li|div|tr)[^>]*class="[^"]*\btopic-item\b([^"]*)"[^>]*?data-lastcontent="(\d+)"[^>]*?'
                    r'data-node-id="(\d+)"', re.S)
_POST = re.compile(r'<li\s+data-node-id="(\d+)"\s+data-node-publishdate="(\d+)"\s+class="(b-post[^"]*)"', re.S)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}


def _int(s: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def _joined(s: str | None) -> int | None:
    m = re.match(r"\s*([A-Za-z]{3})[a-z]*\s+(\d{4})", s or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return int(datetime(int(m.group(2)), _MONTHS[m.group(1).lower()], 1, tzinfo=timezone.utc).timestamp())


def _text(fragment: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>|\s+", " ", fragment or "")).strip()


class Parser:
    POSITION_KEY_PREFIXES = ("thread:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.base = o["base_url"].rstrip("/") + "/"
        self.channels = o.get("channels", [])
        self.since = int(datetime.fromisoformat(str(o.get("since", "2000-01-01")))
                         .replace(tzinfo=timezone.utc).timestamp())
        self.max_list = int(o.get("max_pages_per_channel", 20))
        self.max_pages = int(o.get("max_pages_per_thread", 10))
        self.staff = re.compile(o["staff_title"], re.I) if o.get("staff_title") else None
        self.expert = re.compile(o["expert_title"], re.I) if o.get("expert_title") else None

    def _get(self, url: str, ttl: int | None = None) -> str:
        self.ctx.check_budget()
        return self.ctx.fetcher.get_text(url, ttl=ttl)

    def topics(self, channel: dict) -> list[tuple[str, int]]:
        """[(thread url, last activity)] for threads active since `since`, newest first."""
        path = channel["path"].strip("/")
        out, seen = [], set()
        for n in range(1, self.max_list + 1):
            page = self._get(self.base + path + (f"/page{n}" if n > 1 else ""), ttl=3600)
            found = older = 0
            for m in _TOPIC.finditer(page):
                cls, last, node = m.group(1), int(m.group(2)), m.group(3)
                if node in seen:
                    continue
                seen.add(node)
                block = page[m.end():m.end() + 4000]
                link = re.search(rf'href="({re.escape(self.base)}{re.escape(path)}/{node}-[^"/?#]+)"', block)
                if not link:
                    continue
                found += 1
                if last < self.since:
                    older += "sticky" not in cls
                    continue
                out.append((link.group(1), last))
            if not found or older:       # past `since` (listings are newest first) or out of pages
                break
        return out

    def crawl(self) -> Iterator[Document]:
        for ch in self.channels:
            for url, last in self.topics(ch):
                key = f"thread:{url.rsplit('/', 1)[-1].split('-', 1)[0]}"
                if (self.ctx.state.get(key) or 0) >= last:
                    continue
                try:
                    yield from self._thread(url, ch)
                except FetchError as e:
                    if e.status not in (404, 410):
                        raise
                    self.ctx.log.warning("gone (HTTP %s), skipping: %s", e.status, url)
                self.ctx.state.set(key, last)

    def _thread(self, url: str, channel: dict) -> Iterator[Document]:
        first = self._get(url)
        # <title> is "Thread title - Site name"; the first <h1> can be a site announcement.
        t = re.search(r"<title>(.*?)</title>", first, re.S)
        title = _text(t.group(1)).rsplit(" - ", 1)[0] if t else url.rsplit("/", 1)[-1]
        last_page = max([1] + [int(x) for x in re.findall(rf'href="{re.escape(url)}/page(\d+)"', first)])
        pages = [1] + list(range(max(2, last_page - self.max_pages + 2), last_page + 1))
        root = None
        for n in pages:
            page = first if n == 1 else self._get(f"{url}/page{n}")
            for doc in self._posts(page, url, title, channel, n):
                if doc.kind == "question":
                    root = doc.doc_id
                elif root:
                    doc.parent_id = root
                yield doc

    def _posts(self, page: str, url: str, title: str, channel: dict, page_no: int) -> Iterator[Document]:
        marks = list(_POST.finditer(page))
        for i, m in enumerate(marks):
            node, when, cls = m.group(1), int(m.group(2)), m.group(3)
            block = page[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(page)]
            author = re.search(r'<div class="author[^"]*">\s*<strong>\s*<a href="[^"]*/member/(\d+)-[^"]*"\s*>([^<]+)</a>',
                               block)
            label = _text((re.search(r'<div class="usertitle">(.*?)</div>', block, re.S) or [None, ""])[1]) or None
            posts = re.search(r"<label>Posts:</label>\s*<span>([^<]+)</span>", block)
            joined = re.search(r"<label>Join Date:</label>\s*<span>([^<]+)</span>", block)
            body = extract_element(block, "div", cls="js-post__content-text") or ""
            is_first = "b-post--first" in cls
            yield Document(
                doc_id=f"{self.ctx.source.id}:p:{node}", source=self.ctx.source.id,
                source_type=self.ctx.source.source_type, kind="question" if is_first else "reply",
                url=f"{url}#post{node}" if not is_first else url, title=title,
                content_md=html_to_markdown(body, base_url=self.base), post_date=when, updated_date=when,
                author=html.unescape(author.group(2)).strip() if author else None,
                author_id=author.group(1) if author else None,
                author_reputation=_int(posts.group(1)) if posts else None,
                author_created=_joined(joined.group(1)) if joined else None,
                is_vendor_staff=bool(self.staff and label and self.staff.search(label)),
                extra={"author_label": label, "channel": channel.get("name"), "page": page_no,
                       "recognized_expert": bool(self.expert and label and self.expert.search(label))})
