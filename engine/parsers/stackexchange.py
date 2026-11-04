"""Stack Exchange sites via the official API (https://api.stackexchange.com/docs).

This is the worked example to copy when adding an API-backed source. The
shape every parser follows:

  1. Read options from sources.yaml via ctx.options.
  2. Make every request through ctx.fetcher (robots.txt, throttle, cache, UA).
  3. Yield Document objects. The engine does version tagging, chunking,
     indexing and embedding; parsers only fetch and normalise.
  4. Keep resume state in ctx.state so runs are incremental, and call
     ctx.check_budget() before each request.

Options (sources.yaml):
  site: gis                       # api `site` parameter
  site_url: https://gis.stackexchange.com
  tag_tiers:                      # crawled in order, tier by tier
    - {tier: 1, tags: [arcgis-enterprise, arcgis-portal]}
  fetch_author_profiles: true     # extra /users calls for account age
  quota_reserve: 25               # stop when the daily API quota drops below this

Crawl strategy per tag:
  backfill     sort=creation asc, walking a creation-date cursor; resumable.
  incremental  once backfill finishes, sort=activity asc from the last run's
               watermark, so new questions, answers and edits are picked up.
  Date cursors rather than page numbers: keyless clients can't go past page 25.

Set STACKEXCHANGE_KEY in the environment to raise the daily quota from ~300
to 10,000 requests (register an app at stackapps.com). A key is optional.
"""
from __future__ import annotations

import html
import json
import os
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document, StopCrawl

API = "https://api.stackexchange.com/2.3"

FILTER_FIELDS = [
    ".backoff", ".error_id", ".error_message", ".error_name", ".has_more", ".items",
    ".quota_max", ".quota_remaining",
    "question.question_id", "question.title", "question.link", "question.body_markdown",
    "question.creation_date", "question.last_activity_date", "question.last_edit_date",
    "question.score", "question.tags", "question.owner", "question.answers",
    "question.answer_count", "question.view_count", "question.is_answered",
    "question.accepted_answer_id",
    "answer.answer_id", "answer.body_markdown", "answer.creation_date",
    "answer.last_activity_date", "answer.last_edit_date", "answer.score",
    "answer.is_accepted", "answer.owner", "answer.link",
    "shallow_user.user_id", "shallow_user.display_name", "shallow_user.reputation",
    "shallow_user.user_type",
    "user.user_id", "user.creation_date", "user.reputation", "user.display_name",
]


class Parser:
    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        opts = ctx.options
        self.site = opts["site"]
        self.site_url = opts.get("site_url", f"https://{self.site}.stackexchange.com").rstrip("/")
        self.tiers = sorted(opts.get("tag_tiers", []), key=lambda t: t.get("tier", 0))
        self.fetch_profiles = bool(opts.get("fetch_author_profiles", True))
        self.quota_reserve = int(opts.get("quota_reserve", 25))
        self.key = os.environ.get("STACKEXCHANGE_KEY") or None
        self.quota_remaining: int | None = None

    # -- crawl ------------------------------------------------------------

    def crawl(self) -> Iterator[Document]:
        filt = self._filter()
        for tier in self.tiers:
            for tag in tier["tags"]:
                self.ctx.log.info("tier %s tag %s", tier.get("tier"), tag)
                yield from self._crawl_tag(tag, filt)

    def _crawl_tag(self, tag: str, filt: str) -> Iterator[Document]:
        key = f"tag:{self.site}:{tag}"
        st = self.ctx.state.get(key) or {"phase": "backfill", "cursor": 0, "watermark": 0}

        if st["phase"] == "backfill":
            yield from self._walk(tag, filt, key, st, sort="creation", field="creation_date")
            st["phase"], st["cursor"] = "incremental", st["watermark"]
            self.ctx.state.set(key, st)
            return  # incremental starts next run

        # Incremental: everything with activity since the previous run.
        yield from self._walk(tag, filt, key, st, sort="activity", field="last_activity_date", ttl=0)
        st["cursor"] = st["watermark"]
        self.ctx.state.set(key, st)

    def _walk(self, tag: str, filt: str, key: str, st: dict, sort: str, field: str,
              ttl: int | None = None) -> Iterator[Document]:
        """Page through a tag oldest-first using a date cursor instead of page numbers.

        Keyless clients can't request page > 25, and a cursor also survives new posts
        arriving mid-crawl. `min` is inclusive, so the boundary item repeats (harmless:
        indexing is idempotent).
        """
        while True:
            data = self._api("/questions", {
                "tagged": tag, "sort": sort, "order": "asc", "min": st["cursor"] or None,
                "pagesize": 100, "filter": filt,
            }, ttl=ttl)
            items = data["items"]
            yield from self._page(items)
            for q in items:
                st["watermark"] = max(st["watermark"], q.get("last_activity_date", 0))
            if not data.get("has_more") or not items:
                return
            last = items[-1].get(field, 0)
            st["cursor"] = last if last > st["cursor"] else st["cursor"] + 1
            self.ctx.state.set(key, st)

    def _page(self, questions: list[dict]) -> Iterator[Document]:
        profiles = self._profiles(questions) if self.fetch_profiles else {}
        for q in questions:
            yield from self._thread(q, profiles)

    # -- normalise --------------------------------------------------------

    def _thread(self, q: dict, profiles: dict) -> Iterator[Document]:
        src, stype = self.ctx.source.id, self.ctx.source.source_type
        qdoc = f"{src}:q:{q['question_id']}"
        title = html.unescape(q.get("title", ""))
        answers = q.get("answers") or []
        top = max((a.get("score", 0) for a in answers), default=0)

        yield Document(
            doc_id=qdoc, source=src, source_type=stype, kind="question",
            url=q.get("link") or f"{self.site_url}/q/{q['question_id']}",
            title=title, content_md=html.unescape(q.get("body_markdown", "")),
            post_date=q.get("creation_date"), updated_date=q.get("last_activity_date"),
            score=q.get("score"), tags=q.get("tags", []),
            extra={
                "answer_count": q.get("answer_count"), "view_count": q.get("view_count"),
                "is_answered": q.get("is_answered"),
            },
            **self._author(q.get("owner"), profiles),
        )
        for a in answers:
            yield Document(
                doc_id=f"{src}:a:{a['answer_id']}", source=src, source_type=stype, kind="answer",
                url=a.get("link") or f"{self.site_url}/a/{a['answer_id']}",
                title=title, content_md=html.unescape(a.get("body_markdown", "")),
                post_date=a.get("creation_date"), updated_date=a.get("last_activity_date"),
                score=a.get("score"), is_accepted=bool(a.get("is_accepted")),
                is_top_voted=top > 0 and a.get("score", 0) == top,
                parent_id=qdoc, tags=q.get("tags", []),
                **self._author(a.get("owner"), profiles),
            )

    def _author(self, owner: dict | None, profiles: dict) -> dict:
        if not owner or "user_id" not in owner:
            name = html.unescape(owner.get("display_name", "")) if owner else None
            return {"author": name or None}
        uid = str(owner["user_id"])
        return {
            "author": html.unescape(owner.get("display_name", "")),
            "author_id": uid,
            "author_reputation": owner.get("reputation"),
            "author_created": (profiles.get(uid) or {}).get("created"),
        }

    # -- API --------------------------------------------------------------

    def _profiles(self, questions: list[dict]) -> dict:
        """Account creation dates for every author on the page, cached in state."""
        ids = set()
        for q in questions:
            for post in [q, *(q.get("answers") or [])]:
                uid = (post.get("owner") or {}).get("user_id")
                if uid is not None:
                    ids.add(str(uid))
        known = {uid: self.ctx.state.get(f"user:{self.site}:{uid}") for uid in ids}
        missing = sorted(uid for uid, v in known.items() if v is None)
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            data = self._api(f"/users/{';'.join(batch)}", {"pagesize": 100, "filter": self._filter()})
            for u in data["items"]:
                uid = str(u["user_id"])
                known[uid] = {"created": u.get("creation_date"), "reputation": u.get("reputation")}
                self.ctx.state.set(f"user:{self.site}:{uid}", known[uid])
        return {k: v for k, v in known.items() if v}

    def _filter(self) -> str:
        """Create (once) and cache an API filter that returns body_markdown."""
        cached = self.ctx.state.get("filter")
        if cached and cached.get("fields") == FILTER_FIELDS:
            return cached["filter"]
        data = self._api("/filters/create", {
            "include": ";".join(FILTER_FIELDS), "base": "none", "unsafe": "false",
        }, site=False)
        filt = data["items"][0]["filter"]
        self.ctx.state.set("filter", {"filter": filt, "fields": FILTER_FIELDS})
        return filt

    def _api(self, path: str, params: dict, ttl: int | None = None, site: bool = True) -> dict:
        if self.quota_remaining is not None and self.quota_remaining < self.quota_reserve:
            raise StopCrawl(f"API quota low ({self.quota_remaining} left); resume tomorrow")
        self.ctx.check_budget()
        params = dict(params)
        if site:
            params["site"] = self.site
        if self.key:
            params["key"] = self.key
        try:
            data = self.ctx.fetcher.get_json(API + path, params, ttl=ttl)
        except FetchError as e:
            try:
                err = json.loads(e.body)
            except ValueError:
                raise e
            if err.get("error_id") == 502:
                raise StopCrawl("Stack Exchange throttle violation; stopping for today")
            raise FetchError(f"API error {err.get('error_id')} {err.get('error_name')}: "
                             f"{err.get('error_message')}", e.status, e.body)
        if "error_id" in data:
            raise FetchError(f"API error {data['error_id']} {data.get('error_name')}: {data.get('error_message')}")
        if data.get("backoff"):
            self.ctx.fetcher.pause_host(API, data["backoff"])
        if "quota_remaining" in data:
            self.quota_remaining = data["quota_remaining"]
        return data
