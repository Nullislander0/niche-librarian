"""Medicare Part D formulary coverage (CMS quarterly public use files, data.cms.gov).

Each quarter CMS publishes every Part D plan's formulary: for each drug (RxNorm concept),
its tier and whether prior authorization, step therapy or a quantity limit applies. The
release is one ~2.5 GB ZIP, almost all of it pharmacy-network and pricing files; this parser
reads the ZIP's directory with HTTP range requests and downloads only the formulary and plan
files (~9 MB), honouring data.cms.gov's crawl delay. The extracted files are
kept under data/cache/<pack>/partd/<release>/ so a rebuild needs no network.

Options (sources.yaml):
  drugs:                                   # term: a `signals` term
    - {term: lipitor, name: lipitor}       # brand name looked up in RxNorm (RxNav), or
    - {term: zocor, rxcui: ['196503']}     #   explicit RxNorm ids
  quarters: 2                              # how many recent releases to read on the first run

Documents (source_type "data"), one per drug per release:
  "Medicare Part D coverage: lipitor, 2026-09 (85.2% of plans)", metrics partd_plans_pct,
  partd_pa_pct, partd_step_pct, partd_ql_pct (shares of covering plans), partd_formularies.
  Shares are of plans (contract/plan/segment), not of enrolled people.
"""
from __future__ import annotations

import collections
import csv
import io
import re
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from engine.parsers.base import CrawlContext, Document

CATALOG = "https://data.cms.gov/data.json"
RXNAV = "https://rxnav.nlm.nih.gov/REST/drugs.json"
TITLE = "Quarterly Prescription Drug Plan Formulary, Pharmacy Network, and Pricing Information : "
WANTED = ("basic drugs formulary file", "plan information")


class RangeFile(io.RawIOBase):
    """A seekable read-only view of a remote file, fetched in large blocks with Range requests."""

    def __init__(self, url: str, fetcher, size: int, block: int = 1 << 20):
        self.url, self.fetcher, self.size, self.block = url, fetcher, size, block
        self.pos, self.cache, self.requests = 0, {}, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def prefetch(self, start: int, length: int) -> None:
        """One request for a whole span (e.g. a ZIP member) instead of many small ones."""
        self._get(start, min(self.size, start + length) - 1)

    def _get(self, start: int, end: int) -> None:
        host = re.match(r"https?://([^/]+)", self.url).group(1)
        self.fetcher._throttle(host)
        self.fetcher.network_requests += 1
        self.requests += 1
        req = urllib.request.Request(self.url, headers={"User-Agent": self.fetcher.user_agent,
                                                        "Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            self.cache[start] = r.read()

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        for start, data in self.cache.items():
            if start <= self.pos < start + len(data):
                chunk = data[self.pos - start:self.pos - start + n]
                b[:len(chunk)] = chunk
                self.pos += len(chunk)
                return len(chunk)
        self._get(self.pos, min(self.size, self.pos + max(n, self.block)) - 1)
        return self.readinto(b)


def releases(catalog: dict) -> list[tuple[str, str]]:
    """[(as_of 'YYYY-MM-DD', zip url)], newest first."""
    out = []
    for d in catalog.get("dataset", []):
        t = d.get("title", "")
        if t.startswith(TITLE):
            url = next((x.get("downloadURL") for x in d.get("distribution", [])
                        if (x.get("downloadURL") or "").lower().endswith(".zip")), None)
            if url:
                out.append((t[len(TITLE):].strip(), url))
    return sorted(out, reverse=True)


def coverage(plan_rows, formulary_rows, rxcuis: dict[str, set[str]]) -> dict[str, dict]:
    """Per term: plans covering it and the share with prior auth, step therapy, quantity limits."""
    plans = collections.defaultdict(set)
    for r in plan_rows:
        if r.get("PLAN_SUPPRESSED_YN") != "Y":
            plans[r["FORMULARY_ID"]].add((r["CONTRACT_ID"], r["PLAN_ID"], r["SEGMENT_ID"]))
    all_plans = set().union(*plans.values()) if plans else set()
    by_rx = {rx: t for t, ids in rxcuis.items() for rx in ids}
    rules: dict[str, dict] = collections.defaultdict(dict)
    for r in formulary_rows:
        t = by_rx.get(r["RXCUI"])
        if t and r["FORMULARY_ID"] in plans:
            prev = rules[t].get(r["FORMULARY_ID"])
            cur = (r["TIER_LEVEL_VALUE"], r["PRIOR_AUTHORIZATION_YN"] == "Y", r["STEP_THERAPY_YN"] == "Y",
                   r["QUANTITY_LIMIT_YN"] == "Y")
            rules[t][r["FORMULARY_ID"]] = cur if prev is None else (min(prev[0], cur[0]), prev[1] and cur[1],
                                                                    prev[2] and cur[2], prev[3] and cur[3])
    out = {}
    for t in rxcuis:
        fs = rules.get(t, {})
        covering = set().union(*(plans[f] for f in fs)) if fs else set()
        share = lambda i: round(100 * len(set().union(*(plans[f] for f, v in fs.items() if v[i]))) / len(covering), 1) \
            if covering and any(v[i] for v in fs.values()) else 0.0
        out[t] = {"formularies": len(fs), "plans": len(covering), "all_plans": len(all_plans),
                  "plans_pct": round(100 * len(covering) / len(all_plans), 1) if all_plans else 0.0,
                  "pa_pct": share(1), "step_pct": share(2), "ql_pct": share(3),
                  "tiers": collections.Counter(v[0] for v in fs.values()).most_common()}
    return out


class Parser:
    POSITION_KEY_PREFIXES = ("release:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.drugs = [d for d in o.get("drugs", []) if d.get("rxcui") or d.get("name")]
        self.quarters = int(o.get("quarters", 2))
        self.cache = Path(ctx.fetcher.cache_dir) / "partd"

    def _rxcuis(self, d: dict) -> set[str]:
        if d.get("rxcui"):
            return {str(x) for x in d["rxcui"]}
        self.ctx.check_budget()
        j = self.ctx.fetcher.get_json(RXNAV, {"name": d["name"]}, ttl=30 * 86400)
        return {c["rxcui"] for g in (j.get("drugGroup", {}).get("conceptGroup") or []) if g.get("tty") in ("SBD", "BPCK")
                for c in g.get("conceptProperties", [])}

    def _members(self, as_of: str, url: str) -> dict[str, Path]:
        """{'basic drugs formulary file': path, 'plan information': path} for one release."""
        folder = self.cache / as_of
        found = lambda: {w: next((p for p in folder.glob("*.txt") if w in p.name), None) for w in WANTED}
        if folder.exists() and all(found().values()):
            return found()
        if self.ctx.fetcher.offline:
            raise FileNotFoundError(f"Part D release {as_of} not in cache")
        self.ctx.fetcher._check_robots(url)
        head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": self.ctx.fetcher.user_agent})
        size = int(urllib.request.urlopen(head, timeout=60).headers["Content-Length"])
        rf = RangeFile(url, self.ctx.fetcher, size)
        rf.prefetch(max(0, size - (1 << 16)), 1 << 16)          # central directory
        outer = zipfile.ZipFile(io.BufferedReader(rf, buffer_size=1 << 16))
        folder.mkdir(parents=True, exist_ok=True)
        for info in outer.infolist():
            if any(w in info.filename for w in WANTED):
                rf.prefetch(info.header_offset, info.compress_size + 1024)
                with zipfile.ZipFile(io.BytesIO(outer.read(info))) as inner:
                    inner.extractall(folder)
        self.ctx.log.info("Part D %s: %d range requests", as_of, rf.requests)
        return found()

    @staticmethod
    def _rows(path: Path):
        with path.open(encoding="latin-1", newline="") as fh:
            yield from csv.DictReader(fh, delimiter="|")

    def crawl(self) -> Iterator[Document]:
        self.ctx.check_budget()
        cat = self.ctx.fetcher.get_json(CATALOG, ttl=7 * 86400)
        rxcuis = {d["term"]: self._rxcuis(d) for d in self.drugs}
        for as_of, url in releases(cat)[:self.quarters]:
            key = f"release:{as_of}"
            if self.ctx.state.get(key):
                continue
            m = self._members(as_of, url)
            cov = coverage(self._rows(m["plan information"]), self._rows(m["basic drugs formulary file"]), rxcuis)
            when = int(datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp())
            for term, c in cov.items():
                tiers = ", ".join(f"tier {t}: {n}" for t, n in c["tiers"]) or "none"
                content = (f"Medicare Part D formularies as of {as_of}: {term} is covered by {c['plans']:,} of "
                           f"{c['all_plans']:,} plans ({c['plans_pct']}%), on {c['formularies']} formularies "
                           f"(RxNorm {', '.join(sorted(rxcuis[term])) or 'not found'}). Of covering plans, "
                           f"{c['pa_pct']}% require prior authorization, {c['step_pct']}% step therapy and "
                           f"{c['ql_pct']}% a quantity limit. Formularies by tier: {tiers}. Shares are of plans, "
                           f"not enrollees.")
                yield Document(
                    doc_id=f"{self.ctx.source.id}:{term}:{as_of}", source=self.ctx.source.id,
                    source_type=self.ctx.source.source_type, kind="coverage", url=url,
                    title=f"Medicare Part D coverage: {term}, {as_of[:7]} ({c['plans_pct']}% of plans)",
                    content_md=content, post_date=when, tags=[term],
                    extra={"term": term, "metrics": {"partd_plans_pct": c["plans_pct"], "partd_pa_pct": c["pa_pct"],
                                                     "partd_step_pct": c["step_pct"], "partd_ql_pct": c["ql_pct"],
                                                     "partd_formularies": c["formularies"]}})
            self.ctx.state.set(key, 1)
