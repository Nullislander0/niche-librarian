"""What a parser is, what it yields, and how the engine finds one.

A parser module defines a class named `Parser`:

    class Parser:
        POSITION_KEY_PREFIXES = ("cursor",)  # state keys holding crawl position (reset on rebuild)
        def __init__(self, ctx: CrawlContext): ...
        def crawl(self) -> Iterator[Document]: ...

The engine looks for `packs/<pack>/parsers/<name>.py` first, then
`engine/parsers/<name>.py`, so a pack can override or add parsers without
touching the engine.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Protocol

from engine.config import SourceConfig
from engine.crawl.fetcher import Fetcher


class StopCrawl(Exception):
    """Stop this source cleanly (budget spent, quota low). Progress so far is kept."""


@dataclass
class Document:
    doc_id: str  # stable and unique, e.g. "gis_se:a:12345"
    source: str  # source id from sources.yaml
    source_type: str  # forum, docs, blog, talk, issue, ...
    kind: str  # question, answer, post, page, ...
    url: str
    title: str
    content_md: str
    post_date: int | None = None  # unix seconds
    updated_date: int | None = None
    author: str | None = None
    author_id: str | None = None
    author_reputation: int | None = None
    author_created: int | None = None  # account creation, unix seconds
    is_accepted: bool = False
    is_top_voted: bool = False
    is_vendor_staff: bool = False
    score: int | None = None  # net votes
    parent_id: str | None = None  # thread root, if this is a reply
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class StateStore(Protocol):
    def get(self, key: str, default=None): ...
    def set(self, key: str, value) -> None: ...


@dataclass
class CrawlContext:
    source: SourceConfig
    fetcher: Fetcher
    state: StateStore
    max_requests: int | None = None
    lookup: Callable[[str], dict | None] | None = None  # read an already-indexed document by doc_id
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("crawl"))
    _start_requests: int = 0

    def __post_init__(self):
        self._start_requests = self.fetcher.network_requests

    @property
    def options(self) -> dict:
        return self.source.options

    @property
    def requests_made(self) -> int:
        return self.fetcher.network_requests - self._start_requests

    def check_budget(self) -> None:
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise StopCrawl(f"request budget of {self.max_requests} reached")


def load_parser(name: str, pack_path: Path):
    """Return the Parser class for `name`, preferring a pack override."""
    override = pack_path / "parsers" / f"{name}.py"
    if override.exists():
        spec = importlib.util.spec_from_file_location(f"pack_parser_{pack_path.name}_{name}", override)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(f"engine.parsers.{name}")
    return module.Parser


__all__ = ["CrawlContext", "Document", "StopCrawl", "load_parser", "Iterator"]
