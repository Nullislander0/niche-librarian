"""Pages you approved one at a time (see engine/webpages.py). One Document per page.

The URL list lives in data/web_pages/<pack>.json, not in sources.yaml. Every pack gets this
source automatically. A page is fetched once (a copy read earlier with `read_url` is reused
from the cache) and not refreshed afterwards; `rebuild` replays it from the cache.
"""
from __future__ import annotations

from typing import Iterator

from engine import webpages
from engine.parsers.base import CrawlContext, Document

CACHE_TTL = 30 * 86400   # reuse a copy read for an answer within the last month


class Parser:
    POSITION_KEY_PREFIXES = ("page:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        self.pack_id = ctx.options["pack"]

    def crawl(self) -> Iterator[Document]:
        for url, info in webpages.load(self.pack_id)["approved"].items():
            key = f"page:{url}"
            if self.ctx.state.get(key):
                continue
            self.ctx.check_budget()
            try:
                page = webpages.fetch_page(self.ctx.fetcher, url, ttl=CACHE_TTL)
            except webpages.PageError as e:
                self.ctx.log.warning("skipping %s: %s", url, e)
                continue
            yield Document(
                doc_id=webpages.doc_id(url), source=self.ctx.source.id, source_type=self.ctx.source.source_type,
                kind="page", url=url, title=page["title"], content_md=page["content_md"],
                post_date=page["post_date"], updated_date=page["updated_date"], tags=[webpages.host(url)],
                extra={"description": page["description"], "note": info.get("note"),
                       "proposed_by": info.get("proposed_by")},
            )
            self.ctx.state.set(key, info.get("approved_at") or 1)
