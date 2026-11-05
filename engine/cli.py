"""librarian: crawl, embed, search, fetch, stats, serve."""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys

from engine.config import load_pack


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="librarian", description="Local niche search index for AI agents.")
    p.add_argument("--pack", default=os.environ.get("LIBRARIAN_PACK", "esri_enterprise"),
                   help="topic pack under packs/ (default: $LIBRARIAN_PACK or %(default)s)")
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
    f.add_argument("--max-tokens", type=int)

    eb = sub.add_parser("eval-build", parents=[common], help="build the known-item eval set (local LLM)")
    eb.add_argument("-n", type=int, default=200)
    eb.add_argument("--rewrite", choices=["ollama", "none"], default="ollama",
                    help="'none' leaves queries blank to fill in by hand or another model")
    ev = sub.add_parser("eval", parents=[common], help="run the known-item eval: recall@k and MRR")
    ev.add_argument("--label", default="", help="note stored with the result, e.g. 'synonyms on'")
    ev.add_argument("--reranker", help="cross-encoder model to test (overrides ranking.yaml); 'off' disables")
    ev.add_argument("--rerank-candidates", type=int, default=20)
    ev.add_argument("--alpha", type=float, default=0.7)

    sub.add_parser("stats", parents=[common], help="index statistics")
    sub.add_parser("sources", parents=[common], help="the pack's sources and how each is accessed")
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

    # Long jobs hold a "stay awake" request so the machine doesn't suspend mid-run.
    long_job = a.cmd in ("crawl", "rebuild", "embed", "eval", "eval-build")
    from engine.awake import stay_awake
    with stay_awake(f"librarian {a.cmd} ({a.pack})") if long_job else contextlib.nullcontext():
        if a.cmd == "crawl":
            from engine.indexer import crawl
            print(json.dumps(crawl(pack, a.source, a.max_requests, embed=not a.no_embed), indent=2))
        elif a.cmd == "rebuild":
            from engine.indexer import crawl
            print(json.dumps(crawl(pack, a.source, offline=True), indent=2))
        elif a.cmd == "eval-build":
            from engine.evaluate import build
            print(json.dumps(build(pack, a.n, rewrite=a.rewrite), indent=2))
        elif a.cmd == "eval":
            from engine.evaluate import run
            rr = None
            if a.reranker:
                rr = {} if a.reranker == "off" else {"model": a.reranker, "candidates": a.rerank_candidates,
                                                     "alpha": a.alpha}
            print(json.dumps(run(pack, label=a.label, reranker=rr), indent=2))
        elif a.cmd == "retag":
            from engine.indexer import retag
            print(json.dumps(retag(pack), indent=2))
        elif a.cmd == "sources":
            print(f"| source | type | parser | access | robots | auth | verdict | checked |\n|---|---|---|---|---|---|---|---|")
            for src in pack.sources:
                ac = src.access
                print(f"| {src.id} | {src.source_type} | {src.parser} | {ac.get('method', '?')} | {ac.get('robots', '?')} "
                      f"| {ac.get('auth', 'none')} | {ac.get('verdict', '?')} | {ac.get('checked', '?')} |")
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
            print(Searcher(pack).fetch(a.chunk_id, a.scope, a.max_tokens) or f"not found: {a.chunk_id}")


if __name__ == "__main__":
    main()
