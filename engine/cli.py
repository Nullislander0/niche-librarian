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
    c.add_argument("--sample", type=int, metavar="N",
                   help="stop each source after N threads/pages: a quick end-to-end test of a pack")
    c.add_argument("--watch", action="store_true", help="after crawling, run saved searches (see `watch`)")
    c.add_argument("--notify", action="store_true", help="with --watch: desktop notification for new matches")

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
    s.add_argument("--new-since", help="only documents first indexed on/after this date (YYYY-MM-DD)")

    w = sub.add_parser("watch", parents=[common], help="saved searches: new matches since the last run")
    wsub = w.add_subparsers(dest="watch_cmd", required=True)
    wa = wsub.add_parser("add", help="save a search")
    wa.add_argument("name")
    wa.add_argument("query")
    wa.add_argument("--version")
    wa.add_argument("--source-type")
    wa.add_argument("--source")
    wa.add_argument("--min-score", type=float, default=0.0)
    wa.add_argument("--limit", type=int, default=20)
    wa.add_argument("--web", action="store_true", help="search the live web (Brave API) instead of the index")
    wa.add_argument("--backend", default="brave", help="web backend: brave (web pages) or brave-news")
    wa.add_argument("--freshness", default="pw", help="web: pd/pw/pm/py = past day/week/month/year")
    wa.add_argument("--site", help="web: restrict to one site, e.g. reddit.com")
    wa.add_argument("--require", help="web: regex that must appear in a result's title or snippet")
    wl = wsub.add_parser("add-releases", help="news watches for each product under web.releases in sources.yaml")
    wl.add_argument("--freshness", default="pw", help="pd/pw/pm/py (default: past week)")
    wsub.add_parser("list", help="list saved searches")
    wr = wsub.add_parser("remove", help="delete a saved search")
    wr.add_argument("name")
    wn = wsub.add_parser("run", help="report documents new since each search last ran")
    wn.add_argument("names", nargs="*")
    wn.add_argument("--since", help="override: look back to this date (YYYY-MM-DD)")
    wn.add_argument("--dry-run", action="store_true", help="don't record the run or write a report")
    wn.add_argument("--notify", action="store_true", help="desktop notification when there are new matches")
    wn.add_argument("--local-only", action="store_true", help="skip web watches")
    wn.add_argument("--web-only", action="store_true", help="only web watches")

    pg = sub.add_parser("pages", parents=[common], help="single web pages: review proposals, add or remove")
    pgs = pg.add_subparsers(dest="pages_cmd", required=True)
    pgs.add_parser("list", help="pages waiting for approval, and approved pages")
    pa = pgs.add_parser("approve", help="approve proposed pages and index them")
    pa.add_argument("urls", nargs="*", help="default: all pending")
    pa.add_argument("--no-index", action="store_true", help="approve only; index on the next crawl")
    pj = pgs.add_parser("reject", help="reject proposed pages (they won't be proposed again)")
    pj.add_argument("urls", nargs="*", help="default: all pending")
    pd = pgs.add_parser("add", help="add a page yourself (approved and indexed right away)")
    pd.add_argument("url")
    pd.add_argument("--note")
    pd.add_argument("--force", action="store_true", help="even if a source already crawls its host")
    pr = pgs.add_parser("remove", help="remove an approved page from the index")
    pr.add_argument("url")
    pgs.add_parser("read", help="print a page as the agent's read_url sees it").add_argument("url")

    wb = sub.add_parser("web", parents=[common], help="search the live web (Brave API); results shown, not stored")
    wb.add_argument("query")
    wb.add_argument("--site", help="domain/path, or a shortcut from the pack's web.sites")
    wb.add_argument("--freshness", help="pd/pw/pm/py = past day/week/month/year")
    wb.add_argument("--count", type=int, default=10)
    wb.add_argument("--news", action="store_true", help="search Brave's news index instead")

    sc = sub.add_parser("scout", parents=[common], help="find forums for a topic and draft sources.yaml entries")
    sc.add_argument("topic")
    sc.add_argument("--max-sites", type=int, default=12, help="sites to visit (one robots.txt + one page each)")
    sc.add_argument("--query", action="append", help="an extra search query (repeatable)")
    sc.add_argument("--out", help="also write the report (markdown) to this file")

    sg = sub.add_parser("signals", parents=[common], help="count what documents say about tracked terms")
    sgs = sg.add_subparsers(dest="signals_cmd", required=True)
    sr = sgs.add_parser("run", help="label new documents (local LLM, or keyword rules)")
    sr.add_argument("--source", action="append")
    sr.add_argument("--limit", type=int, help="label at most N documents this run")
    sr.add_argument("--keywords-only", action="store_true", help="skip the LLM; use the categories' keyword rules")
    sr.add_argument("--relabel", action="store_true", help="discard all labels first (after editing terms/categories)")
    sp = sgs.add_parser("report", help="counts by term, category and week")
    sp.add_argument("--term")
    sp.add_argument("--source")
    sp.add_argument("--by", choices=["week", "month"], default="week")
    sp.add_argument("--periods", type=int, default=8)
    sp.add_argument("--json", action="store_true")

    md = sub.add_parser("memo-data", parents=[common], help="report numbers: latest, previous, change, source")
    md.add_argument("--json", action="store_true")
    md.add_argument("--out", help="also write to this file")

    pv = sub.add_parser("privacy", parents=[common], help="apply the pack's privacy setting to documents already indexed")
    pv.add_argument("action", choices=["apply", "audit"], help="apply: pseudonymize what's indexed; "
                    "audit: check the index for leftover handles, profile links, emails, phone numbers")

    wr2 = sub.add_parser("web-results", parents=[common], help="stored web search results (storage-rights plans)")
    wr2.add_argument("action", choices=["count", "purge"])

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

    sub.add_parser("check", parents=[common], help="check the pack's sources.yaml for typos and bad values")
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
            print(json.dumps(crawl(pack, a.source, a.max_requests, embed=not a.no_embed, sample=a.sample),
                             indent=2))
            if a.watch:
                _run_watches(pack, None, None, False, a.notify)
        elif a.cmd == "watch":
            from engine import watches
            if a.watch_cmd == "add":
                print(json.dumps(watches.add(pack, a.name, a.query, a.version, a.source_type, a.source,
                                             a.min_score, a.limit, a.web, a.backend, a.freshness, a.site,
                                             a.require), indent=2))
            elif a.watch_cmd == "add-releases":
                added = watches.add_releases(pack, a.freshness)
                if not added:
                    print("no products under web.releases in this pack's sources.yaml")
                for name, wt in added.items():
                    print(f"{name}: {wt['query']}")
            elif a.watch_cmd == "list":
                for name, wt in watches.load(pack).items():
                    last = watches.fmt_date(wt.get("last_run")) if wt.get("last_run") else "never"
                    kind = (f"web/{wt.get('backend')} {wt.get('freshness') or ''}{' site:' + wt['site'] if wt.get('site') else ''}"
                            if wt.get("web") else f"index, min score {wt.get('min_score', 0)}")
                    print(f"{name}: {wt['query']!r} ({kind}; last run {last})")
            elif a.watch_cmd == "remove":
                print("removed" if watches.remove(pack, a.name) else f"no watch named {a.name!r}")
            elif a.watch_cmd == "run":
                _run_watches(pack, a.names, a.since, a.dry_run, a.notify, not a.web_only, not a.local_only)
        elif a.cmd == "pages":
            _pages(pack, a)
        elif a.cmd == "web":
            from engine import websearch
            from engine.indexer import open_store
            from engine.mcp_server.server import format_web_results
            sites = pack.web.get("sites") or {}
            hits = websearch.backend("brave-news" if a.news else "brave").search(a.query, a.count, a.freshness,
                                                     sites.get(a.site, a.site) if a.site else None)
            print(format_web_results(a.query, hits, pack, open_store(pack)))
            print(f"\nquota: {websearch.agent_budget()[1]}")
        elif a.cmd == "signals":
            from engine import signals
            from engine.indexer import open_store
            store = open_store(pack)
            if a.signals_cmd == "run":
                print(json.dumps(signals.run(pack, store, a.source, a.limit, keywords_only=a.keywords_only,
                                             relabel=a.relabel, log=lambda m: print(m, file=sys.stderr)), indent=2))
            else:
                agg = signals.aggregate(pack, store, a.by, a.periods, a.term, a.source)
                print(json.dumps(agg, indent=2) if a.json else signals.format_report(agg))
        elif a.cmd == "memo-data":
            from engine import memo_data
            from engine.indexer import open_store
            data = memo_data.build(pack, open_store(pack))
            text = json.dumps(data, indent=2) if a.json else memo_data.to_markdown(data)
            print(text)
            if a.out:
                with open(a.out, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
        elif a.cmd == "privacy":
            from engine.indexer import embed_missing, open_store
            from engine.privacy import apply_to_store, audit
            store = open_store(pack)
            if a.action == "audit":
                res = audit(pack, store)
                print(json.dumps(res, indent=2))
                if res.get("leaks"):
                    sys.exit(1)
                return
            res = apply_to_store(pack, store)
            res["embedded_chunks"] = embed_missing(store)
            print(json.dumps(res, indent=2))
        elif a.cmd == "web-results":
            from engine import webresults
            from engine.indexer import open_store
            if a.action == "purge":
                print(f"deleted {webresults.purge(pack)} stored web results")
            else:
                n = open_store(pack).db.execute("SELECT COUNT(*) FROM documents WHERE source=?",
                                                (webresults.SOURCE_ID,)).fetchone()[0]
                print(f"{n} stored web results (storage_rights: {webresults.enabled(pack)})")
        elif a.cmd == "scout":
            from engine import scout, websearch
            res = scout.run(a.topic, pack, max_sites=a.max_sites, extra_queries=a.query,
                            log=lambda m: print(m, file=sys.stderr))
            text = scout.report(res)
            print(text)
            if a.out:
                with open(a.out, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            print(f"\nquota: {websearch.agent_budget()[1]}", file=sys.stderr)
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
        elif a.cmd == "check":
            from engine.config import _read_yaml, check_meta, check_options
            from engine.parsers.base import load_parser
            problems = check_meta(_read_yaml(pack.path / "sources.yaml"))
            for src in pack.sources:
                try:
                    problems += check_options(src, load_parser(src.parser, pack.path))
                except ImportError as e:
                    problems.append(f"source {src.id}: no parser named {src.parser!r} ({e})")
            print("\n".join(problems) or f"{pack.id}: no problems found")
            if problems:
                sys.exit(1)
        elif a.cmd == "stats":
            from engine.search import Searcher
            print(json.dumps(Searcher(pack).info(), indent=2))
        elif a.cmd == "search":
            from engine.mcp_server.server import format_results
            from engine.search import Searcher
            res = Searcher(pack).search(a.query, a.limit, a.version, a.version_mode, a.date_from, a.date_to,
                                        a.source_type, a.source, a.explain, new_since=a.new_since)
            print(json.dumps(res, indent=2) if a.json else format_results(res))
        elif a.cmd == "fetch":
            from engine.search import Searcher
            print(Searcher(pack).fetch(a.chunk_id, a.scope, a.max_tokens) or f"not found: {a.chunk_id}")


def _pages(pack, a) -> None:
    from engine import webpages
    from engine.indexer import crawl, make_fetcher, open_store
    index = False
    if a.pages_cmd == "list":
        q = webpages.load(pack.id)
        print(f"Waiting for approval ({len(q['pending'])}):")
        for url, info in q["pending"].items():
            print(f"  {url}\n      {info.get('note') or '(no note)'}  [{info.get('proposed_by')}]")
        print(f"Approved ({len(q['approved'])}):")
        for url, info in q["approved"].items():
            print(f"  {url}" + (f"  ({info['note']})" if info.get("note") else ""))
    elif a.pages_cmd == "approve":
        done = webpages.approve(pack, a.urls or None)
        print(f"approved {len(done)}" + "".join(f"\n  {u}" for u in done))
        index = bool(done) and not a.no_index
    elif a.pages_cmd == "reject":
        done = webpages.reject(pack, a.urls or None)
        print(f"rejected {len(done)}" + "".join(f"\n  {u}" for u in done))
    elif a.pages_cmd == "add":
        msg = webpages.propose(pack, a.url, a.note, store=open_store(pack), by="user", force=a.force)
        if "Queued" not in msg:
            print(msg)
            return
        webpages.approve(pack, [a.url])
        index = True
    elif a.pages_cmd == "remove":
        print("removed" if webpages.remove(pack, a.url, open_store(pack)) else "not found")
    elif a.pages_cmd == "read":
        print(webpages.read_url(pack, a.url, make_fetcher(pack), open_store(pack)))
    if index:
        print(json.dumps(crawl(pack, [webpages.SOURCE_ID]), indent=2))


def _run_watches(pack, names, since, dry_run, notify, local=True, web=True) -> None:
    from engine import watches
    out = watches.run(pack, names or None, since, dry_run, local=local, web=web)
    for name, hits in out["results"].items():
        print(f"{name}: {len(hits)} new in the index")
        for r in hits[:5]:
            print(f"   {r['date']}  {r['source']}/{r['kind']}  {r['title'][:70]}  {r['url']}")
    for name, hits in out["web"].items():
        kept = out.get("stored", {}).get(name)
        print(f"{name}: {len(hits)} new on the web (Brave Search; "
              + (f"{kept} stored: plan has storage rights)" if kept is not None else "shown, not stored)"))
        hits = sorted(hits, key=lambda h: h["kind"] != "news")
        for h in hits:
            print(f"   [{h['kind']}] {h['title'][:80]}  {h['age']}\n      {h['url']}\n      {h['snippet'][:160]}")
    for name, err in out["errors"].items():
        print(f"{name}: skipped ({err})")
    if out["report"]:
        print(f"report (index results only): {out['report']}")
    total = sum(len(h) for h in out["results"].values()) + sum(len(h) for h in out["web"].values())
    if notify and total:
        counts = {**{n: len(h) for n, h in out["results"].items()}, **{n: len(h) for n, h in out["web"].items()}}
        watches.notify(f"niche-librarian: {total} new match(es)", ", ".join(f"{n} ({c})" for n, c in counts.items() if c))


if __name__ == "__main__":
    main()
