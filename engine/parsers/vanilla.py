"""Vanilla Forums communities via the public API v2 (/api/v2/discussions, /api/v2/comments).

Works for any Vanilla community that allows anonymous API reads. It only uses
the documented read endpoints, and stops if the site starts refusing them.

Options (sources.yaml):
  base_url: https://community.example.com
  category_tiers:                   # crawled in order; ids or {id, name}
    - {tier: 1, categories: [{id: 739, name: Questions}, 1440]}
  page_size: 500                    # API max per request
  revisit_days: 30                  # each run re-reads this window to catch accepted
                                    # answers and votes that changed after indexing
  redirect_param: target            # unwrap /home/leaving?target=... links
  staff_label_pattern: '^\\s*Esri\\b'    # user label -> is_vendor_staff
  expert_label_pattern: '\\bMVP\\b'      # user label -> extra.recognized_expert

Strategy: for each category, walk discussions then comments oldest-first with a
dateInserted cursor (listings are capped at 10,000 results, so page numbers
can't reach the end). Comments are fetched in bulk per category, not per thread.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown


def _ts(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso).timestamp()) if iso else None


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Parser:
    POSITION_KEY_PREFIXES = ("cat:",)  # crawl positions; a rebuild resets only these

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.base = o["base_url"].rstrip("/")
        self.tiers = sorted(o.get("category_tiers", []), key=lambda t: t.get("tier", 0))
        self.page_size = int(o.get("page_size", 500))
        self.revisit = int(o.get("revisit_days", 30)) * 86400
        self.redirect_param = o.get("redirect_param")
        self.staff = re.compile(o["staff_label_pattern"]) if o.get("staff_label_pattern") else None
        self.expert = re.compile(o["expert_label_pattern"]) if o.get("expert_label_pattern") else None

    def crawl(self) -> Iterator[Document]:
        for tier in self.tiers:
            for cat in tier["categories"]:
                cat = cat["id"] if isinstance(cat, dict) else cat
                self.ctx.log.info("tier %s category %s", tier.get("tier"), cat)
                yield from self._walk(cat, "discussions", self._discussion)
                yield from self._walk(cat, "comments", self._comment)

    def _walk(self, category: int, endpoint: str, convert) -> Iterator[Document]:
        key = f"cat:{category}:{endpoint}"
        st = self.ctx.state.get(key) or {"cursor": 0, "complete": False}
        cursor = max(0, st["cursor"] - self.revisit) if st["complete"] else st["cursor"]
        size = self.page_size
        while True:
            self.ctx.check_budget()
            try:
                items = self.ctx.fetcher.get_json(f"{self.base}/api/v2/{endpoint}", {
                    "categoryID": category, "sort": "dateInserted", "limit": size,
                    "dateInserted": f">={_iso(cursor)}", "expand": "insertUser",
                }, ttl=0 if st["complete"] else None)
            except FetchError as e:
                # The API sometimes fails to serialise one malformed post ("Validation failed").
                # Shrink the page to isolate it, then step past it by one second.
                if e.status != 400 or "Validation failed" not in e.body:
                    raise
                if size > 1:
                    size = max(1, size // 5)
                else:
                    self.ctx.log.warning("skipping an item the API can't return (%s, %s at %s)",
                                         endpoint, category, _iso(cursor))
                    cursor, size = cursor + 1, self.page_size
                continue
            for item in items:
                doc = convert(item)
                if doc:
                    yield doc
            if len(items) < size:
                break
            last = _ts(items[-1]["dateInserted"])
            cursor = last if last > cursor else cursor + 1
            st["cursor"] = max(st["cursor"], cursor)
            self.ctx.state.set(key, st)
            size = self.page_size
        if items:
            st["cursor"] = max(st["cursor"], _ts(items[-1]["dateInserted"]))
        st["complete"] = True
        self.ctx.state.set(key, st)

    # -- normalise --------------------------------------------------------

    def _author(self, item: dict) -> dict:
        u = item.get("insertUser") or {}
        label = (u.get("label") or "").strip()
        return {
            "author": u.get("name"),
            "author_id": str(u["userID"]) if u.get("userID") else None,
            "is_vendor_staff": bool(self.staff and self.staff.search(label)),
            "extra": {"author_label": label or None,
                      "recognized_expert": bool(self.expert and self.expert.search(label))},
        }

    def _discussion(self, d: dict) -> Document:
        a = self._author(d)
        a["extra"].update(category_id=d.get("categoryID"), views=d.get("countViews"),
                          comments=d.get("countComments"), labels=d.get("labelCodes") or [],
                          question_status=((d.get("attributes") or {}).get("question") or {}).get("status"))
        return Document(
            doc_id=f"{self.ctx.source.id}:d:{d['discussionID']}", source=self.ctx.source.id,
            source_type=self.ctx.source.source_type, kind=d.get("type") or "discussion",
            url=d.get("canonicalUrl") or d["url"], title=d.get("name", ""),
            content_md=html_to_markdown(d.get("body", ""), self.redirect_param),
            post_date=_ts(d.get("dateInserted")),
            updated_date=_ts(d.get("dateLastComment") or d.get("dateUpdated") or d.get("dateInserted")),
            score=d.get("score"), tags=[t["name"] for t in d.get("tags") or [] if t.get("name")], **a,
        )

    def _comment(self, c: dict) -> Document | None:
        if c.get("parentRecordType", "discussion") != "discussion":
            return None
        parent_id = f"{self.ctx.source.id}:d:{c['discussionID']}"
        parent = self.ctx.lookup(parent_id) if self.ctx.lookup else None
        title = parent["title"] if parent else re.sub(r"^Re:\s*", "", c.get("name") or "")
        answer = (c.get("attributes") or {}).get("answer") or {}
        return Document(
            doc_id=f"{self.ctx.source.id}:c:{c['commentID']}", source=self.ctx.source.id,
            source_type=self.ctx.source.source_type, kind="answer" if answer else "reply",
            url=c.get("url", ""), title=title,
            content_md=html_to_markdown(c.get("body", ""), self.redirect_param),
            post_date=_ts(c.get("dateInserted")), updated_date=_ts(c.get("dateUpdated") or c.get("dateInserted")),
            score=c.get("score"), is_accepted=answer.get("status") == "accepted",
            parent_id=parent_id, tags=parent["tags"] if parent else [], **self._author(c),
        )

