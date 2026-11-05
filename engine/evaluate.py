"""Known-item retrieval eval: can search re-find threads that have an accepted answer?

build: pick solved threads and save them to data/eval/<pack>.jsonl (local; derived content)
       with a rewritten query per thread: by a local LLM (--rewrite ollama), or left blank
       (--rewrite none) to be filled in by hand or by another model.
run:   search each rewritten query and record where the original thread lands.
       Reports recall@1/5/10 and MRR, overall and per source, so ranking or synonym
       changes can be compared run to run.

No domain expert needed: the ground truth is "this thread was solved".
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.request
from pathlib import Path

from engine.config import DATA_DIR, EMBEDDING, Pack
from engine.search import Searcher

log = logging.getLogger("eval")

EVAL_MODEL = os.environ.get("LIBRARIAN_EVAL_MODEL", "qwen3:4b")

PROMPT = """Rewrite this forum question as the short search query a different person with the same \
problem would type into a search box. Use your own words: do not copy the title's phrasing, \
keep any product names, version numbers and error codes. Reply with the query only.

Title: {title}
Question: {body}"""


def eval_path(pack: Pack) -> Path:
    return DATA_DIR / "eval" / f"{pack.id}.jsonl"


def _rewrite(title: str, body: str) -> str:
    req = urllib.request.Request(
        f"{EMBEDDING['url']}/api/chat",
        data=json.dumps({"model": EVAL_MODEL, "stream": False, "think": False,
                         "options": {"temperature": 0.3, "num_predict": 120},
                         "messages": [{"role": "user", "content": PROMPT.format(title=title, body=body[:1500])}]}
                        ).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = json.loads(r.read())["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"')
    if not text:
        raise RuntimeError(f"{EVAL_MODEL} returned only reasoning; use a non-thinking model "
                           f"(LIBRARIAN_EVAL_MODEL) or --rewrite none")
    return text.splitlines()[0][:300]


def build(pack: Pack, n: int = 200, seed: int = 7, since: str = "2018-01-01", rewrite: str = "ollama") -> dict:
    s = Searcher(pack)
    db = s.store.db
    since_ts = int(time.mktime(time.strptime(since, "%Y-%m-%d")))
    rows = db.execute("""
        SELECT q.doc_id, q.source, q.title, q.content_md,
               MAX(a.is_vendor_staff) AS staff, MAX(a.score) AS best
        FROM documents q JOIN documents a ON a.parent_id = q.doc_id AND a.is_accepted = 1
        WHERE q.parent_id IS NULL AND q.post_date >= ? AND length(q.content_md) > 80
        GROUP BY q.doc_id""", (since_ts,)).fetchall()
    rng = random.Random(seed)
    by_source: dict[str, list] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    picks = []
    per = max(1, n // max(1, len(by_source)))
    for src, items in by_source.items():  # balance sources so the big one doesn't dominate
        rng.shuffle(items)
        items.sort(key=lambda r: -(r["staff"] or 0))  # stable: staff-accepted first, still shuffled within
        picks += items[:per]
    path = eval_path(pack)
    path.parent.mkdir(parents=True, exist_ok=True)
    made = 0
    with path.open("w", encoding="utf-8") as f:
        for r in picks:
            try:
                query = _rewrite(r["title"], r["content_md"]) if rewrite == "ollama" else ""
            except (OSError, RuntimeError) as e:
                log.error("rewrite failed (is Ollama running with %s?): %s", EVAL_MODEL, e)
                break
            f.write(json.dumps({"target": r["doc_id"], "source": r["source"], "title": r["title"],
                                "query": query, "staff_accepted": bool(r["staff"]),
                                "excerpt": re.sub(r"\s+", " ", r["content_md"])[:200]}) + "\n")
            f.flush()
            made += 1
            if made % 20 == 0:
                log.info("%d/%d queries written", made, len(picks))
    return {"candidates": len(rows), "written": made, "path": str(path)}


def run(pack: Pack, k: int = 10, label: str = "", reranker: dict | None = None) -> dict:
    """reranker overrides ranking.yaml's `reranker` block for this run ({} turns it off)."""
    s = Searcher(pack)
    if reranker is not None:
        from engine.rank.cross import CrossReranker
        s.cross = CrossReranker.from_config(reranker)
    t0 = time.time()
    cases = [json.loads(line) for line in eval_path(pack).read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [c for c in cases if c.get("query")]  # blank = not yet rewritten
    ranks, per_source, misses = [], {}, []
    for c in cases:
        res = s.search(c["query"], limit=k)
        rank = None
        for i, r in enumerate(res["results"], 1):
            root = s.store.get_document(r["chunk_id"].split("#")[0]) or {}
            if (root.get("parent_id") or root.get("doc_id")) == c["target"]:
                rank = i
                break
        ranks.append(rank)
        per_source.setdefault(c["source"], []).append(rank)
        if rank is None:
            misses.append({"query": c["query"], "title": c["title"]})

    def summary(rs):
        n = len(rs) or 1
        return {"n": len(rs),
                **{f"recall@{x}": round(sum(1 for r in rs if r and r <= x) / n, 3) for x in (1, 5, k)},
                "mrr": round(sum(1 / r for r in rs if r) / n, 3)}

    out = {"label": label, "date": time.strftime("%Y-%m-%d %H:%M"), "overall": summary(ranks),
           "reranker": (s.cross.__dict__ if s.cross else None),
           "seconds_per_query": round((time.time() - t0) / max(1, len(cases)), 2),
           "by_source": {src: summary(rs) for src, rs in per_source.items()}}
    with (eval_path(pack).with_suffix(".results.jsonl")).open("a", encoding="utf-8") as f:
        f.write(json.dumps(out) + "\n")
    out["sample_misses"] = misses[:8]
    return out
