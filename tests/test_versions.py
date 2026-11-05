import yaml

from engine.config import PACKS_DIR
from engine.versions import VersionTagger, version_matches

cfg = yaml.safe_load((PACKS_DIR / "esri_enterprise" / "versions.yaml").read_text(encoding="utf-8"))
t = VersionTagger(cfg)


def test_tags_versions_near_product_terms():
    assert t.tag("Upgraded ArcGIS Enterprise 10.9.1 to 11.3 last week") == ["10.9.1", "11.3"]


def test_ignores_numbers_without_context():
    assert t.tag("The disk had 10.5 GB free and the job took 11.2 minutes.") == []


def test_ignores_ip_addresses():
    assert t.tag("Portal on server 10.1.2.3 is unreachable") == []


def test_aliases():
    assert t.tag("we run portal 10.81") == ["10.8.1"]


def test_query_needs_no_context():
    assert t.detect_query("11.3 federation error") == ["11.3"]


def test_version_matches_family():
    assert version_matches("11.3", "11")
    assert version_matches("10.9.1", "10.9")
    assert not version_matches("10.9.1", "10.8")


def test_trailing_zero_patch_is_same_release():
    assert t.tag("installed ArcGIS Enterprise 11.5.0 then 11.5") == ["11.5"]


def test_database_versions_are_not_arcgis_versions():
    assert t.tag("ArcGIS Server on PostgreSQL 12.2 and Oracle 12.2, portal 11.3") == ["11.3"]
    assert t.tag("upgrade enterprise geodatabase from postgres version 11.7") == []


def test_multi_product_versions():
    from engine.versions import VersionTagger, version_key, version_matches
    t = VersionTagger({"products": [
        {"prefix": "pve", "pattern": r"(?i)(?:proxmox(?:\s?ve)?|\bpve)\s*v?(\d\.\d(?:\.\d+)?)"},
        {"prefix": "truenas", "pattern": r"(?i)truenas(?:\s(?:scale|core))?\s*(\d{2}\.\d{2}(?:\.\d+)?)"},
    ]})
    assert t.tag("Upgraded Proxmox VE 8.2 and TrueNAS SCALE 24.10, then pve 9.0") == ["pve 8.2", "pve 9.0", "truenas 24.10"]
    assert t.tag("my 8.2 TB disk") == []
    assert version_matches("pve 8.2", "pve 8") and sorted(["pve 8.10", "pve 8.2"], key=version_key) == ["pve 8.2", "pve 8.10"]


def test_source_scoped_product_rule():
    from engine.versions import VersionTagger
    t = VersionTagger({"products": [{"prefix": "truenas", "only_sources": ["truenas_forums"],
                                     "pattern": r"(?<![\w.])(2[2-9]\.(?:04|10)(?:\.\d+)?)(?![\d.])"}]})
    assert t.tag("On 25.10.7 the bug is fixed", "truenas_forums") == ["truenas 25.10.7"]
    assert t.tag("On 25.10.7 the bug is fixed", "docker_forums") == []
