"""MediaWiki sites via the official API (api.php). One Document per article.

Options (sources.yaml):
  api: https://wiki.example.org/api.php
  page_url: https://wiki.example.org/title/{title}   # {title} is URL-encoded with _ for spaces
  namespaces: [0]                                   # 0 = articles
  exclude_titles: ['\\((Español|Deutsch|...)\\)$']   # e.g. translated copies
  include_titles: []                                # if set, only matching titles
  kind: page

Discovery lists every page with its latest revision id (500 per request); a page is
fetched (action=parse, rendered HTML) only when its revision changed since the last run,
so incremental runs cost a few listing requests plus whatever was edited.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime
from typing import Iterator

from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown

# Wiki chrome that isn't content.
_STRIP = [
    r'<span class="mw-editsection">.*?</span>\s*</span>',
    r'<div[^>]+(?:id="toc"|class="toc")[^>]*>.*?</div>\s*</div>',
    r'<(?:div|table)[^>]+class="[^"]*\b(?:navbox|noprint|archwiki-template-meta-related-articles)\b[^"]*"[^>]*>.*?</(?:div|table)>',
    r'<sup[^>]+class="reference"[^>]*>.*?</sup>',
    r"<style[^>]*>.*?</style>",
]


def clean_html(h: str) -> str:
    for pat in _STRIP:
        h = re.sub(pat, "", h, flags=re.S | re.I)
    return h


def _ts(s: str | None) -> int | None:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


class Parser:
    POSITION_KEY_PREFIXES = ("rev:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.api = o["api"]
        self.page_url = o["page_url"]
        self.namespaces = o.get("namespaces", [0])
        self.exclude = [re.compile(p) for p in o.get("exclude_titles", [])]
        self.include = [re.compile(p) for p in o.get("include_titles", [])]
        self.kind = o.get("kind", "page")

    def _api(self, params: dict, ttl: int | None = 0) -> dict:
        self.ctx.check_budget()
        data = self.ctx.fetcher.get_json(self.api, {"format": "json", "formatversion": 2, **params}, ttl=ttl)
        if "error" in data:
            raise RuntimeError(f"MediaWiki API error: {data['error']}")
        return data

    def _pages(self) -> Iterator[dict]:
        for ns in self.namespaces:
            cont: dict = {}
            while True:
                data = self._api({"action": "query", "generator": "allpages", "gapnamespace": ns,
                                  "gapfilterredir": "nonredirects", "gaplimit": "max", "prop": "info", **cont})
                yield from (data.get("query") or {}).get("pages", [])
                if "continue" not in data:
                    break
                cont = data["continue"]

    def _wanted(self, title: str) -> bool:
        if self.include and not any(p.search(title) for p in self.include):
            return False
        return not any(p.search(title) for p in self.exclude)

    def crawl(self) -> Iterator[Document]:
        pages = [p for p in self._pages() if self._wanted(p["title"])]
        self.ctx.log.info("%d pages listed", len(pages))
        for p in pages:
            key = f"rev:{p['pageid']}"
            if self.ctx.state.get(key) == p.get("lastrevid"):
                continue  # unchanged since last run
            data = self._api({"action": "parse", "pageid": p["pageid"], "prop": "text|displaytitle|revid"},
                             ttl=None)
            parsed = data.get("parse") or {}
            title = re.sub(r"<[^>]+>", "", parsed.get("displaytitle") or p["title"])
            url = self.page_url.format(title=urllib.parse.quote(p["title"].replace(" ", "_"), safe="/:()_-.,"))
            touched = _ts(p.get("touched"))
            yield Document(
                doc_id=f"{self.ctx.source.id}:{p['pageid']}", source=self.ctx.source.id,
                source_type=self.ctx.source.source_type, kind=self.kind, url=url, title=title,
                content_md=html_to_markdown(clean_html(parsed.get("text", "")), base_url=url),
                post_date=touched, updated_date=touched,
                extra={"revid": parsed.get("revid") or p.get("lastrevid")},
            )
            self.ctx.state.set(key, p.get("lastrevid"))
