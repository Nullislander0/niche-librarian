"""memo-data: latest vs previous per measure, with periods and citations; FAERS lag respected."""
import copy
import time
from datetime import datetime, timezone

from engine import memo_data
from engine.config import load_pack
from engine.index.store import Store
from engine.parsers.base import Document


def ts(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def doc(i, kind, when, term, metrics, **extra):
    return Document(doc_id=f"x:{i}", source="x", source_type="data", kind=kind, url="u", title="t", content_md="c",
                    post_date=when, extra={"term": term, "metrics": metrics, **extra})


def test_latest_previous_change_and_lag():
    pack = copy.deepcopy(load_pack("homelab"))
    store = Store(":memory:", 16)
    now = ts(2026, 11, 6)
    for d in [doc(1, "utilization", ts(2025, 10), "branda", {"medicaid_rx": 2729}, state="XX"),
              doc(2, "utilization", ts(2026, 1), "branda", {"medicaid_rx": 2682}, state="XX"),
              doc(3, "utilization", ts(2026, 1), "branda", {"medicaid_rx": 40}, state="CA"),     # state rows ignored
              doc(4, "coverage", ts(2026, 6), "brandb", {"partd_plans_pct": 0.1}),
              doc(5, "coverage", ts(2026, 9), "brandb", {"partd_plans_pct": 16.5}),
              doc(6, "report", ts(2026, 8), "branda", {"faers_reports": 361}),
              doc(7, "report", ts(2026, 10), "branda", {"faers_reports": 0}),                   # inside the lag
              doc(8, "shortage", ts(2026, 7), "branda", {"shortage_listings": 1, "shortage_current": 1})]:
        store.upsert(d, [], ["c"])
    data = memo_data.build(pack, store, now=now)
    rx = data["terms"]["branda"]["measures"]["medicaid_rx"]
    assert (rx["value"], rx["previous"], rx["change"], rx["change_pct"]) == (2682, 2729, -47, -1.7)
    assert rx["period"] == "2026 Q1" and rx["previous_period"] == "2025 Q4" and rx["cite"] == "x:2"
    pd = data["terms"]["brandb"]["measures"]["partd_plans_pct"]
    assert pd["value"] == 16.5 and pd["change"] == 16.4
    fa = data["terms"]["branda"]["measures"]["faers_reports"]
    assert fa["value"] == 361 and fa["period"] == "2026-08"            # October left out as incomplete
    assert data["terms"]["branda"]["events"]["shortages_current"] == 1
    md = memo_data.to_markdown(data)
    assert "| Medicaid prescriptions | 2,682 (2026 Q1) | 2,729 (2025 Q4) | -47 (-1.7%) | [x:2] |" in md
