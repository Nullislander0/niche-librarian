"""The repo holds recipes and code only: no reports, crawl results, data or secrets."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(r"(^|/)(data|reports|secrets)/|digest|(^|/)memo/|\.db$|\.bundle$|pseudonym|\.key$|(^|/)\.env",
                       re.I)


def test_no_reports_results_or_secrets_are_tracked():
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    bad = [p for p in tracked if p and FORBIDDEN.search(p)]
    assert not bad, f"private or result files are tracked: {bad[:10]}"
