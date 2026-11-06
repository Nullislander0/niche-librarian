"""Medicaid State Drug Utilization Data (data.medicaid.gov, CMS): prescriptions per drug per quarter.

States report every Medicaid outpatient prescription reimbursed, by NDC and quarter, for
fee-for-service and managed care. Counts under 11 are suppressed (shown as empty). The
national rows (state "XX") are read by default.

Options (sources.yaml):
  drugs:                                   # term: a `signals` term; product: product-name prefixes
    - {term: lipitor, product: [LIPITOR]}  #   (CMS truncates names to 10 characters)
  since_year: 2024
  states: [XX]                             # XX = national; add state codes for a breakdown

Documents (source_type "data"), one per drug per quarter (and state, if listed):
  "Medicaid prescriptions: lipitor, 2026 Q1 (1,234)", metrics medicaid_rx, medicaid_units,
  medicaid_reimbursed (before manufacturer rebates). Recent quarters are re-read because
  states revise them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Iterator

from engine.parsers.base import CrawlContext, Document

BASE = "https://data.medicaid.gov/api/1"
PAGE = 500
TITLE = "State Drug Utilization Data "


def _quarter_ts(year: int, q: int) -> int:
    return int(datetime(year, 3 * (q - 1) + 1, 1, tzinfo=timezone.utc).timestamp())


def _num(v) -> float:
    try:
        return float(v or 0)
    except ValueError:
        return 0.0


class Parser:
    POSITION_KEY_PREFIXES = ("quarter:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.drugs = [d for d in o.get("drugs", []) if d.get("product")]
        self.since = int(o.get("since_year", date.today().year - 2))
        self.states = list(o.get("states", ["XX"]))

    def datasets(self) -> dict[int, str]:
        self.ctx.check_budget()
        items = self.ctx.fetcher.get_json(f"{BASE}/metastore/schemas/dataset/items", ttl=7 * 86400)
        return {int(i["title"][len(TITLE):]): i["identifier"] for i in items
                if i.get("title", "").startswith(TITLE) and i["title"][len(TITLE):].isdigit()}

    def _rows(self, dataset: str, product: str, state: str) -> list[dict]:
        rows, offset = [], 0
        while True:
            self.ctx.check_budget()
            d = self.ctx.fetcher.get_json(f"{BASE}/datastore/query/{dataset}/0", {
                "limit": PAGE, "offset": offset,
                "conditions[0][property]": "product_name", "conditions[0][value]": f"{product[:10]}%",
                "conditions[0][operator]": "LIKE",
                "conditions[1][property]": "state", "conditions[1][value]": state, "conditions[1][operator]": "="},
                ttl=7 * 86400)
            rows += d.get("results") or []
            offset += PAGE
            if offset >= int(d.get("count") or 0) or not d.get("results"):
                return rows

    def crawl(self) -> Iterator[Document]:
        sets = self.datasets()
        this_year = date.today().year
        for drug in self.drugs:
            for state in self.states:
                for year in sorted(y for y in sets if y >= self.since):
                    key = f"quarter:{drug['term']}:{state}:{year}"
                    if year < this_year - 1 and self.ctx.state.get(key):
                        continue      # older years are final
                    rows = [r for p in drug["product"] for r in self._rows(sets[year], p, state)]
                    yield from self._quarters(drug, state, rows)
                    self.ctx.state.set(key, 1)

    def _quarters(self, drug: dict, state: str, rows: list[dict]) -> Iterator[Document]:
        seen, agg = set(), defaultdict(lambda: {"rx": 0.0, "units": 0.0, "paid": 0.0, "suppressed": 0, "ndcs": set()})
        for r in rows:
            k = (r.get("ndc"), r.get("year"), r.get("quarter"), r.get("utilization_type"))
            if k in seen:                     # overlapping product prefixes
                continue
            seen.add(k)
            a = agg[(int(r["year"]), int(r["quarter"]))]
            a["rx"] += _num(r.get("number_of_prescriptions"))
            a["units"] += _num(r.get("units_reimbursed"))
            a["paid"] += _num(r.get("total_amount_reimbursed"))
            a["suppressed"] += str(r.get("suppression_used")).lower() == "true"
            a["ndcs"].add(r.get("ndc"))
        where = "nationally" if state == "XX" else f"in {state}"
        for (y, q), a in sorted(agg.items()):
            rx = int(a["rx"])
            content = (f"Medicaid outpatient prescriptions for {drug['term']} ({', '.join(drug['product'])}) {where}, "
                       f"{y} Q{q}: {rx:,} prescriptions, {a['units']:,.0f} units, ${a['paid']:,.0f} reimbursed "
                       f"before manufacturer rebates, across {len(a['ndcs'])} NDCs."
                       + (f" {a['suppressed']} rows were suppressed (fewer than 11 prescriptions), so the "
                          f"total is a floor." if a["suppressed"] else ""))
            yield Document(
                doc_id=f"{self.ctx.source.id}:{drug['term']}:{state}:{y}Q{q}", source=self.ctx.source.id,
                source_type=self.ctx.source.source_type, kind="utilization",
                url="https://data.medicaid.gov/dataset/state-drug-utilization-data",
                title=f"Medicaid prescriptions: {drug['term']}, {y} Q{q} ({rx:,})" + ("" if state == "XX" else f", {state}"),
                content_md=content, post_date=_quarter_ts(y, q), tags=[drug["term"]],
                extra={"term": drug["term"], "state": state,
                       "metrics": {"medicaid_rx": rx, "medicaid_units": round(a["units"]),
                                   "medicaid_reimbursed": round(a["paid"])}})
