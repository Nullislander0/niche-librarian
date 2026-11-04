"""librarian: crawl, embed, search, fetch, stats, serve."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from engine.config import load_pack


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="librarian", description="Local niche search index for AI agents.")
    p.add_argument("--pack", default="esri_enterprise", help="topic pack under packs/ (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    # Also accept --pack after the subcommand (`librarian serve --pack x`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pack", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", parents=[common], help="fetch sources and index them")
    c.add_argument("--source", action="append", help="only this source id (repeatable)")
    c.add_argument("--max-requests", type=int, help="network request budget per source for this run")
    c.add_argument("--no-embed", action="store_true", help="skip embedding new chunks")

    r = sub.add_parser("rebuild", parents=[common], help="re-parse saved API responses (no network)")
    r.add_argument("--source", action="append", help="only this source id (repeatable)")

    sub.add_parser("retag", parents=[common], help="recompute version tags after editing versions.yaml")

    e = sub.add_parser("embed", parents=[common], help="embed chunks that have no vector yet")
    e.add_argument("--reset", action="store_true", help="drop all vectors first (after changing model)")

    s = sub.add_parser("search", parents=[common], help="search from the terminal")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--version")
    s.add_argument("--version-mode", choices=["boost", "filter"], default="boost")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--source-type")
    s.add_argument("--source")
    s.add_argument("--explain", action="store_true")
    s.add_argument("--json", action="store_true")

    f = sub.add_parser("fetch", parents=[common], help="print a document by chunk id")
    f.add_argument("chunk_id")
    f.add_argument("--scope", choices=["chunk", "document", "thread"], default="document")

    sub.add_parser("stats", parents=[common], help="index statistics")
    o = sub.add_parser("outline", parents=[common], help="what the index covers")
    o.add_argument("--source")
    sub.add_parser("serve", parents=[common], help="run the MCP server on stdio")

    a = p.parse_args(argv)
    if a.cmd == "serve":
        from engine.mcp_server.server import main as serve
        serve(a.pack)
        return

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    pack = load_pack(a.pack)

    if a.cmd == "crawl":
        from engine.indexer import crawl
        print(json.dumps(crawl(pack, a.source, a.max_requests, embed=not a.no_embed), indent=2))
    elif a.cmd == "rebuild":
        from engine.indexer import crawl
        print(json.dumps(crawl(pack, a.source, offline=True), indent=2))
    elif a.cmd == "retag":
        from engine.indexer import retag
        print(json.dumps(retag(pack), indent=2))
    elif a.cmd == "outline":
        from engine.search import Searcher
        print(Searcher(pack).outline(a.source))
    elif a.cmd == "embed":
        from engine.indexer import embed_missing, open_store
        store = open_store(pack, reset_embeddings=a.reset)
        print(f"embedded: {embed_missing(store)}")
    elif a.cmd == "stats":
        from engine.search import Searcher
        print(json.dumps(Searcher(pack).info(), indent=2))
    elif a.cmd == "search":
        from engine.mcp_server.server import format_results
        from engine.search import Searcher
        res = Searcher(pack).search(a.query, a.limit, a.version, a.version_mode, a.date_from, a.date_to,
                                    a.source_type, a.source, a.explain)
        print(json.dumps(res, indent=2) if a.json else format_results(res))
    elif a.cmd == "fetch":
        from engine.search import Searcher
        print(Searcher(pack).fetch(a.chunk_id, a.scope) or f"not found: {a.chunk_id}")


if __name__ == "__main__":
    main()
