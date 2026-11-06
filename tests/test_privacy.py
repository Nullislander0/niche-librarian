"""Pseudonymized authors: stable, keyed, scrubbed from text; poster counts and switch flows."""
import copy
import json

from engine import privacy, signals
from engine.config import load_pack
from engine.index.store import Store
from engine.indexer import index_document
from engine.parsers.base import Document
from engine.versions import VersionTagger

KEY = b"test-key"


def post(i, author, text, when, author_id=None):
    return Document(doc_id=f"f:p:{i}", source="f", source_type="forum", kind="reply", url=f"https://f.example/t/{i}",
                    title="Eye drops", content_md=text, post_date=when, author=author,
                    author_id=author_id or str(100 + i), author_reputation=1234, author_created=1600000000)


def test_pseudonyms_are_stable_keyed_and_scrub_text():
    p = privacy.Pseudonymizer(KEY)
    a = p.apply(post(1, "Luis Pereira", "My doctor switched me. Email me at luis@example.com or 555-123-4567.", 1))
    b = p.apply(post(2, "Walkey", "> [Luis Pereira said:](https://f.example/t/1)\n> switched\n\n@Luis Pereira @Walkey "
                                  "same here", 2))
    assert a.author == a.author_id and a.author.startswith("P-") and a.author != "Luis Pereira"
    assert "luis@example.com" not in a.content_md and "555-123-4567" not in a.content_md
    assert "Luis" not in b.content_md and "Walkey" not in b.content_md
    assert f"[{a.author} said:]" in b.content_md and f"@{a.author}" in b.content_md and f"@{b.author}" in b.content_md
    assert a.author_reputation == 1000 and b.extra["pseudonymized"] == "pseudonymize"
    # Same person, new run: same pseudonym. Another key: a different one.
    assert privacy.Pseudonymizer(KEY).apply(post(3, "Luis Pereira", "x", 3)).author == a.author
    assert privacy.Pseudonymizer(b"other").apply(post(3, "Luis Pereira", "x", 3)).author != a.author


def test_drop_mode_and_band():
    d = privacy.Pseudonymizer(KEY).apply(post(1, "Someone", "hi", 1), mode="drop")
    assert d.author is None and d.author_id is None
    assert [privacy.band(n) for n in (None, 3, 42, 999, 54321)] == [None, 0, 10, 100, 10000]


def test_apply_to_existing_store_and_switch_flows(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "DATA_DIR", tmp_path)
    pack = copy.deepcopy(load_pack("homelab"))
    pack.privacy = {"authors": "pseudonymize"}
    pack.signals = {"terms": [{"name": "branda"}, {"name": "brandb"}],
                    "categories": {"switching": {"keywords": "switch"}}}
    store = Store(":memory:", 16)
    tagger = VersionTagger({})
    for i, (who, text, when) in enumerate([("ann", "On Branda now", 10), ("ann", "Switched to Brandb", 20),
                                           ("bob", "Branda works", 15), ("bob", "Tried Brandb too, switch?", 30),
                                           ("cy", "Brandb first", 5), ("cy", "then Branda", 40)]):
        index_document(store, tagger, post(i, who, text, when))
    res = privacy.apply_to_store(pack, store)
    assert res["pseudonymized"] == 6
    authors = {r[0] for r in store.db.execute("SELECT author FROM documents")}
    assert not authors & {"ann", "bob", "cy"} and all(a.startswith("P-") for a in authors) and len(authors) == 3
    assert privacy.apply_to_store(pack, store)["pseudonymized"] == 0           # idempotent

    signals.run(pack, store, keywords_only=True)
    agg = signals.aggregate(pack, store, by="month")
    rows = {(r["term"], r["category"]): r for r in agg["rows"]}
    assert rows[("branda", "(any mention)")]["total"] == 3 and rows[("branda", "(any mention)")]["posters"] == 3
    flows = {(r["from"], r["to"]): r["posters"] for r in agg["switches"]}
    assert flows == {("branda", "brandb"): 2, ("brandb", "branda"): 1}
    assert "ann" not in signals.format_report(agg)


def test_greetings_attributions_and_bare_handles_but_not_drug_words(monkeypatch):
    monkeypatch.setattr(privacy, "_WORDS", {"incorrect", "sushi"})
    p = privacy.Pseudonymizer(KEY)
    for name in ("rushadaflame", "Benga", "Concertaconcert", "MrG", "haloperidol", "5-HT2A", "Sushi", "Tom"):
        p.apply(post(hash(name) % 1000, name, "x", 1))
    text = ("Hi, rushadaflame, welcome!\n\"Benga asked about it\"\nConcertaconcert added 316 Minutes later.\n"
            "Later Benga came back, MrG too, and Tom. I take [haloperidol](https://f.example/wiki/haloperidol) and haloperidol "
            "works; 5-HT2A agonists; sushi for dinner, Sushi too.")
    out = p.scrub("f", text)
    for gone in ("rushadaflame", "Benga", "Concertaconcert", "MrG"):
        assert gone not in out, gone
    for kept in ("[haloperidol](", "haloperidol works", "5-HT2A agonists", "sushi for dinner", "Sushi too"):
        assert kept in out, kept


def test_profile_links_and_urls_are_removed():
    p = privacy.Pseudonymizer(KEY)
    out = p.scrub("f", "[elliemee](https://forum.example/member/15180-elliemee) yes, see "
                       "https://forum.example/members/bob.42/ and [a drug](https://forum.example/wiki/x)")
    assert "elliemee" not in out and "bob" not in out and "/member" not in out
    assert out.startswith(p._for_handle("f", "elliemee")) and "[a drug](https://forum.example/wiki/x)" in out


def test_case_insensitive_greetings_sign_offs_and_possessives(monkeypatch):
    monkeypatch.setattr(privacy, "_WORDS", {"max", "ariel", "member"})
    p = privacy.Pseudonymizer(KEY)
    for name in ("Nevil", "lelagy", "Max", "Ariel", "Member"):
        p.apply(post(hash(name) % 1000, name, "x", 1))
    out = p.scrub("f", "Hello nevil ,\nThank you Lelagy, great.\nIn response to Ariel's original posting, yes.\n\n"
                       "Max\n\nMax dosage is fine. She is a member here.")
    for gone in ("nevil", "Lelagy", "Ariel's", "\nMax\n"):
        assert gone not in out, gone
    assert "Max dosage is fine" in out and "is a member here" in out
