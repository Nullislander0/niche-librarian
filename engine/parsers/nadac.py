"""NADAC (National Average Drug Acquisition Cost) from data.medicaid.gov. Public data (CMS).

NADAC is what retail pharmacies pay for drugs, surveyed weekly, per unit (tablet, mL, ...).
Two datasets are read through the data.medicaid.gov query API:
  - "NADAC (National Average Drug Acquisition Cost) <year>": weekly price snapshots
  - "NADAC Comparison": each week's price changes (old and new price, percent, reason)

Options (sources.yaml):
  drugs:                                   # term: a `signals` term; match: text in the NDC description
    - {term: oxycodone, match: [OXYCODONE]}
  since: "2026-01-01"                      # price changes from this date
  levels_weeks: 8                          # weekly snapshots to backfill on the first run

Documents (source_type "data"; numbers in extra.metrics for `librarian signals report`):
  - "NADAC prices: <drug>, as of <date>": per product (description), median price per unit,
    generic or brand. Metrics: products, median price per unit (most common unit: EA, ML, GM)
    and median generic price per tablet.
  - "NADAC price changes: <drug>, week of <date>": increases and decreases that week.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

from engine.parsers.base import CrawlContext, Document

BASE = "https://data.medicaid.gov/api/1"
PAGE = 500


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc).timestamp())


def _monday(s: str) -> str:
    d = date.fromisoformat(s[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


class Parser:
    POSITION_KEY_PREFIXES = ("levels:", "changes:")

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.drugs = [d for d in o.get("drugs", []) if d.get("match")]
        self.since = str(o.get("since", "2026-01-01"))
        self.levels_weeks = int(o.get("levels_weeks", 8))
        self._datasets: dict[str, str] | None = None

    def datasets(self) -> dict[str, str]:
        """{"2026": id, ..., "comparison": id} from the catalog (identifiers change each year)."""
        if self._datasets is None:
            self.ctx.check_budget()
            items = self.ctx.fetcher.get_json(f"{BASE}/metastore/schemas/dataset/items", ttl=7 * 86400)
            out = {}
            for i in items:
                t = i.get("title", "")
                if t.startswith("NADAC (National Average Drug Acquisition Cost) "):
                    out[t.rsplit(" ", 1)[-1]] = i["identifier"]
                elif t == "NADAC Comparison":
                    out["comparison"] = i["identifier"]
            self._datasets = out
        return self._datasets

    def _query(self, dataset: str, match: str, date_field: str, since: str, ttl: int = 86400) -> list[dict]:
        rows, offset = [], 0
        while True:
            self.ctx.check_budget()
            d = self.ctx.fetcher.get_json(f"{BASE}/datastore/query/{dataset}/0", {
                "limit": PAGE, "offset": offset,
                "conditions[0][property]": "ndc_description", "conditions[0][value]": f"%{match}%",
                "conditions[0][operator]": "LIKE",
                "conditions[1][property]": date_field, "conditions[1][value]": since,
                "conditions[1][operator]": ">=",
                "sorts[0][property]": date_field, "sorts[0][order]": "desc"}, ttl=ttl)
            rows += d.get("results") or []
            offset += PAGE
            if offset >= int(d.get("count") or 0) or not d.get("results"):
                return rows

    def crawl(self) -> Iterator[Document]:
        ds = self.datasets()
        today = date.today()
        levels_since = (today - timedelta(weeks=self.levels_weeks)).isoformat()
        for drug in self.drugs:
            yield from self._levels(drug, ds, levels_since, today)
            if "comparison" in ds:
                yield from self._changes(drug, ds["comparison"])

    def _doc(self, doc_id, kind, title, content, when, term, metrics, url) -> Document:
        return Document(doc_id=f"{self.ctx.source.id}:{doc_id}", source=self.ctx.source.id,
                        source_type=self.ctx.source.source_type, kind=kind, url=url, title=title,
                        content_md=content, post_date=when, updated_date=when, tags=[term],
                        extra={"term": term, "metrics": metrics})

    def _levels(self, drug: dict, ds: dict, since: str, today: date) -> Iterator[Document]:
        rows = []
        for year in sorted({since[:4], str(today.year)}):
            if year in ds:
                for m in drug["match"]:
                    rows += self._query(ds[year], m, "as_of_date", since)
        by_week: dict[str, dict] = defaultdict(dict)
        for r in rows:   # overlapping patterns (CITALOPRAM / ESCITALOPRAM) return a row twice
            by_week[r["as_of_date"][:10]].setdefault(r.get("ndc"), r)
        for as_of, by_ndc in sorted(by_week.items()):
            rs = list(by_ndc.values())
            key = f"levels:{drug['term']}:{as_of}"
            if self.ctx.state.get(key):
                continue
            products: dict[tuple, list] = defaultdict(list)
            for r in rs:
                try:
                    products[(r["ndc_description"], r["classification_for_rate_setting"], r["pricing_unit"])].append(
                        float(r["nadac_per_unit"]))
                except (KeyError, ValueError):
                    continue
            table = [(desc, cls, unit, statistics.median(p)) for (desc, cls, unit), p in products.items()]
            generic = [p for _, cls, unit, p in table if cls == "G" and unit == "EA"]
            units = [u for _, _, u, _ in table]
            main_unit = max(set(units), key=units.count) if units else None
            all_main = [p for _, _, unit, p in table if unit == main_unit]
            lines = "\n".join(f"| {desc} | {'generic' if cls == 'G' else 'brand' if cls == 'B' else cls} | "
                              f"${p:.4f} / {unit} |" for desc, cls, unit, p in sorted(table))
            content = (f"National Average Drug Acquisition Cost (what retail pharmacies pay), survey as of "
                       f"{as_of}, products matching {', '.join(drug['match'])}.\n\n| product | type | price |\n"
                       f"|---|---|---|\n{lines}")
            yield self._doc(f"levels:{drug['term']}:{as_of}", "price", f"NADAC prices: {drug['term']}, as of {as_of}",
                            content, _ts(as_of), drug["term"],
                            {"nadac_products": len(table),
                             "nadac_median_unit_price": round(statistics.median(all_main), 4) if all_main else None,
                             "nadac_median_generic_unit_price": round(statistics.median(generic), 4) if generic else None},
                            "https://data.medicaid.gov/dataset/" + ds.get(as_of[:4], ""))
            self.ctx.state.set(key, 1)

    def _changes(self, drug: dict, dataset: str) -> Iterator[Document]:
        rows = []
        for m in drug["match"]:
            rows += self._query(dataset, m, "effective_date", self.since)
        by_week: dict[str, dict] = defaultdict(dict)
        for r in rows:   # one change per product per week (several NDCs share a description)
            by_week[_monday(r["effective_date"])].setdefault((r["ndc_description"], r["effective_date"]), r)
        this_week = _monday(date.today().isoformat())
        for week, changes in sorted(by_week.items()):
            key = f"changes:{drug['term']}:{week}"
            if self.ctx.state.get(key):
                continue
            pct = []
            for r in changes.values():
                try:
                    pct.append(float(r["percent_change"]))
                except (KeyError, ValueError):
                    pass
            up, down = sum(p > 0 for p in pct), sum(p < 0 for p in pct)
            lines = "\n".join(
                f"| {r['ndc_description']} | ${float(r['old_nadac_per_unit']):.4f} | ${float(r['new_nadac_per_unit']):.4f} | "
                f"{r['percent_change']}% | {r.get('primary_reason', '')} |"
                for r in sorted(changes.values(), key=lambda r: -abs(float(r.get("percent_change") or 0))))
            content = (f"NADAC price changes for {drug['term']} effective the week of {week}: {up} increases, "
                       f"{down} decreases.\n\n| product | old / unit | new / unit | change | reason |\n"
                       f"|---|---|---|---|---|\n{lines}")
            yield self._doc(f"changes:{drug['term']}:{week}", "price_change",
                            f"NADAC price changes: {drug['term']}, week of {week} (+{up} / -{down})", content,
                            _ts(week), drug["term"],
                            {"nadac_increases": up, "nadac_decreases": down,
                             "nadac_median_change_pct": round(statistics.median(pct), 2) if pct else None},
                            "https://data.medicaid.gov/dataset/" + dataset)
            if week < this_week:      # the current week may still gain changes
                self.ctx.state.set(key, 1)
