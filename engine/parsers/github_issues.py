"""GitHub issues and their comments, through the GitHub REST API. One Document per issue or comment.

Options (sources.yaml):
  repos: [owner/name, ...]                  # e.g. Esri/arcgis-powershell-dsc
  since: "2018-01-01"                       # first run: issues updated since then
  include_pull_requests: false              # PRs are code review, not Q&A
  staff_associations: [OWNER, MEMBER]       # author_association -> is_vendor_staff (the repo's org)
  min_comments: 0                           # skip issues with fewer comments (0 keeps all)

Token: GITHUB_TOKEN in the environment (a read-only, public-repositories token is enough).
It is sent as a header, never cached. Without one GitHub allows 60 requests an hour.

An issue is the root ("question"); comments are replies. Labels become tags; reactions on
an issue or comment become its score. Each run asks for issues updated since the last run
(per repo), so closed-and-reopened or newly commented issues are picked up.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document

API = "https://api.github.com"
PAGE = 100


def _ts(s: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None
    except ValueError:
        return None


class Parser:
    POSITION_KEY_PREFIXES = ("repo:",)

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.repos = list(o.get("repos", []))
        self.since = str(o.get("since", "2018-01-01"))
        self.prs = bool(o.get("include_pull_requests", False))
        self.staff = {a.upper() for a in o.get("staff_associations", ["OWNER", "MEMBER"])}
        self.min_comments = int(o.get("min_comments", 0))
        token = os.environ.get("GITHUB_TOKEN")
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                        **({"Authorization": f"Bearer {token}"} if token else {})}
        if not token:
            ctx.log.warning("GITHUB_TOKEN not set: GitHub allows only 60 requests an hour without one")

    def _get(self, path: str, params: dict) -> list | dict:
        self.ctx.check_budget()
        return self.ctx.fetcher.get_json(f"{API}{path}", params, ttl=3600, headers=self.headers)

    def crawl(self) -> Iterator[Document]:
        for repo in self.repos:
            key = f"repo:{repo}"
            since = self.ctx.state.get(key) or f"{self.since}T00:00:00Z"
            newest = since
            page = 1
            while True:
                issues = self._get(f"/repos/{repo}/issues", {"state": "all", "sort": "updated", "direction": "asc",
                                                             "since": since, "per_page": PAGE, "page": page})
                if not isinstance(issues, list) or not issues:
                    break
                for issue in issues:
                    newest = max(newest, issue.get("updated_at") or newest)
                    if "pull_request" in issue and not self.prs:
                        continue
                    if issue.get("comments", 0) < self.min_comments:
                        continue
                    yield from self._issue(repo, issue)
                if len(issues) < PAGE:
                    break
                page += 1
            self.ctx.state.set(key, newest)

    def _doc(self, repo, item, doc_id, kind, parent, title, labels, state) -> Document:
        user = item.get("user") or {}
        assoc = str(item.get("author_association", "")).upper()
        return Document(
            doc_id=doc_id, source=self.ctx.source.id, source_type=self.ctx.source.source_type, kind=kind,
            url=item.get("html_url") or "", title=title, content_md=item.get("body") or "",
            post_date=_ts(item.get("created_at")), updated_date=_ts(item.get("updated_at")),
            author=user.get("login"), author_id=str(user.get("id")) if user.get("id") else None,
            score=(item.get("reactions") or {}).get("total_count"), parent_id=parent,
            is_vendor_staff=assoc in self.staff, tags=[repo.split("/")[-1], *labels],
            extra={"repo": repo, "state": state, "author_association": assoc or None,
                   "recognized_expert": assoc == "CONTRIBUTOR"})

    def _issue(self, repo: str, issue: dict) -> Iterator[Document]:
        labels = [lb.get("name") for lb in issue.get("labels") or [] if isinstance(lb, dict) and lb.get("name")]
        state = issue.get("state")
        title = f"{issue.get('title', '')} ({repo.split('/')[-1]} #{issue.get('number')})"
        root = f"{self.ctx.source.id}:{repo}#{issue.get('number')}"
        yield self._doc(repo, issue, root, "question", None, title, labels, state)
        if not issue.get("comments"):
            return
        page = 1
        while True:
            try:
                comments = self._get(f"/repos/{repo}/issues/{issue['number']}/comments", {"per_page": PAGE, "page": page})
            except FetchError as e:
                if e.status in (404, 410):
                    return
                raise
            if not isinstance(comments, list) or not comments:
                return
            for c in comments:
                yield self._doc(repo, c, f"{root}:c{c.get('id')}", "reply", root, title, labels, state)
            if len(comments) < PAGE:
                return
            page += 1
