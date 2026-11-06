"""Parsers against the structure of real pages (tests/fixtures/real, made by tools/make_fixture.py).

Each fixture holds the responses a parser read from a real site, with all words, names and
hosts scrambled but the markup, JSON keys, numbers and dates intact, plus what the real parse
produced. Replaying it must give the same result, with no parse-health warnings: a parser
change that breaks on real-world markup fails here, not in a live crawl.
"""
import gzip
import json
import time
from pathlib import Path

import pytest

from engine.config import SourceConfig, load_pack
from engine.crawl.fetcher import FetchError
from engine.indexer import ParseHealth
from engine.parsers.base import CrawlContext, load_parser

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "real").glob("*.json.gz"))


class DictState(dict):
    def set(self, k, v):
        self[k] = v


class Replay:
    """Serves the recorded responses; anything else is a 404, like a missing page."""
    network_requests = 0
    offline = False
    user_agent = "test"

    def __init__(self, pages):
        self.pages = {(p["url"], json.dumps(p["params"] or {}, sort_keys=True)): p["body"] for p in pages}
        self.misses = []

    def get_text(self, url, params=None, ttl=None, **kw):
        key = (url, json.dumps({k: v for k, v in (params or {}).items()
                                if k not in ("key", "api_key", "access_token")}, sort_keys=True))
        if key not in self.pages:
            self.misses.append(url)
            raise FetchError(f"HTTP 404 for {url}", 404)
        return self.pages[key]

    def get_json(self, url, params=None, ttl=None, **kw):
        return json.loads(self.get_text(url, params, ttl))


@pytest.mark.parametrize("path", FIXTURES, ids=[p.name.split(".")[0] for p in FIXTURES])
def test_parser_reads_real_structure(path, monkeypatch):
    fx = json.load(gzip.open(path, "rt", encoding="utf-8"))
    monkeypatch.setattr(time, "time", lambda: float(fx["recorded_at"]))
    src = SourceConfig(id="fx", parser=fx["parser"], source_type=fx["source_type"], options=fx["options"])
    replay = Replay(fx["pages"])
    parser = load_parser(fx["parser"], load_pack("homelab").path)(CrawlContext(source=src, fetcher=replay,
                                                                               state=DictState()))
    docs, roots = [], 0
    for d in parser.crawl():
        if d.parent_id is None:
            roots += 1
            if roots > fx["expect"]["roots"]:
                break
        docs.append(d)
    got = {"documents": len(docs), "roots": sum(d.parent_id is None for d in docs),
           "with_author": sum(bool(d.author) for d in docs), "with_date": sum(d.post_date is not None for d in docs),
           "kinds": sorted({d.kind for d in docs})}
    assert got == fx["expect"], f"parse changed (requests not in the fixture: {replay.misses[:3]})"
    health = ParseHealth(fx["source_type"])
    for d in docs:
        health.add(d)
    assert health.warnings() == []
    assert all(d.content_md.strip() for d in docs if d.kind != "page" or d.parent_id is None)
