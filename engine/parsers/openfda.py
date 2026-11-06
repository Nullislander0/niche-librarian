"""openFDA (api.fda.gov): FDA adverse event reports, drug shortages and recalls. Public domain.

Options (sources.yaml):
  drugs:                                   # term: a `signals` term; generic and/or brand names
    - {term: benzodiazepines, generic: [alprazolam, clonazepam, lorazepam, diazepam]}
    - {term: lipitor, brand: [LIPITOR]}    # brand: matched as the reporter named the product
                                           #   (openFDA's harmonized brand_name links reports by
                                           #   ingredient, so it would count every cyclosporine)
  feeds: [events, shortages, recalls]
  since: "2025-07"                         # first month of adverse-event counts
  reaction_top: 15
  misuse_reactions: 'DIVERSION|ABUSE|MISUSE|DEPENDENCE|OVERDOSE'   # counted as a measure

Documents (source_type "data"; numbers in extra.metrics for `librarian signals report`):
  - one per drug per month: adverse event reports received (FAERS), serious ones, misuse-type
    reactions and the top reactions. FAERS reports are unverified and don't show causation.
  - one per shortage listing (availability, reason, status) and one per recall.

Key: OPENFDA_API_KEY (optional). Without one openFDA allows 1,000 requests a day per IP and
count queries return at most 100 terms (so misuse-type reactions are summed over the top
100 reactions); a first run with 15 drugs over 16 months needs about 500 requests.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timezone
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document

API = "https://api.fda.gov/drug"
STABLE_AFTER = 120 * 86400     # a month's FAERS counts are re-checked until this long after it ends


def _or(field: str, names: list[str]) -> str:
    return "(" + " OR ".join(f'{field}:"{n}"' for n in names) + ")"


def _months(since: str, today: date) -> list[str]:
    y, m = map(int, since[:7].split("-"))
    out = []
    while (y, m) <= (today.year, today.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_bounds(month: str) -> tuple[str, str, int, int]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    last = date.fromordinal(end.toordinal() - 1)
    ts = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return start.strftime("%Y%m%d"), last.strftime("%Y%m%d"), ts(start), ts(end)


def _date(s: str | None) -> int | None:
    for fmt in ("%Y%m%d", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except (TypeError, ValueError):
            continue
    return None


class Parser:
    POSITION_KEY_PREFIXES = ("events:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.drugs = [d for d in o.get("drugs", []) if d.get("generic") or d.get("brand")]
        self.feeds = o.get("feeds", ["events", "shortages", "recalls"])
        self.since = str(o.get("since", "2025-07"))
        self.top = int(o.get("reaction_top", 15))
        self.misuse = re.compile(o.get("misuse_reactions", "DIVERSION|ABUSE|MISUSE|DEPENDENCE|OVERDOSE"), re.I)
        self.key = os.environ.get("OPENFDA_API_KEY")

    def _get(self, endpoint: str, params: dict, ttl: int = 86400) -> dict:
        self.ctx.check_budget()
        if self.key:
            params = {**params, "api_key": self.key}
        try:
            return self.ctx.fetcher.get_json(f"{API}/{endpoint}.json", params, ttl=ttl)
        except FetchError as e:
            if e.status == 404:          # openFDA's "No matches found!"
                return {"results": [], "meta": {"results": {"total": 0}}}
            raise

    def crawl(self) -> Iterator[Document]:
        for d in self.drugs:
            if "events" in self.feeds:
                yield from self._events(d)
            if "shortages" in self.feeds:
                yield from self._shortages(d)
            if "recalls" in self.feeds:
                yield from self._recalls(d)

    def _doc(self, doc_id, kind, title, content, when, term, metrics, url, **extra) -> Document:
        return Document(doc_id=f"{self.ctx.source.id}:{doc_id}", source=self.ctx.source.id,
                        source_type=self.ctx.source.source_type, kind=kind, url=url, title=title,
                        content_md=content, post_date=when, updated_date=when, tags=[term],
                        extra={"term": term, "metrics": metrics, **extra})

    @staticmethod
    def _names(d: dict) -> list[str]:
        return list(d.get("generic") or []) + list(d.get("brand") or [])

    @staticmethod
    def _query(d: dict, generic_field: str, brand_field: str) -> str:
        parts = ([_or(generic_field, d["generic"])] if d.get("generic") else []) + \
                ([_or(brand_field, d["brand"])] if d.get("brand") else [])
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    def _events(self, d: dict) -> Iterator[Document]:
        now = time.time()
        drug_q = self._query(d, "patient.drug.openfda.generic_name", "patient.drug.medicinalproduct")
        for month in _months(self.since, date.today()):
            key = f"events:{d['term']}:{month}"
            first, last, start_ts, end_ts = _month_bounds(month)
            if (self.ctx.state.get(key) or 0) > end_ts + STABLE_AFTER:
                continue
            q = f"{drug_q} AND receivedate:[{first} TO {last}]"
            ttl = 30 * 86400 if now > end_ts + STABLE_AFTER else 7 * 86400
            serious = {r["term"]: r["count"] for r in self._get("event", {"search": q, "count": "serious"}, ttl)["results"]}
            total = sum(serious.values())
            reactions = self._get("event", {"search": q, "count": "patient.reaction.reactionmeddrapt.exact",
                                            "limit": 1000 if self.key else 100},   # 100 is the keyless maximum
                                  ttl)["results"] if total else []
            misuse = sum(r["count"] for r in reactions if self.misuse.search(r["term"]))
            rows = "\n".join(f"| {r['term'].title()} | {r['count']} |" for r in reactions[:self.top])
            content = (f"FDA Adverse Event Reporting System (FAERS): {total} reports received in {month} that "
                       f"name {', '.join(self._names(d))}; {serious.get(1, 0)} serious. Reports naming misuse-type "
                       f"reactions (diversion, abuse, misuse, dependence, overdose): {misuse}.\n\n"
                       + (f"| reaction | reports |\n|---|---|\n{rows}\n\n" if rows else "")
                       + "FAERS reports are submitted voluntarily, are not verified, and do not establish that a "
                         "drug caused the reaction.")
            yield self._doc(f"events:{d['term']}:{month}", "report",
                            f"FDA adverse event reports: {d['term']}, {month} ({total} reports)", content,
                            start_ts, d["term"],
                            {"faers_reports": total, "faers_serious": serious.get(1, 0), "faers_misuse": misuse},
                            f"https://api.fda.gov/drug/event.json?search={q}", feed="events", month=month)
            self.ctx.state.set(key, int(now))

    def _shortages(self, d: dict) -> Iterator[Document]:
        res = self._get("shortages", {"search": self._query(d, "generic_name", "openfda.brand_name"),
                                      "limit": 100})["results"]
        for r in res:
            ident = r.get("package_ndc") or f"{r.get('generic_name', '')}:{r.get('initial_posting_date', '')}"
            when = _date(r.get("update_date")) or _date(r.get("initial_posting_date"))
            fields = [("Availability", r.get("availability")), ("Status", r.get("status")),
                      ("Reason", r.get("shortage_reason")), ("Presentation", r.get("presentation")),
                      ("Company", r.get("company_name")), ("Details", r.get("related_info")),
                      ("First posted", r.get("initial_posting_date")), ("Updated", r.get("update_date"))]
            content = "\n".join(f"- **{k}:** {v}" for k, v in fields if v)
            yield self._doc(f"shortage:{ident}", "shortage",
                            f"FDA drug shortage: {(r.get('generic_name') or d['term'])[:120]} ({r.get('availability') or r.get('status') or 'listed'})",
                            content, when, d["term"],
                            {"shortage_listings": 1,
                             "shortage_current": int(str(r.get("status", "")).lower().startswith("current"))},
                            "https://www.accessdata.fda.gov/scripts/drugshortages/", feed="shortages")

    def _recalls(self, d: dict) -> Iterator[Document]:
        since = self.since[:7].replace("-", "") + "01"
        q = f"{self._query(d, 'openfda.generic_name', 'openfda.brand_name')} AND report_date:[{since} TO 20991231]"
        for r in self._get("enforcement", {"search": q, "limit": 100, "sort": "report_date:desc"})["results"]:
            fields = [("Classification", r.get("classification")), ("Status", r.get("status")),
                      ("Reason", r.get("reason_for_recall")), ("Product", r.get("product_description")),
                      ("Firm", r.get("recalling_firm")), ("Quantity", r.get("product_quantity")),
                      ("Distribution", r.get("distribution_pattern"))]
            yield self._doc(f"recall:{r.get('recall_number')}", "recall",
                            f"FDA recall ({r.get('classification', '')}): {(r.get('product_description') or d['term'])[:110]}",
                            "\n".join(f"- **{k}:** {v}" for k, v in fields if v), _date(r.get("report_date")),
                            d["term"], {"recalls": 1}, "https://www.accessdata.fda.gov/scripts/ires/index.cfm",
                            feed="recalls")
