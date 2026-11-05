"""Signals (labels and trend counts) and storing web results only under storage rights."""
import copy
import time

from engine import signals, watches, webresults
from engine.config import load_pack
from engine.index.store import Store
from engine.indexer import index_document
from engine.parsers.base import Document
from engine.search import Searcher
from engine.versions import VersionTagger

from test_pipeline import FakeEmbedder

WEEK = 7 * 86400

# Self-contained (the rx pack itself stays local): a public pack plus a signals config.
SIGNALS = {
    "terms": [{"name": "amphetamine", "match": "adderall|amphetamine|vyvanse"}],
    "categories": {
        "price": {"weight": 1.0, "description": "cost", "keywords": r"price|cost|\$\d"},
        "shortage": {"weight": 1.0, "description": "shortages", "keywords": "shortage|can.t fill|couldn.t fill"},
        "recreational": {"weight": 0.5, "description": "non-medical use", "keywords": "recreational|get(ting)? high"},
    },
}


def rx_pack():
    pack = copy.deepcopy(load_pack("homelab"))
    pack.signals = copy.deepcopy(SIGNALS)
    pack.web = {"storage_rights": False}
    return pack


def post(i, text, when):
    return Document(doc_id=f"drugs_forum:p:{i}", source="drugs_forum", source_type="forum", kind="reply",
                    url=f"https://drugs-forum.com/threads/x.{i}/", title="thread", content_md=text,
                    post_date=when, author=f"user{i}")


def test_parse_labels_keeps_known_categories_only():
    cats = {"price": {}, "shortage": {}}
    assert signals.parse_labels('{"categories": ["price", "made_up"]}', cats) == ["price"]
    assert signals.parse_labels('<think>hm</think>{"categories": []}', cats) == []


def test_run_and_aggregate_with_weights():
    pack = rx_pack()
    store = Store(":memory:", 16)
    now = int(time.time())
    for i, (text, when) in enumerate([
            ("Adderall shortage again, pharmacy couldn't fill it", now - WEEK),
            ("Generic adderall price went up to $90 cash", now - WEEK),
            ("took my adderall recreationally to get high", now),
            ("Unrelated post about the weather", now)]):
        store.upsert(post(i, text, when), [], [text])
    calls = []

    def fake_llm(text, terms):
        calls.append(terms)
        return signals.keyword_classify(text, signals._categories(pack))

    res = signals.run(pack, store, classifier=fake_llm)
    assert res["mentioning_a_term"] == 3 and all(t == ["amphetamine"] for t in calls)
    assert signals.run(pack, store, classifier=fake_llm)["documents_checked"] == 0     # nothing new

    agg = signals.aggregate(pack, store)
    rows = {(r["term"], r["category"]): r for r in agg["rows"]}
    assert rows[("amphetamine", "(any mention)")]["total"] == 3
    assert rows[("amphetamine", "shortage")]["total"] == 1 and rows[("amphetamine", "price")]["total"] == 1
    assert rows[("amphetamine", "recreational")]["weighted"] == 0.5             # half weight
    assert len(agg["periods"]) == 2
    report = signals.format_report(agg)
    assert "user0" not in report and "drugs-forum.com" not in report             # no people, no links


def brave_hits():
    return [{"kind": "discussion", "url": "https://www.reddit.com/r/ADHD/comments/abc/adderall_shortage/",
             "title": "Adderall shortage again?", "snippet": "Three pharmacies told me they can't fill my Adderall",
             "age": "2 days ago", "published": "2026-11-03T10:00:00", "backend": "brave"},
            {"kind": "web", "url": "https://example.com/adderall-prices",
             "title": "Adderall cash price", "snippet": "The cash price is $85 without insurance",
             "age": "", "backend": "brave"}]


def test_web_results_are_stored_only_with_storage_rights():
    pack = rx_pack()
    store = Store(":memory:", 16)
    assert not webresults.enabled(pack)
    assert webresults.store(pack, brave_hits(), "adderall shortage", store=store) == 0
    assert store.find_urls([h["url"] for h in brave_hits()]) == {}

    licensed = copy.deepcopy(pack)
    licensed.web["storage_rights"] = True
    assert webresults.store(licensed, brave_hits(), "adderall shortage", "w", store=store, embed=False) == 2
    doc = store.get_document(store.find_urls([brave_hits()[0]["url"]])[brave_hits()[0]["url"]])
    assert doc["source"] == "web_results" and doc["extra"]["query"] == "adderall shortage"
    assert time.gmtime(doc["post_date"]).tm_mday == 3

    # Retrievable like anything else in the index, and countable.
    s = Searcher(licensed, store=store, embedder=FakeEmbedder())
    assert any(r["source"] == "web_results" for r in s.search("adderall pharmacies can't fill")["results"])
    signals.run(licensed, store, keywords_only=True)
    rows = {(r["term"], r["category"]) for r in signals.aggregate(licensed, store, source="web_results")["rows"]}
    assert ("amphetamine", "shortage") in rows and ("amphetamine", "price") in rows

    assert webresults.purge(licensed, store) == 2
    assert store.find_urls([h["url"] for h in brave_hits()]) == {}
    assert store.db.execute("SELECT COUNT(*) FROM signal_labels").fetchone()[0] == 0


def test_watch_stores_results_only_when_licensed(tmp_path, monkeypatch):
    monkeypatch.setattr(watches, "DATA_DIR", tmp_path)
    stored = []
    monkeypatch.setattr(webresults, "store", lambda pack, hits, q, name, **k: stored.append(len(hits)) or len(hits))

    class Web:
        def search(self, *a, **k):
            return brave_hits()
    pack = rx_pack()
    watches.add(pack, "adhd", "adderall shortage", web=True)
    out = watches.run(pack, web_client=Web(), local=False)
    assert out["stored"] == {} and stored == []

    licensed = copy.deepcopy(pack)
    licensed.web["storage_rights"] = True
    watches.add(licensed, "adhd2", "adderall prices", web=True)
    out = watches.run(licensed, ["adhd2"], web_client=Web(), local=False)
    assert out["stored"] == {"adhd2": 2} and stored == [2]
