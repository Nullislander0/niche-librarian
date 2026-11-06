"""Command-line checks for failures that wouldn't crash, so nobody would notice them:
exit codes other tools rely on, flags that limit a crawl, the private-address block, and the
numbers that go into reports. Everything runs in a throwaway pack and data folder, offline."""
import json

import pytest

from engine import cli, config, privacy, watches, webpages
from engine.index.embed import OllamaEmbedder
from engine.index.store import Store

FAKE_PARSER = '''
from engine.parsers.base import Document

class Parser:
    POSITION_KEY_PREFIXES = ()

    def __init__(self, ctx):
        self.ctx = ctx

    def crawl(self):
        o, sid = self.ctx.options, self.ctx.source.id
        if o.get("data"):
            for i, (q, rx) in enumerate([("2025-10-01", 100), ("2026-01-01", 120)]):
                yield Document(doc_id=f"{sid}:q{i}", source=sid, source_type="data", kind="utilization", url="u",
                               title=f"Medicaid prescriptions: branda {q}", content_md="numbers",
                               post_date=1759276800 + i * 7948800,
                               extra={"term": "branda", "state": "XX", "metrics": {"medicaid_rx": rx}})
            return
        for i in range(o.get("threads", 4)):
            root = f"{sid}:t{i}"
            yield Document(doc_id=root, source=sid, source_type="forum", kind="question",
                           url=f"https://forum.example.org/{sid}/{i}", title=f"Branda cash price question {i}",
                           content_md=f"Has anyone paid cash for Branda? Thread {i}.", post_date=1790000000 + i,
                           author=f"Zorblaxx{i}")
            yield Document(doc_id=f"{root}:r", source=sid, source_type="forum", kind="reply", parent_id=root,
                           url=f"https://forum.example.org/{sid}/{i}#r", title=f"Branda cash price question {i}",
                           content_md="Yes, the cash price is $59 a month.", post_date=1790000100 + i,
                           author="Quintessa")
'''

PACK = """
name: CLI test pack
description: Throwaway pack for command-line tests.
privacy: {authors: pseudonymize}
signals:
  terms: [{name: branda}]
  categories: {price: {weight: 1.0, description: cost, keywords: 'price|cash|\\\\$\\\\d'}}
sources:
  - {id: forum_a, parser: fakeforum, source_type: forum, options: {threads: 4}}
  - {id: forum_b, parser: fakeforum, source_type: forum, options: {threads: 4}}
  - {id: data, parser: fakeforum, source_type: data, options: {data: true}}
"""


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    pack_dir = tmp_path / "packs" / "clipack"
    (pack_dir / "parsers").mkdir(parents=True)
    (pack_dir / "parsers" / "fakeforum.py").write_text(FAKE_PARSER, encoding="utf-8")
    (pack_dir / "sources.yaml").write_text(PACK, encoding="utf-8")
    for mod in (config, webpages, watches, privacy):
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PACK_DIRS", [tmp_path / "packs"])
    monkeypatch.setattr(OllamaEmbedder, "available", lambda self: False)

    def librarian(*args, pack="clipack"):
        """Run `librarian --pack PACK ARGS...`; returns (exit code, printed output)."""
        capsys.readouterr()
        code = 0
        try:
            cli.main(["--pack", pack, *args])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        return code, capsys.readouterr().out

    librarian.tmp = tmp_path
    librarian.pack_dir = pack_dir
    return librarian


def store(run):
    return Store(config.load_pack("clipack").db_path, 768)


def test_check_exit_codes(run):
    assert run("check") == (0, "clipack: no problems found\n")
    yaml = (run.pack_dir / "sources.yaml").read_text().replace("signals:", "signal:")
    (run.pack_dir / "sources.yaml").write_text(yaml)
    code, out = run("check")
    assert code == 1 and "unknown top-level key 'signal'" in out


def test_crawl_source_and_sample_limits(run):
    code, out = run("crawl", "--source", "forum_a", "--sample", "2", "--no-embed")
    report = json.loads(out[:out.rindex("}") + 1])
    assert code == 0 and set(report) - {"privacy_audit"} == {"forum_a"}          # forum_b never touched
    s = store(run)
    roots = s.db.execute("SELECT COUNT(*) FROM documents WHERE parent_id IS NULL").fetchone()[0]
    sources = {r[0] for r in s.db.execute("SELECT DISTINCT source FROM documents")}
    assert roots == 2 and sources == {"forum_a"}                                 # only 2 threads


def test_privacy_audit_exit_codes(run):
    run("crawl", "--source", "forum_a", "--no-embed")
    code, out = run("privacy", "audit")
    assert code == 0 and json.loads(out)["leaks"] == 0                          # crawl pseudonymized it all
    s = store(run)                                                              # plant a leftover handle
    s.db.execute("UPDATE documents SET content_md = content_md || ' Thanks Quintessa!' WHERE doc_id='forum_a:t1:r'")
    s.db.commit()
    code, out = run("privacy", "audit")
    assert code == 1 and json.loads(out)["sources"]["forum_a"]["documents_with_leaks"] == 1
    assert "Quintessa" not in out                                               # the report never names people


def test_pages_add_refuses_private_addresses(run):
    for url in ("http://127.0.0.1:11434/api/tags", "http://192.168.1.1/admin", "http://169.254.169.254/latest/"):
        code, out = run("pages", "add", url)
        assert "private or local" in out, url
    q = webpages.load("clipack")
    assert q["pending"] == {} and q["approved"] == {}


def test_crawl_then_search_round_trip(run):
    run("crawl", "--no-embed")
    code, out = run("search", "cash price Branda")
    assert code == 0 and "Branda cash price question" in out and "#c0" in out
    s = store(run)
    authors = {r[0] for r in s.db.execute("SELECT DISTINCT author FROM documents WHERE source_type='forum'")}
    assert authors and all(a.startswith("P-") for a in authors)                 # no handles stored


def test_memo_data_numbers_and_file(run):
    run("crawl", "--source", "data", "--no-embed")
    out_file = run.tmp / "memo.md"
    code, out = run("memo-data", "--out", str(out_file))
    row = "| Medicaid prescriptions | 120 (2026 Q1) | 100 (2025 Q4) | +20 (+20.0%) | [data:q1] |"
    assert code == 0 and row in out and row in out_file.read_text()
    code, out = run("memo-data", "--json")
    m = json.loads(out)["terms"]["branda"]["measures"]["medicaid_rx"]
    assert (m["value"], m["previous"], m["change_pct"]) == (120, 100, 20.0)
