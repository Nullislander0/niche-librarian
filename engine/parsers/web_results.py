"""Stored web search results (engine/webresults.py). They are written when watches run, so
there is nothing to crawl here; the source exists so the index and `outline` know about it."""
from __future__ import annotations

from typing import Iterator

from engine.parsers.base import CrawlContext, Document


class Parser:
    POSITION_KEY_PREFIXES = ()

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx

    def crawl(self) -> Iterator[Document]:
        return iter(())
