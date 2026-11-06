import os
import sys
from pathlib import Path

# Tests use only the packs in the repo, never a private packs folder on this machine.
os.environ["LIBRARIAN_PACKS"] = str(Path(__file__).resolve().parent.parent / "packs")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def no_stay_awake(monkeypatch):
    monkeypatch.setenv("LIBRARIAN_NO_STAY_AWAKE", "1")


@pytest.fixture(autouse=True)
def no_model_downloads(monkeypatch):
    """Tests never load the real cross-encoder (it's an optional download); see test_cross.py."""
    from engine.rank import cross
    monkeypatch.setattr(cross.CrossReranker, "from_config", classmethod(lambda cls, cfg: None))
