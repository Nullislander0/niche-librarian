import yaml

from engine.config import PACKS_DIR
from engine.versions import VersionTagger, version_matches

cfg = yaml.safe_load((PACKS_DIR / "esri_enterprise" / "versions.yaml").read_text())
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
