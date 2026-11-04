"""MCP server exposing one pack's index: search, fetch, index_info. Runs over stdio."""
from __future__ import annotations

import json
import logging
import sys
import textwrap
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
    hints = pack.agent or {}
    good = "; ".join(f"'{q}'" for q in hints.get("good_queries", []))
    bad = "; ".join(f"'{q}'" for q in hints.get("bad_queries", []))
    examples = (f" Good: {good}." if good else "") + (f" Bad: {bad}." if bad else "")
    server = MCPServer(
        name=f"librarian-{pack.id}",
        instructions=(
            f"Local search index: {pack.name}. {pack.description} "
            "Workflow: call `outline` once to see what is covered; `search` for candidate passages; "
            "`fetch` the best hits (scope='thread' for a whole discussion) before answering. "
            "Weigh sources: results flagged 'official' (vendor docs, knowledge base, bug reports) outrank "
            "forum posts; among forum posts prefer accepted answers, vendor staff, recognized experts, and "
            "posts on the user's version. Say when sources disagree, when advice is for a different "
            "version, or when nothing relevant was found. Cite every claim by chunk_id and URL."
        ),
    )

    search_description = textwrap.dedent(f"""\
        Hybrid keyword + semantic search over the local index, reranked for quality and version.

        Write one specific question or symptom per call; include error text and product names.
        If the user's question spans several distinct topics, make one call per topic.{examples}
        If two or three rephrased searches find nothing relevant, stop and say so rather than
        searching indefinitely.

        Returns compact results: chunk_id (stable, citable), date, versions, source/kind, flags
        (official, accepted, top_voted, vendor_staff, expert), score, approximate tokens, URL, snippet.

        Args:
            query: the question or symptom, e.g. an error message plus the component involved.
            limit: max results (1-50).
            version: the user's product version, e.g. "11.3" or "10.9.1" ("11" = any 11.x).
                Pass it whenever known: matching versions rank higher.
            version_mode: "boost" (default) ranks matching versions higher; "filter" keeps only them
                (drops the many posts that never state a version, so use sparingly).
            date_from / date_to: ISO dates (YYYY-MM-DD) bounding the post date.
            source_type: e.g. "docs", "kb", "bug", "forum" (see `outline`).
            source: a source id from `outline`.
            explain: include the per-signal scoring breakdown.
        """).strip()

    @server.tool(description=search_description)
    def search(query: str, limit: int = 10, version: str | None = None, version_mode: str = "boost",
               date_from: str | None = None, date_to: str | None = None,
               source_type: str | None = None, source: str | None = None, explain: bool = False) -> str:
        with lock:
            res = searcher.search(query, max(1, min(limit, 50)), version, version_mode, date_from, date_to,
                                  source_type, source, explain)
        return format_results(res)

    @server.tool()
    def fetch(chunk_id: str, scope: str = "document", max_tokens: int = 6000) -> str:
        """Full text of a result as clean markdown, with citation metadata.

        Args:
            chunk_id: id from search results, e.g. "gis_se:a:12345#c0".
            scope: "chunk" (just that passage), "document" (the whole post or page, default),
                   or "thread" (question plus replies: accepted answer, vendor staff, experts and
                   top-voted first).
            max_tokens: approximate output budget (default 6000). Content beyond it is listed by id
                so you can fetch specific parts; raise it only when you need the whole thread.
        """
        with lock:
            text = searcher.fetch(chunk_id, scope, max_tokens)
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
