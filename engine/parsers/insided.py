"""Gainsight / inSided communities (e.g. community.safe.com). One Document per post.

There's no public API, but topic pages embed their data as JSON (data-props): the topic,
its replies with isBestAnswer and likes, and an authors map (name, title, rank, likes
received). Topics are discovered from the site's own sitemap and filtered by URL slug.

Options (sources.yaml):
  base_url: https://community.example.com
  sitemap: https://community.example.com/sitemap.xml       # index or topics sitemap
  include_slugs: ['esri|arcgis', 'smallworld']             # a topic is kept if any regex matches its URL slug
  exclude_categories: ['ideas']                            # first path segment, e.g. "ideas"
  staff_titles: 'Safer|Staff|Moderator'                    # author title/rank regex -> is_vendor_staff
  expert_titles: 'Evangelist|Influencer|Celebrity'          # -> recognized_expert
  refresh_days: 30
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def _ts(s: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(s).timestamp()) if s else None
    except ValueError:
        return None


def topic_props(page: str) -> dict | None:
    """The embedded JSON blob that holds the topic and its replies."""
    for raw in re.findall(r'data-props="([^"]+)"', page):
        if "repliesProps" in raw:
            try:
                return json.loads(html.unescape(raw))
            except ValueError:
                return None
    return None


def _likes(item: dict) -> int:
    for k in ("likesCount", "likes"):
        if str(item.get(k, "")).isdigit():
            return int(item[k])
    return len(item.get("likedBy") or [])


class Parser:
    POSITION_KEY_PREFIXES = ("topic:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.base = o["base_url"].rstrip("/")
        self.sitemap = o.get("sitemap", self.base + "/sitemap.xml")
        self.include = [re.compile(p, re.I) for p in o.get("include_slugs", [])]
        self.exclude_cats = set(o.get("exclude_categories", []))
        self.staff = re.compile(o["staff_titles"], re.I) if o.get("staff_titles") else None
        self.expert = re.compile(o["expert_titles"], re.I) if o.get("expert_titles") else None
        self.refresh = int(o.get("refresh_days", 30)) * 86400

    def _topics(self) -> list[str]:
        urls, queue = [], [self.sitemap]
        while queue:
            self.ctx.check_budget()
            body = self.ctx.fetcher.get_text(queue.pop(0), ttl=86400)
            for loc in _LOC.findall(body):
                loc = html.unescape(loc)
                if loc.endswith(".xml"):
                    if "topic" in loc:        # skip structure/category sitemaps
                        queue.append(loc)
                    continue
                path = loc.replace(self.base, "").strip("/")
                if "/" not in path or path.split("/")[0] in self.exclude_cats:
                    continue
                slug = path.rsplit("/", 1)[-1]
                if not self.include or any(p.search(slug) for p in self.include):
                    urls.append(loc)
        self.ctx.log.info("%d matching topics in sitemap", len(urls))
        # Newest first: topic ids are the trailing number of the URL.
        return sorted(set(urls), key=lambda u: -int(re.search(r"-(\d+)$", u).group(1))
                      if re.search(r"-(\d+)$", u) else 0)

    def crawl(self) -> Iterator[Document]:
        now = time.time()
        for url in self._topics():
            key = f"topic:{url}"
            seen = self.ctx.state.get(key)
            if seen and now - seen < self.refresh:
                continue
            try:
                yield from self._topic(url)
            except FetchError as e:
                if e.status not in (404, 410):
                    raise
                self.ctx.log.warning("gone (HTTP %s), skipping: %s", e.status, url)
            self.ctx.state.set(key, now)

    def _page(self, url: str) -> dict | None:
        self.ctx.check_budget()
        return topic_props(self.ctx.fetcher.get_text(url, ttl=self.refresh))

    def _topic(self, url: str) -> Iterator[Document]:
        d = self._page(url)
        if not d or not d.get("topic"):
            self.ctx.log.warning("no topic data in %s", url)
            return
        t, rp = d["topic"], d.get("repliesProps") or {}
        authors = dict(d.get("authors") or {})
        replies = list(rp.get("replies") or [])
        for n in range(2, int(rp.get("totalPages") or 1) + 1):   # long topics
            more = self._page(f"{url}?page={n}")
            if not more or more.get("currentPage") not in (n, str(n)):
                break
            authors.update(more.get("authors") or {})
            replies += (more.get("repliesProps") or {}).get("replies") or []
        category = (url.replace(self.base, "").strip("/").split("/") or [""])[0]
        tags = [x.get("name", x) if isinstance(x, dict) else x for x in t.get("tags") or []]
        root = f"{self.ctx.source.id}:t:{t.get('publicId') or t.get('id')}"
        title = html.unescape(t.get("title") or "")
        yield self._doc(root, None, "question", url, title, t, t.get("publishedAt"), authors, tags, category)
        for r in replies:
            rid = r.get("publicReplyId") or r.get("id")
            kind = "answer" if r.get("isBestAnswer") else "reply"
            yield self._doc(f"{self.ctx.source.id}:r:{rid}", root, kind, f"{url}#post{rid}", title, r,
                            r.get("repliedAt"), authors, tags, category)

    def _doc(self, doc_id, parent, kind, url, title, item, when, authors, tags, category) -> Document:
        a = authors.get(str(item.get("authorId"))) or {}
        label = " / ".join(dict.fromkeys(filter(None, [a.get("userTitle"), (a.get("rank") or {}).get("name")])))
        return Document(
            doc_id=doc_id, source=self.ctx.source.id, source_type=self.ctx.source.source_type, kind=kind,
            url=url, title=title, content_md=html_to_markdown(item.get("content") or "", base_url=self.base + "/"),
            post_date=_ts(when), updated_date=_ts(item.get("lastEditedAt") or when),
            author=a.get("name"), author_id=str(item.get("authorId")) if item.get("authorId") else None,
            author_reputation=a.get("likesReceived"), score=_likes(item),
            is_accepted=kind == "answer", parent_id=parent, tags=tags,
            is_vendor_staff=bool(self.staff and label and self.staff.search(label)),
            extra={"author_label": label or None, "category": category,
                   "recognized_expert": bool(self.expert and label and self.expert.search(label))},
        )
