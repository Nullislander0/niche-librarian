"""sources.yaml checks: typos in keys and options are reported, valid packs are clean."""
from engine.config import SourceConfig, check_meta, check_options, load_pack
from engine.parsers.base import load_parser


def test_typos_and_bad_values_are_reported():
    meta = {"name": "x", "crawlr": {}, "crawler": {"min_interval_secs": 2}, "privacy": {"authors": "hide"},
            "web": {"storage_right": True},
            "sources": [{"id": "a", "parser": "discourse", "trust": "high", "optons": {}},
                        {"id": "a", "parser": "vanilla"}, {"parser": "mediawiki"}]}
    problems = " | ".join(check_meta(meta))
    for expected in ("unknown top-level key 'crawlr'", "crawler: unknown key 'min_interval_secs'",
                     "privacy.authors must be", "web: unknown key 'storage_right'", "source a: unknown key 'optons'",
                     "trust must be a number", "duplicate source id 'a'", "missing 'id'"):
        assert expected in problems, expected


def test_unknown_parser_options_but_not_documented_or_read_ones():
    src = SourceConfig(id="f", parser="discourse", source_type="forum",
                       options={"base_url": "https://f.example", "categorys": [], "since": "2024-01-01"})
    assert check_options(src, load_parser("discourse", load_pack("homelab").path)) == [
        "source f: option 'categorys' isn't one discourse understands"]


def test_shipped_packs_are_clean():
    from engine.config import _read_yaml
    for name in ("homelab", "esri_enterprise"):
        pack = load_pack(name)
        assert check_meta(_read_yaml(pack.path / "sources.yaml")) == []
        for src in pack.sources:
            assert check_options(src, load_parser(src.parser, pack.path)) == [], src.id
