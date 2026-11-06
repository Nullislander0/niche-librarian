"""openFDA and NADAC parsers, and their numbers in the signals report."""
import copy
from datetime import date

from engine import signals
from engine.config import SourceConfig, load_pack
from engine.crawl.fetcher import FetchError
from engine.index.store import Store
from engine.indexer import index_document
from engine.parsers import nadac, openfda
from engine.parsers.base import CrawlContext
from engine.versions import VersionTagger


class DictState(dict):
    def set(self, k, v):
        self[k] = v


class Fake:
    """Answers by matching a substring of the request."""
    network_requests = 0

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url, params=None, ttl=None):
        key = url + " " + " ".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        self.calls.append(key)
        for pattern, answer in self.routes:
            if all(p in key for p in pattern.split("&&")):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise FetchError("HTTP 404", 404)


def ctx(parser_id, options, fetcher):
    src = SourceConfig(id=parser_id, parser=parser_id, source_type="data", options=options)
    return CrawlContext(source=src, fetcher=fetcher, state=DictState())


def test_openfda_monthly_events_shortages_recalls():
    month = date.today().strftime("%Y-%m")
    f = Fake([
        ("event.json&&count=serious", {"results": [{"term": 1, "count": 30}, {"term": 2, "count": 70}]}),
        ("event.json&&reactionmeddrapt", {"results": [{"term": "NAUSEA", "count": 20},
                                                      {"term": "DRUG DIVERSION", "count": 6},
                                                      {"term": "INTENTIONAL PRODUCT MISUSE", "count": 4}]}),
        ("shortages.json", {"results": [{"generic_name": "Oxycodone Tablet", "availability": "Limited Availability",
                                         "status": "Current", "update_date": "10/01/2026", "package_ndc": "1-2-3"}]}),
        ("enforcement.json", {"results": [{"recall_number": "D-1-2026", "classification": "Class II",
                                           "report_date": "20260218", "reason_for_recall": "Defective container",
                                           "product_description": "Oxycodone HCl Tablets"}]}),
    ])
    docs = list(openfda.Parser(ctx("openfda", {"since": month, "drugs": [{"term": "oxycodone", "generic": ["oxycodone"]}]},
                                   f)).crawl())
    ev, sh, rc = docs
    assert ev.extra["metrics"] == {"faers_reports": 100, "faers_serious": 30, "faers_misuse": 10}
    assert "do not establish" in ev.content_md and ev.kind == "report"
    assert sh.extra["metrics"]["shortage_current"] == 1 and "Limited Availability" in sh.title
    assert rc.doc_id == "openfda:recall:D-1-2026" and rc.extra["term"] == "oxycodone"
    assert 'patient.drug.openfda.generic_name:"oxycodone"' in f.calls[0]


def test_openfda_no_matches_is_zero_not_an_error():
    f = Fake([])
    docs = list(openfda.Parser(ctx("openfda", {"since": date.today().strftime("%Y-%m"), "feeds": ["events", "shortages"],
                                               "drugs": [{"term": "x", "generic": ["x"]}]}, f)).crawl())
    assert len(docs) == 1 and docs[0].extra["metrics"]["faers_reports"] == 0


def test_nadac_levels_and_changes():
    today = date.today().isoformat()
    year = today[:4]
    catalog = [{"title": f"NADAC (National Average Drug Acquisition Cost) {year}", "identifier": "Y"},
               {"title": "NADAC Comparison", "identifier": "C"}]
    level = lambda ndc, desc, price, cls="G": {"ndc": ndc, "ndc_description": desc, "nadac_per_unit": price,
                                               "classification_for_rate_setting": cls, "pricing_unit": "EA",
                                               "as_of_date": today}
    f = Fake([
        ("metastore", catalog),
        ("query/Y/0&&offset=0", {"count": 3, "results": [level("1", "OXYCODONE 10 MG TAB", "0.16"),
                                                         level("2", "OXYCODONE 10 MG TAB", "0.18"),
                                                         level("3", "OXYCONTIN 10 MG TAB", "3.10", "B")]}),
        ("query/C/0&&offset=0", {"count": 2, "results": [
            {"ndc_description": "OXYCODONE 10 MG TAB", "old_nadac_per_unit": "0.16", "new_nadac_per_unit": "0.17",
             "percent_change": "6.25", "primary_reason": "Survey Rate", "effective_date": today},
            {"ndc_description": "OXYCODONE 30 MG TAB", "old_nadac_per_unit": "0.25", "new_nadac_per_unit": "0.24",
             "percent_change": "-4.00", "primary_reason": "Survey Rate", "effective_date": today}]}),
    ])
    docs = list(nadac.Parser(ctx("nadac", {"drugs": [{"term": "oxycodone", "match": ["OXYCODONE"]}],
                                           "since": today}, f)).crawl())
    lv, ch = docs
    assert lv.extra["metrics"] == {"nadac_products": 2, "nadac_median_unit_price": 1.635, "nadac_median_generic_unit_price": 0.17}
    assert "brand" in lv.content_md and "$3.1000 / EA" in lv.content_md
    assert ch.extra["metrics"] == {"nadac_increases": 1, "nadac_decreases": 1, "nadac_median_change_pct": 1.12}

    # In the signals report: measures next to forum counts, and never sent to the LLM.
    pack = copy.deepcopy(load_pack("homelab"))
    pack.signals = {"terms": [{"name": "oxycodone"}], "categories": {"price": {"keywords": "price"}}}
    store = Store(":memory:", 16)
    for d in docs:
        index_document(store, VersionTagger({}), d)
    assert signals.run(pack, store, classifier=lambda *a: 1 / 0)["mentioning_a_term"] == 0
    m = {r["measure"]: r for r in signals.aggregate(pack, store)["measures"]}
    assert m["nadac_increases"]["how"] == "sum" and m["nadac_median_generic_unit_price"]["how"] == "median"
    assert "Measures from official data" in signals.format_report(signals.aggregate(pack, store))


def test_openfda_brand_names_use_the_reported_product_name():
    month = date.today().strftime("%Y-%m")
    f = Fake([("event.json&&count=serious", {"results": [{"term": 2, "count": 5}]}),
              ("event.json&&reactionmeddrapt", {"results": [{"term": "EYE IRRITATION", "count": 3}]})])
    docs = list(openfda.Parser(ctx("openfda", {"since": month, "feeds": ["events"],
                                               "drugs": [{"term": "branda", "brand": ["BRANDA"]}]}, f)).crawl())
    assert docs[0].extra["metrics"]["faers_reports"] == 5
    assert 'patient.drug.medicinalproduct:"BRANDA"' in f.calls[0] and "openfda.brand_name" not in f.calls[0]


def test_partd_coverage_shares_are_of_covering_plans():
    from engine.parsers.partd_formulary import coverage
    plan = lambda c, p, f, sup="N": {"CONTRACT_ID": c, "PLAN_ID": p, "SEGMENT_ID": "000", "FORMULARY_ID": f,
                                     "PLAN_SUPPRESSED_YN": sup}
    plans = [plan("H1", "001", "F1"), plan("H1", "001", "F1"), plan("H2", "001", "F2"), plan("H3", "001", "F3"),
             plan("H4", "001", "F4", sup="Y")]
    row = lambda f, rx, pa="N", st="N", tier="3": {"FORMULARY_ID": f, "RXCUI": rx, "TIER_LEVEL_VALUE": tier,
                                                   "PRIOR_AUTHORIZATION_YN": pa, "STEP_THERAPY_YN": st,
                                                   "QUANTITY_LIMIT_YN": "N"}
    rows = [row("F1", "100", pa="Y"), row("F2", "100", st="Y", tier="4"), row("F4", "100"), row("F3", "200")]
    c = coverage(plans, rows, {"branda": {"100"}, "other": {"200"}, "absent": {"999"}})
    assert c["branda"]["plans"] == 2 and c["branda"]["all_plans"] == 3 and c["branda"]["plans_pct"] == 66.7
    assert c["branda"]["pa_pct"] == 50.0 and c["branda"]["step_pct"] == 50.0      # suppressed plan F4 ignored
    assert c["absent"] == {"formularies": 0, "plans": 0, "all_plans": 3, "plans_pct": 0.0, "pa_pct": 0.0,
                           "step_pct": 0.0, "ql_pct": 0.0, "tiers": []}


def test_medicaid_quarters_sum_ndcs_and_flag_suppression():
    from engine.parsers import medicaid_sdud
    p = medicaid_sdud.Parser(ctx("medicaid_sdud", {"drugs": [{"term": "branda", "product": ["BRANDA"]}]}, Fake([])))
    r = lambda ndc, q, rx, typ="FFSU", sup="false": {"ndc": ndc, "year": "2026", "quarter": q, "utilization_type": typ,
                                                     "number_of_prescriptions": rx, "units_reimbursed": "10",
                                                     "total_amount_reimbursed": "100.5", "suppression_used": sup}
    rows = [r("1", "1", "20"), r("1", "1", "30", typ="MCOU"), r("1", "1", "30", typ="MCOU"),   # duplicate row
            r("2", "1", None, sup="true"), r("1", "2", "5")]
    docs = list(p._quarters({"term": "branda", "product": ["BRANDA"]}, "XX", rows))
    q1 = docs[0]
    assert q1.extra["metrics"]["medicaid_rx"] == 50 and "a floor" in q1.content_md
    assert q1.title == "Medicaid prescriptions: branda, 2026 Q1 (50)" and len(docs) == 2
