"""MCP server exposing one pack's index: search, fetch, index_info. Runs over stdio."""
from __future__ import annotations

import json
import logging
import sys
import threading

from mcp.server.mcpserver import MCPServer

from engine.config import load_pack
from engine.search import Searcher


def format_results(res: dict) -> str:
    if not res["results"]:
        return f"No results for {res['query']!r} (mode: {res['mode']})."
    head = f"{len(res['results'])} results for {res['query']!r} (mode: {res['mode']}"
    if res["query_versions"]:
        head += f"; version context: {', '.join(res['query_versions'])}"
    if res.get("expanded_with"):
        head += f"; also searched: {', '.join(res['expanded_with'])}"
    lines = [head + ")"]
    for i, r in enumerate(res["results"], 1):
        meta = [r["date"], f"v {','.join(r['versions'])}" if r["versions"] else "v ?",
                f"{r['source']}/{r['kind']}", *r["flags"], f"score {r['score']}",
                f"~{r['tokens']['document']} tok"]
        lines.append(f"\n[{i}] {r['chunk_id']}  |  {'  |  '.join(meta)}\n    {r['title']}\n"
                     f"    {r['url']}\n    {r['snippet']}")
        if "explain" in r:
            lines.append(f"    why: {json.dumps(r['explain'])}")
    return "\n".join(lines)


def build_server(pack_id: str) -> MCPServer:
    pack = load_pack(pack_id)
    searcher = Searcher(pack)
    lock = threading.Lock()  # one SQLite connection, tools may run on worker threads
    server = MCPServer(
        name=f"librarian-{pack.id}",
        instructions=(
            f"Local search index: {pack.name}. {pack.description} "
            "Call `outline` once to see what it covers. Use `search` to find candidate passages, then `fetch` "
            "with a chunk_id to read the full text (scope='thread' for the whole discussion) before answering. "
            "Forum advice varies in quality: prefer accepted answers, vendor staff, and posts matching the "
            "user's version; say when sources disagree or when nothing relevant was found. "
            "Cite results by chunk_id and URL. Mention a product version in the query (or pass `version`) "
            "to rank matching versions higher."
        ),
    )

    @server.tool()
    def search(query: str, limit: int = 10, version: str | None = None, version_mode: str = "boost",
               date_from: str | None = None, date_to: str | None = None,
               source_type: str | None = None, source: str | None = None, explain: bool = False) -> str:
        """Hybrid keyword + semantic search over the local index.

        Returns compact results: chunk_id (stable, citable), date, detected versions, source, flags
        (accepted / top_voted / vendor_staff), score, URL, and a short snippet.

        Args:
            query: natural-language question or keywords; error text works well.
            limit: max results (1-50).
            version: product version to prefer, e.g. "11.3" or "10.9.1"; "11" matches all 11.x.
            version_mode: "boost" ranks matching versions higher (default); "filter" keeps only them.
            date_from / date_to: ISO dates (YYYY-MM-DD) bounding the post date.
            source_type: e.g. "forum", "docs", "blog", "issue", "talk".
            source: a source id from index_info.
            explain: include per-signal scoring breakdown.
        """
        with lock:
            res = searcher.search(query, max(1, min(limit, 50)), version, version_mode, date_from, date_to,
                                  source_type, source, explain)
        return format_results(res)

    @server.tool()
    def fetch(chunk_id: str, scope: str = "document") -> str:
        """Full text of a result as clean markdown, with citation metadata.

        Args:
            chunk_id: id from search results, e.g. "gis_se:a:12345#c0".
            scope: "chunk" (just that passage), "document" (the whole post, default),
                   or "thread" (question plus all answers, accepted first).
        """
        with lock:
            text = searcher.fetch(chunk_id, scope)
        return text if text is not None else f"No document for {chunk_id!r}."

    @server.tool()
    def outline(source: str | None = None) -> str:
        """Map of the index: sources, boards/tags, date ranges and version coverage.
        Read this first to learn what the index can answer and which terms and versions it covers.

        Args:
            source: limit to one source id.
        """
        with lock:
            return searcher.outline(source)

    @server.tool()
    def index_info() -> str:
        """What this index covers: sources, document counts, date ranges, version tag counts."""
        with lock:
            return json.dumps(searcher.info(), indent=1)

    return server


def main(pack_id: str) -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout belongs to MCP
    build_server(pack_id).run()
