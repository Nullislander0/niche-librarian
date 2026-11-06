"""GitHub issues: issues and comments, staff flags, PRs skipped, incremental 'since' per repo."""
from engine.config import SourceConfig
from engine.parsers.base import CrawlContext
from engine.parsers.github_issues import Parser


class DictState(dict):
    def set(self, k, v):
        self[k] = v


class Fake:
    network_requests = 0

    def __init__(self):
        self.calls = []

    def get_json(self, url, params=None, ttl=None, headers=None):
        self.calls.append((url, dict(params or {}), headers))
        if url.endswith("/issues") and params["page"] == 1:
            return [{"number": 7, "title": "DSC fails on 11.3 upgrade", "body": "Error during upgrade to 11.3",
                     "html_url": "https://github.com/Esri/x/issues/7", "state": "closed", "comments": 1,
                     "created_at": "2025-01-02T00:00:00Z", "updated_at": "2025-01-05T00:00:00Z",
                     "user": {"login": "someone", "id": 1}, "author_association": "NONE",
                     "labels": [{"name": "bug"}], "reactions": {"total_count": 3}},
                    {"number": 8, "title": "Add feature", "pull_request": {}, "comments": 0,
                     "updated_at": "2025-01-06T00:00:00Z"}]
        if url.endswith("/issues/7/comments"):
            return [{"id": 99, "body": "Fixed in the next release.", "html_url": "https://github.com/Esri/x/issues/7#c",
                     "created_at": "2025-01-04T00:00:00Z", "updated_at": "2025-01-04T00:00:00Z",
                     "user": {"login": "esri-dev", "id": 2}, "author_association": "MEMBER"}]
        return []


def test_issues_comments_staff_and_incremental_since(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    src = SourceConfig(id="gh", parser="github_issues", source_type="issue",
                       options={"repos": ["Esri/x"], "since": "2024-01-01"})
    f, state = Fake(), DictState()
    docs = list(Parser(CrawlContext(source=src, fetcher=f, state=state)).crawl())
    q, c = docs                                                    # the pull request is skipped
    assert q.doc_id == "gh:Esri/x#7" and q.kind == "question" and q.title == "DSC fails on 11.3 upgrade (x #7)"
    assert q.tags == ["x", "bug"] and q.score == 3 and not q.is_vendor_staff and q.extra["state"] == "closed"
    assert c.parent_id == q.doc_id and c.is_vendor_staff and "next release" in c.content_md
    assert f.calls[0][1]["since"] == "2024-01-01T00:00:00Z"
    assert f.calls[0][2]["Authorization"] == "Bearer test-token"
    assert state["repo:Esri/x"] == "2025-01-06T00:00:00Z"          # next run starts from here
    f2 = Fake()
    list(Parser(CrawlContext(source=src, fetcher=f2, state=state)).crawl())
    assert f2.calls[0][1]["since"] == "2025-01-06T00:00:00Z"
