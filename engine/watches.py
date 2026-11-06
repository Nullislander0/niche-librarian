"""Saved searches ("watches") with alerts: what's new since the last run?

A watch is a stored query (plus optional version/source filters and a minimum score).
`run` searches only documents that entered the index since that watch last ran, writes a
markdown report, and remembers the run time. Watches are personal, so they live under
data/watches/ (git-ignored), never in the pack.

    librarian watch add smallworld "Smallworld migration to utility network" --min-score 0.6
    librarian crawl --watch          # crawl, then report new matches
    librarian watch run --notify     # or run on their own; --notify pops a desktop notice

Web watches (`watch add NAME "query" --web`) run the query through a live web search API
(Brave) instead of the local index. Per Brave's terms their results are only displayed:
never written to the index or to report files. New-vs-seen is tracked by URL fingerprints.

Release watches (`watch add-releases`) are web watches on Brave's news index, one per product
listed under `web.releases` in sources.yaml: new versions, patches, security advisories and
end-of-support notices (override the words with `web.release_terms`, e.g. for drugs). A
result only counts if the product's name (or its `match` regex) appears in its title or snippet.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from engine.config import DATA_DIR, Pack
from engine.search import Searcher, fmt_date

DEFAULT_LOOKBACK_DAYS = 14   # a new watch's first run looks back this far


def _path(pack: Pack) -> Path:
    return DATA_DIR / "watches" / f"{pack.id}.json"


def load(pack: Pack) -> dict:
    p = _path(pack)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save(pack: Pack, watches: dict) -> None:
    p = _path(pack)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(watches, indent=2, ensure_ascii=False), encoding="utf-8")


def add(pack: Pack, name: str, query: str, version: str | None = None, source_type: str | None = None,
        source: str | None = None, min_score: float = 0.0, limit: int = 20, web: bool = False,
        backend: str = "brave", freshness: str | None = "pw", site: str | None = None,
        require: str | None = None) -> dict:
    watches = load(pack)
    w = {"query": query, "limit": limit, "last_run": None, "created": int(time.time())}
    if web:
        w.update(web=True, backend=backend, freshness=freshness, site=site, seen=[], require=require)
    else:
        w.update(version=version, source_type=source_type, source=source, min_score=min_score)
    watches[name] = w
    save(pack, watches)
    return watches[name]


RELEASE_TERMS = 'release OR patch OR update OR "end of support" OR vulnerability'


def add_releases(pack: Pack, freshness: str = "pw") -> dict[str, dict]:
    """One news watch per product under `web.releases` in sources.yaml. Existing release
    watches are updated in place and keep their seen-URL history."""
    existing = load(pack)
    added = {}
    for r in pack.web.get("releases") or []:
        r = {"name": r} if isinstance(r, str) else r
        name = "release-" + re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
        query = r.get("query") or f'"{r["name"]}" {pack.web.get("release_terms") or RELEASE_TERMS}'
        require = r.get("match") or re.escape(r["name"])
        seen = (existing.get(name) or {}).get("seen") or []
        added[name] = add(pack, name, query, web=True, backend="brave-news", freshness=freshness,
                          limit=20, require=require)
        if seen:
            ws = load(pack)
            ws[name]["seen"] = seen
            save(pack, ws)
    return added


def remove(pack: Pack, name: str) -> bool:
    watches = load(pack)
    if watches.pop(name, None) is None:
        return False
    save(pack, watches)
    return True


SEEN_CAP = 5000   # URL fingerprints remembered per web watch


def run(pack: Pack, names: list[str] | None = None, since: str | None = None, dry_run: bool = False,
        searcher: Searcher | None = None, now: float | None = None, web_client=None,
        local: bool = True, web: bool = True) -> dict:
    """Run watches. Returns {"report", "results" (local, also written to the report), "web"
    (shown; also indexed if the pack declares storage rights), "stored", "errors"}."""
    watches = load(pack)
    selected = {n: w for n, w in watches.items() if not names or n in names}
    local_w = {n: w for n, w in selected.items() if not w.get("web")} if local else {}
    web_w = {n: w for n, w in selected.items() if w.get("web")} if web else {}
    now = now or time.time()
    results: dict[str, list] = {}
    web_results: dict[str, list] = {}
    errors: dict[str, str] = {}
    stored: dict[str, int] = {}
    for name, w in web_w.items():
        try:
            client = web_client or _web_backend(w.get("backend", "brave"))
            hits = client.search(w["query"], count=w.get("limit", 20), freshness=w.get("freshness"),
                                 site=w.get("site"))
        except Exception as e:  # missing key, quota, network: report and carry on
            errors[name] = str(e)
            continue
        seen = set(w.get("seen") or [])
        new = [h for h in hits if _fp(h["url"]) not in seen]
        if w.get("require"):   # e.g. the product name must appear: drops loosely related news
            req = re.compile(w["require"], re.I)
            new = [h for h in new if req.search(f"{h['title']} {h['snippet']}")]
        web_results[name] = new
        if not dry_run and new:
            from engine import webresults
            if webresults.enabled(pack):
                try:
                    stored[name] = webresults.store(pack, new, w["query"], name)
                except Exception as e:  # e.g. embedding server down: keep the watch running
                    errors[name] = f"results shown but not stored: {e}"
        if not dry_run:
            w["seen"] = (list(w.get("seen") or []) + [_fp(h["url"]) for h in new])[-SEEN_CAP:]
            w["last_run"] = int(now)
    s = (searcher or Searcher(pack)) if local_w else None
    for name, w in local_w.items():
        start = (int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp()) if since
                 else (w["last_run"] + 1) if w.get("last_run")     # strictly after the last run
                 else int(now - DEFAULT_LOOKBACK_DAYS * 86400))
        res = s.search(w["query"], limit=w.get("limit", 20), version=w.get("version"),
                       source_type=w.get("source_type"), source=w.get("source"), new_since=start)
        hits = [r for r in res["results"] if r["score"] >= w.get("min_score", 0)]
        results[name] = hits
        if not dry_run:
            w["last_run"] = int(now)
    report = None
    if not dry_run:
        save(pack, watches)
        if local_w:
            report = _write_report(pack, local_w, results, now)
    return {"report": str(report) if report else None, "results": results, "web": web_results,
            "errors": errors, "stored": stored}


def _web_backend(name: str):
    from engine.websearch import backend
    return backend(name)


def _fp(url: str) -> str:
    from engine.websearch import fingerprint
    return fingerprint(url)


def _write_report(pack: Pack, watches: dict, results: dict, now: float) -> Path:
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    lines = [f"# New matches: {pack.name}", f"_Run {stamp}_", ""]
    for name, hits in results.items():
        w = watches[name]
        lines.append(f"## {name}: {len(hits)} new  \n`{w['query']}`")
        for r in hits:
            flags = f" · {', '.join(r['flags'])}" if r["flags"] else ""
            vers = f" · v {', '.join(r['versions'])}" if r["versions"] else ""
            lines.append(f"- **{r['title']}** ({r['source']}/{r['kind']}, {r['date']}{vers}{flags})  \n"
                         f"  {r['url']}  \n  `{r['chunk_id']}`: {r['snippet'][:200]}")
        lines.append("")
    path = DATA_DIR / "watches" / f"{pack.id}-{datetime.fromtimestamp(now).strftime('%Y%m%d-%H%M')}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def notify(summary: str, body: str) -> bool:
    """Best-effort desktop notification (Linux notify-send, macOS osascript). Opt-in only."""
    try:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "--app-name=niche-librarian", summary, body], check=False, timeout=10)
            return True
        if shutil.which("osascript"):
            subprocess.run(["osascript", "-e", f'display notification {json.dumps(body)} with title {json.dumps(summary)}'],
                           check=False, timeout=10)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


__all__ = ["add", "remove", "load", "run", "notify", "fmt_date"]
