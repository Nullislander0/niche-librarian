"""Signals: count what the index's documents talk about, by term, category and week.

A pack defines `signals:` in sources.yaml:

    signals:
      terms:                                  # what to track; `match` is a regex (default: the name)
        - {name: oxycodone, match: 'oxycodone|oxycontin|percocet'}
      categories:                             # what is said about it; weight scales its count
        price: {weight: 1.0, description: "cost, price changes, coupons", keywords: 'price|cost|\\$\\d'}
        recreational: {weight: 0.5, description: "non-medical use", keywords: 'got high|recreational'}

`librarian signals run` labels each document that mentions a term with the categories it
discusses, using a local LLM through Ollama (LIBRARIAN_SIGNALS_MODEL, default qwen3:4b), or
the categories' `keywords` regexes with --keywords-only or when Ollama isn't running. Only
labels are stored (table signal_labels: doc id, terms, categories): no text, no authors.
`librarian signals report` (and the MCP `signals` tool) aggregates them by week or month.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

from engine.config import EMBEDDING, Pack

MODEL = os.environ.get("LIBRARIAN_SIGNALS_MODEL", "qwen3:4b")
SCHEMA = """CREATE TABLE IF NOT EXISTS signal_labels (
    doc_id TEXT PRIMARY KEY, content_hash TEXT, terms TEXT, categories TEXT, method TEXT, labeled_at INTEGER)"""

PROMPT = """You label posts for a research tally. Which of these topics does the text below \
actually discuss about {terms}? Only include a topic if the text says something about it.

{categories}

Reply with JSON only: {{"categories": ["topic_key", ...]}} (an empty list if none apply).

Text:
{text}"""


def _terms(pack: Pack) -> list[tuple[str, re.Pattern]]:
    out = []
    for t in pack.signals.get("terms") or []:
        t = {"name": t} if isinstance(t, str) else t
        out.append((t["name"], re.compile(rf"\b(?:{t.get('match') or re.escape(t['name'])})", re.I)))
    return out


def _categories(pack: Pack) -> dict[str, dict]:
    return {k: (v or {}) for k, v in (pack.signals.get("categories") or {}).items()}


def keyword_classify(text: str, categories: dict[str, dict]) -> list[str]:
    return [k for k, c in categories.items() if c.get("keywords") and re.search(c["keywords"], text, re.I)]


class OllamaClassifier:
    method = f"llm:{MODEL}"

    def __init__(self, categories: dict[str, dict], model: str = MODEL, url: str = EMBEDDING["url"]):
        self.categories, self.model, self.url = categories, model, url
        self.method = f"llm:{model}"

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=3) as r:
                return any(m["name"].split(":")[0] == self.model.split(":")[0] for m in json.load(r)["models"])
        except (OSError, ValueError, KeyError):
            return False

    def __call__(self, text: str, terms: list[str]) -> list[str]:
        cats = "\n".join(f"- {k}: {c.get('description', k)}" for k, c in self.categories.items())
        body = json.dumps({"model": self.model, "stream": False, "think": False, "format": "json",
                           "options": {"temperature": 0, "num_predict": 80},
                           "messages": [{"role": "user", "content": PROMPT.format(
                               terms=", ".join(terms), categories=cats, text=text[:2500])}]}).encode()
        req = urllib.request.Request(f"{self.url}/api/chat", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            content = json.loads(r.read())["message"]["content"]
        return parse_labels(content, self.categories)


def parse_labels(content: str, categories: dict) -> list[str]:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    try:
        got = json.loads(content).get("categories") or []
    except (ValueError, AttributeError):
        got = re.findall(r'"(\w+)"', content)
    return sorted({g for g in got if isinstance(g, str) and g in categories})


def run(pack: Pack, store, sources: list[str] | None = None, limit: int | None = None,
        classifier=None, keywords_only: bool = False, relabel: bool = False, log=lambda m: None) -> dict:
    """Label documents that mention a term and aren't labeled yet (or changed since).
    relabel=True starts over, e.g. after editing terms or categories."""
    terms, categories = _terms(pack), _categories(pack)
    if not terms or not categories:
        raise ValueError(f"pack {pack.id!r} has no signals: terms/categories in sources.yaml")
    db = store.db
    db.execute(SCHEMA)
    if relabel:
        db.execute("DELETE FROM signal_labels")
    if classifier is None and not keywords_only:
        llm = OllamaClassifier(categories)
        classifier = llm if llm.available() else None
        if classifier is None:
            log(f"Ollama model {MODEL} not available: using keyword rules")
    method = getattr(classifier, "method", "keywords")
    sql = ("SELECT d.doc_id, d.title, d.content_md, d.content_hash FROM documents d "
           "LEFT JOIN signal_labels l ON l.doc_id = d.doc_id "
           "WHERE (l.doc_id IS NULL OR l.content_hash != d.content_hash)")
    args: list = []
    if sources:
        sql += f" AND d.source IN ({','.join('?' * len(sources))})"
        args += sources
    seen = labeled = 0
    t0 = time.time()
    for doc_id, title, content, chash in db.execute(sql, args).fetchall():
        text = f"{title}\n{content}"
        hit = [name for name, rx in terms if rx.search(text)]
        cats: list[str] = []
        if hit:
            if limit is not None and labeled >= limit:
                break
            cats = classifier(text, hit) if classifier else keyword_classify(text, categories)
            labeled += 1
            if labeled % 25 == 0:
                log(f"labeled {labeled} ({time.time() - t0:.0f}s)")
        seen += 1
        db.execute("INSERT OR REPLACE INTO signal_labels VALUES (?,?,?,?,?,?)",
                   (doc_id, chash, json.dumps(hit), json.dumps(cats), method if hit else "no-term", int(time.time())))
        if seen % 100 == 0:
            db.commit()
    db.commit()
    return {"documents_checked": seen, "mentioning_a_term": labeled, "method": method,
            "seconds": round(time.time() - t0, 1)}


def _period(ts: int | None, by: str) -> str:
    if not ts:
        return "undated"
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.strftime("%Y-%m") if by == "month" else f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def aggregate(pack: Pack, store, by: str = "week", periods: int = 8, term: str | None = None,
              source: str | None = None) -> dict:
    """{"periods": [...], "rows": [{term, category, counts: {period: n}, total, weighted}]}."""
    store.db.execute(SCHEMA)
    categories = _categories(pack)
    sql = ("SELECT l.terms, l.categories, COALESCE(d.post_date, d.first_seen) FROM signal_labels l "
           "JOIN documents d ON d.doc_id = l.doc_id WHERE l.terms != '[]'")
    args: list = []
    if source:
        sql += " AND d.source = ?"
        args.append(source)
    counts: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    mentions: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for terms_j, cats_j, ts in store.db.execute(sql, args):
        p = _period(ts, by)
        for t in json.loads(terms_j):
            if term and t.lower() != term.lower():
                continue
            mentions[t][p] += 1
            for c in json.loads(cats_j):
                counts[(t, c)][p] += 1
    all_periods = sorted({p for m in mentions.values() for p in m if p != "undated"})[-periods:]
    rows = []
    for t, per in sorted(mentions.items(), key=lambda kv: -sum(kv[1].values())):
        rows.append({"term": t, "category": "(any mention)", "counts": {p: per.get(p, 0) for p in all_periods},
                     "total": sum(per.values()), "weighted": float(sum(per.values()))})
        for (t2, c), cper in sorted(counts.items(), key=lambda kv: -sum(kv[1].values())):
            if t2 != t:
                continue
            w = float(categories.get(c, {}).get("weight", 1.0))
            total = sum(cper.values())
            rows.append({"term": t, "category": c, "counts": {p: cper.get(p, 0) for p in all_periods},
                         "total": total, "weighted": round(total * w, 1)})
    return {"by": by, "periods": all_periods, "rows": rows}


def format_report(agg: dict) -> str:
    if not agg["rows"]:
        return "No signals yet: run `librarian signals run` after crawling."
    ps = agg["periods"]
    head = "| term | category | " + " | ".join(ps) + " | total | weighted |"
    lines = [f"Documents per {agg['by']} (labels only; weighted = total x category weight)", "",
             head, "|" + "---|" * (len(ps) + 4)]
    for r in agg["rows"]:
        lines.append(f"| {r['term'] if r['category'] == '(any mention)' else ''} | {r['category']} | "
                     + " | ".join(str(r["counts"][p] or "") for p in ps) + f" | {r['total']} | {r['weighted']} |")
    return "\n".join(lines)
