"""The numbers for a report, computed once from the index so a writing agent never does arithmetic.

    librarian memo-data                  # markdown scorecard with changes, periods and sources
    librarian memo-data --json           # the same, structured, for an agent

For every tracked term and every official-data measure (documents with `extra.metrics`):
the latest value, the previous one, the change, the period each covers, and the document id
to cite. FDA adverse-event months that ended less than FAERS_LAG_DAYS ago are skipped as
incomplete (FAERS fills in over the following weeks).
Forum sources add posts, posters and switch flows from `signals`.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone

FAERS_LAG_DAYS = 30        # after the end of the month
# Which measures to show per kind of document, in order; other measures are listed after.
SHOWN = {
    "utilization": ["medicaid_rx"],
    "coverage": ["partd_plans_pct", "partd_pa_pct", "partd_step_pct"],
    "price": ["nadac_median_unit_price"],
    "report": ["faers_reports", "faers_serious", "faers_misuse"],
}
LABELS = {
    "medicaid_rx": "Medicaid prescriptions", "partd_plans_pct": "Part D plans covering (%)",
    "partd_pa_pct": "Part D prior auth (% of covering)", "partd_step_pct": "Part D step therapy (% of covering)",
    "nadac_median_unit_price": "NADAC price per unit ($)", "faers_reports": "FDA adverse event reports",
    "faers_serious": "FDA reports, serious", "faers_misuse": "FDA reports, misuse-type",
    "medicaid_reimbursed": "Medicaid reimbursed, before rebates ($)", "medicaid_units": "Medicaid units",
    "partd_ql_pct": "Part D quantity limit (% of covering)", "partd_formularies": "Part D formularies",
    "nadac_median_generic_unit_price": "NADAC generic price per tablet ($)", "nadac_products": "NADAC products",
}


def _date(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def _period(kind: str, ts: int) -> str:
    d = datetime.fromtimestamp(ts, timezone.utc)
    if kind == "utilization":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    if kind == "report":
        return d.strftime("%Y-%m")
    return d.strftime("%Y-%m-%d")


def build(pack, store, now: float | None = None) -> dict:
    now = now or time.time()
    rows = store.db.execute(
        "SELECT doc_id, source, kind, post_date, extra FROM documents "
        "WHERE json_extract(extra, '$.metrics') IS NOT NULL AND post_date IS NOT NULL ORDER BY post_date").fetchall()
    series: dict[tuple, list] = defaultdict(list)       # (term, kind) -> [(ts, metrics, doc_id)]
    events: dict[str, dict] = defaultdict(lambda: {"shortages_current": 0, "shortage_docs": [], "recalls_12m": 0,
                                                   "recall_docs": []})
    for doc_id, source, kind, ts, extra in rows:
        e = json.loads(extra)
        term, metrics = e.get("term"), e.get("metrics") or {}
        if not term:
            continue
        if kind == "shortage":
            if metrics.get("shortage_current"):
                events[term]["shortages_current"] += 1
                events[term]["shortage_docs"].append(doc_id)
            continue
        if kind == "recall":
            if ts >= now - 365 * 86400:
                events[term]["recalls_12m"] += 1
                events[term]["recall_docs"].append(doc_id)
            continue
        if kind == "price_change" or (kind == "utilization" and e.get("state", "XX") != "XX"):
            continue
        if kind == "report":
            d = datetime.fromtimestamp(ts, timezone.utc)
            month_end = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc).timestamp()
            if month_end > now - FAERS_LAG_DAYS * 86400:
                continue                                   # FAERS month not complete yet
        series[(term, kind)].append((ts, metrics, doc_id))
    terms = sorted({t for t, _ in series} | set(events))
    out = {"pack": pack.id, "as_of": _date(int(now)), "faers_lag_days": FAERS_LAG_DAYS, "terms": {}}
    for term in terms:
        t = out["terms"].setdefault(term, {"measures": {}, "events": events.get(term)})
        for (tt, kind), pts in series.items():
            if tt != term or not pts:
                continue
            last = pts[-1]
            prev = pts[-2] if len(pts) > 1 else None
            keys = SHOWN.get(kind, []) + sorted(k for k in last[1] if k not in SHOWN.get(kind, []))
            for k in keys:
                v = last[1].get(k)
                if v is None:
                    continue
                p = prev[1].get(k) if prev else None
                change = None if p is None else round(v - p, 4)
                pct = None if not p else round(100 * (v - p) / p, 1)
                t["measures"][k] = {"label": LABELS.get(k, k), "kind": kind, "value": v,
                                    "period": _period(kind, last[0]), "previous": p,
                                    "previous_period": _period(kind, prev[0]) if prev else None,
                                    "change": change, "change_pct": pct, "cite": last[2],
                                    "cite_previous": prev[2] if prev else None}
    out["forum"] = _forum(pack, store)
    return out


def _forum(pack, store) -> dict:
    if not pack.signals.get("terms"):
        return {}
    from engine import signals
    agg = signals.aggregate(pack, store, by="month", periods=3)
    per_term = {r["term"]: {"posts": r["total"], "posters": r.get("posters")} for r in agg["rows"]
                if r["category"] == "(any mention)"}
    cats = defaultdict(dict)
    for r in agg["rows"]:
        if r["category"] != "(any mention)":
            cats[r["term"]][r["category"]] = {"posts": r["total"], "posters": r.get("posters")}
    forum_sources = [s.id for s in pack.sources if s.source_type == "forum"]
    totals = {}
    for sid in forum_sources:
        n, posters, last = store.db.execute("SELECT COUNT(*), COUNT(DISTINCT author), MAX(post_date) FROM documents "
                                            "WHERE source=?", (sid,)).fetchone()
        totals[sid] = {"posts": n, "posters": posters, "newest_post": _date(last)}
    return {"sources": totals, "terms": per_term, "categories": dict(cats), "switches": agg.get("switches", [])}


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{int(v):,}"


def _signed(v: float) -> str:
    return f"{v:+,.0f}" if float(v).is_integer() or abs(v) >= 100 else f"{v:+,.4f}".rstrip("0").rstrip(".")


def to_markdown(data: dict) -> str:
    lines = [f"# Report data: {data['pack']}", f"*As of {data['as_of']}. FDA months that ended less than "
             f"{data['faers_lag_days']} days ago are left out as incomplete. Cite the ids in brackets.*", ""]
    for term, t in data["terms"].items():
        if not t["measures"] and not t["events"]:
            continue
        lines += [f"## {term}", "", "| measure | latest | previous | change | source |", "|---|---|---|---|---|"]
        for k, m in t["measures"].items():
            chg = "–" if m["change"] is None else (_signed(m["change"]) + (f" ({m['change_pct']:+.1f}%)"
                                                                          if m["change_pct"] is not None else ""))
            lines.append(f"| {m['label']} | {_fmt(m['value'])} ({m['period']}) | "
                         f"{_fmt(m['previous'])}{' (' + m['previous_period'] + ')' if m['previous_period'] else ''} | "
                         f"{chg} | [{m['cite']}] |")
        ev = t["events"]
        if ev:
            lines.append(f"| Shortages listed as current | {ev['shortages_current']} | | | "
                         f"{'[' + ', '.join(ev['shortage_docs'][:3]) + ']' if ev['shortage_docs'] else ''} |")
            lines.append(f"| Recalls, last 12 months | {ev['recalls_12m']} | | | "
                         f"{'[' + ', '.join(ev['recall_docs'][:3]) + ']' if ev['recall_docs'] else ''} |")
        lines.append("")
    f = data.get("forum") or {}
    if f.get("sources"):
        lines += ["## Forums", ""]
        for sid, s in f["sources"].items():
            lines.append(f"- {sid}: {s['posts']:,} posts from {s['posters']:,} posters; newest post {s['newest_post']}")
        if f["terms"]:
            lines += ["", "| term | posts | posters |", "|---|---|---|"]
            lines += [f"| {k} | {v['posts']} | {v['posters']} |" for k, v in f["terms"].items()]
        if f["switches"]:
            lines += ["", "Switch flows (posters who mentioned one term, later another):", ""]
            lines += [f"- {s['from']} → {s['to']}: {s['posters']}" for s in f["switches"]]
    return "\n".join(lines)
