"""Every MCP tool, called the way an agent calls it: through the real server, on a small index."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from engine import config, privacy, watches, webpages, websearch
from engine.index.embed import OllamaEmbedder
from engine.index.store import Store
from engine.parsers.base import Document

PACK_YAML = """
name: Test pack
description: A small pack for testing the MCP tools.
web:
  sites: {forum: forum.example.org}
  blocked_hosts: [blocked.example.org]
signals:
  terms: [{name: branda}, {name: brandb}]
  categories:
    price: {weight: 1.0, description: cost, keywords: 'price|cost|\\$\\d'}
sources:
  - id: forum
    parser: vbulletin5
    source_type: forum
    options: {base_url: "https://forum.example.org/", channels: []}
  - id: data
    parser: openfda
    source_type: data
    options: {drugs: []}
"""


def ts(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def server(tmp_path, monkeypatch):
    packs = tmp_path / "packs" / "testpack"
    packs.mkdir(parents=True)
    (packs / "sources.yaml").write_text(PACK_YAML, encoding="utf-8")
    data = tmp_path / "data"
    for mod in (config, webpages, watches, privacy):
        monkeypatch.setattr(mod, "DATA_DIR", data)
    monkeypatch.setattr(config, "PACK_DIRS", [tmp_path / "packs"])
    monkeypatch.setattr(OllamaEmbedder, "available", lambda self: False)      # keyword search, no Ollama
    monkeypatch.setenv("BRAVE_API_KEY", "test-key-not-real")

    pack = config.load_pack("testpack")
    store = Store(pack.db_path, 768)
    for doc in [
        Document(doc_id="forum:p:1", source="forum", source_type="forum", kind="question",
                 url="https://forum.example.org/t/1", title="Branda price went up",
                 content_md="The cash price of Branda is now $90 a month.", post_date=ts(2026, 10, 1), author="P-aaaaaa"),
        Document(doc_id="forum:p:2", source="forum", source_type="forum", kind="reply", parent_id="forum:p:1",
                 url="https://forum.example.org/t/1#2", title="Branda price went up",
                 content_md="I switched to Brandb because of the cost.", post_date=ts(2026, 10, 2), author="P-bbbbbb"),
        Document(doc_id="data:q1", source="data", source_type="data", kind="utilization", url="u",
                 title="Medicaid prescriptions: branda, 2026 Q1", content_md="numbers", post_date=ts(2026, 1),
                 extra={"term": "branda", "state": "XX", "metrics": {"medicaid_rx": 120}}),
        Document(doc_id="data:q4", source="data", source_type="data", kind="utilization", url="u",
                 title="Medicaid prescriptions: branda, 2025 Q4", content_md="numbers", post_date=ts(2025, 10),
                 extra={"term": "branda", "state": "XX", "metrics": {"medicaid_rx": 100}}),
    ]:
        store.upsert(doc, [], [doc.content_md])
    from engine import signals
    signals.run(pack, store, keywords_only=True)
    store.close()

    calls = []

    class FakeBrave:
        def search(self, query, count=10, freshness=None, site=None):
            calls.append({"query": query, "site": site, "freshness": freshness})
            return [{"kind": "discussion", "url": "https://forum.example.org/t/1", "title": "Branda price",
                     "snippet": "price thread", "age": "1 day ago", "backend": "brave"},
                    {"kind": "web", "url": "https://news.example.net/branda", "title": "Branda news",
                     "snippet": "news", "age": "", "backend": "brave"}]

    monkeypatch.setattr(websearch, "backend", lambda name: FakeBrave())
    monkeypatch.setattr(websearch, "_usage_path", lambda: tmp_path / "usage.json")
    from engine.mcp_server.server import build_server
    srv = build_server("testpack")
    srv.brave_calls = calls
    return srv


def call(srv, name, **args) -> str:
    result = asyncio.run(srv.call_tool(name, args))
    return "\n".join(c.text for c in result.content)


def test_the_expected_tools_are_offered(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"search", "fetch", "outline", "index_info", "read_url", "index_url", "web_search",
                     "signals", "memo_data"}


def test_outline_and_index_info(server):
    assert "# Test pack" in call(server, "outline") and "## forum" in call(server, "outline")
    info = json.loads(call(server, "index_info"))
    assert info["documents"] == 4 and set(info["by_source"]) == {"forum", "data"}


def test_search_then_fetch(server):
    out = call(server, "search", query="Branda cash price")
    assert "forum:p:1#c0" in out and "Branda price went up" in out and "mode: keyword" in out
    assert "No results" in call(server, "search", query="zzzz qqqq")
    doc = call(server, "fetch", chunk_id="forum:p:1#c0", scope="thread")
    assert "$90 a month" in doc and "switched to Brandb" in doc
    assert "No document" in call(server, "fetch", chunk_id="forum:p:999#c0")


def test_read_url_refuses_local_addresses_and_points_to_the_index(server):
    assert "private or local" in call(server, "read_url", url="http://127.0.0.1:11434/api/tags")
    assert "Already in the index as forum:p:1" in call(server, "read_url", url="https://forum.example.org/t/1")
    assert "blocked" in call(server, "read_url", url="https://blocked.example.org/page")


def test_index_url_only_queues(server):
    assert "Queued for the user's approval" in call(server, "index_url", url="https://news.example.net/a",
                                                    note="useful")
    assert "Already waiting" in call(server, "index_url", url="https://news.example.net/a")
    assert "crawled by source 'forum'" in call(server, "index_url", url="https://forum.example.org/t/77")
    q = webpages.load("testpack")
    assert list(q["pending"]) == ["https://news.example.net/a"] and q["approved"] == {}


def test_web_search_resolves_shortcuts_and_marks_indexed_results(server):
    out = call(server, "web_search", query="branda price", site="forum", freshness="pw")
    assert server.brave_calls[-1] == {"query": "branda price", "site": "forum.example.org", "freshness": "pw"}
    assert "[indexed: fetch forum:p:1#c0]" in out and "shown, not stored" in out
    assert "https://news.example.net/branda" in out


def test_signals_and_memo_data(server):
    sig = call(server, "signals", by="month")
    assert "| branda | (any mention) |" in sig and "| brandb | (any mention) |" in sig
    md = call(server, "memo_data")
    assert "| Medicaid prescriptions | 120 (2026 Q1) | 100 (2025 Q4) | +20 (+20.0%) | [data:q1] |" in md
    assert json.loads(call(server, "memo_data", as_json=True))["terms"]["branda"]["measures"]["medicaid_rx"]["value"] == 120
