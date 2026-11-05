"""Discourse forums via their built-in JSON API (every Discourse site has one).

Options (sources.yaml):
  base_url: https://forum.example.org
  categories: [{id: 7, name: General}]   # parent categories include their subcategories
  since: "2022-01-01"                    # first run goes back this far (by last activity)
  revisit_days: 14                       # later runs re-check topics active this recently
  max_topics_per_run: 5000
  vendor_groups: 'iXsystems|Docker'      # user group/flair -> is_vendor_staff
  expert_trust_level: 4                  # trust level 4 ("leader") or site staff -> expert
  max_posts_per_topic: 300               # huge topics: the opening post plus the newest replies

Walk: each category's latest list (newest activity first) down to a date floor. A topic
is fetched only when its post count changed since we last saw it, so an interrupted
first run resumes cheaply (listing pages again, skipping finished topics) and later runs
cost little. Long topics load the rest of their posts 20 at a time.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterator

from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown


def _ts(s: str | None) -> int | None:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def _likes(post: dict) -> int:
    likes = next((a.get("count", 0) for a in post.get("actions_summary") or [] if a.get("id") == 2), 0)
    return max(likes, post.get("reaction_users_count") or 0, post.get("like_count") or 0)


class Parser:
    POSITION_KEY_PREFIXES = ("cat:", "topic:", "slugs")

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.base = o["base_url"].rstrip("/")
        self.categories = o.get("categories", [])
        self.since = _ts(str(o.get("since", "2000-01-01")) + "T00:00:00+00:00")
        self.revisit = int(o.get("revisit_days", 14)) * 86400
        self.max_topics = int(o.get("max_topics_per_run", 5000))
        self.vendor = re.compile(o["vendor_groups"], re.I) if o.get("vendor_groups") else None
        self.expert_tl = int(o.get("expert_trust_level", 4))
        self.max_posts = int(o.get("max_posts_per_topic", 300))
        self.done = 0

    def _get(self, path: str, ttl: int | None = 0) -> dict:
        self.ctx.check_budget()
        return self.ctx.fetcher.get_json(f"{self.base}{path}", ttl=ttl)

    def _slugs(self) -> dict[int, str]:
        cats = self._get("/categories.json?include_subcategories=true", ttl=86400)["category_list"]["categories"]
        out = {}
        for c in cats:
            out[c["id"]] = c["slug"]
            for s in c.get("subcategory_list") or []:
                out[s["id"]] = f"{c['slug']}/{s['slug']}"
        return out

    def crawl(self) -> Iterator[Document]:
        slugs = self._slugs()
        for cat in self.categories:
            cid = cat["id"] if isinstance(cat, dict) else cat
            if cid not in slugs:
                self.ctx.log.warning("category %s not found on %s", cid, self.base)
                continue
            self.ctx.log.info("category %s (%s)", cid, slugs[cid])
            yield from self._category(cid, slugs[cid])
            if self.done >= self.max_topics:
                self.ctx.log.info("max_topics_per_run (%d) reached", self.max_topics)
                return

    def _category(self, cid: int, slug: str) -> Iterator[Document]:
        key = f"cat:{cid}"
        st = self.ctx.state.get(key) or {"watermark": 0}
        floor = st["watermark"] - self.revisit if st["watermark"] else self.since
        newest, page = st["watermark"], 0
        while True:
            data = self._get(f"/c/{slug}/{cid}/l/latest.json?page={page}")
            topics = (data.get("topic_list") or {}).get("topics") or []
            stop = not topics
            for t in topics:
                bumped = _ts(t.get("bumped_at")) or 0
                if bumped < floor:
                    if t.get("pinned"):
                        continue  # pinned topics sit on top regardless of age
                    stop = True
                    break
                tkey = f"topic:{t['id']}"
                if self.ctx.state.get(tkey) != t.get("highest_post_number"):
                    yield from self._topic(t["id"])
                    self.ctx.state.set(tkey, t.get("highest_post_number"))
                    self.done += 1
                newest = max(newest, bumped)
                if self.done >= self.max_topics:
                    return  # partial: watermark not advanced, next run resumes
            if stop or not data["topic_list"].get("more_topics_url"):
                break
            page += 1
        st["watermark"] = newest
        self.ctx.state.set(key, st)

    def _topic(self, tid: int) -> Iterator[Document]:
        t = self._get(f"/t/{tid}.json", ttl=None)
        stream = t.get("post_stream") or {}
        ids = list(stream.get("stream") or [])
        truncated = len(ids) > self.max_posts
        if truncated:  # keep the opening post and the newest replies
            ids = ids[:1] + ids[-(self.max_posts - 1):]
        keep = set(ids)
        posts = [p for p in stream.get("posts") or [] if p["id"] in keep]
        have = {p["id"] for p in posts}
        missing = [i for i in ids if i not in have]
        for i in range(0, len(missing), 20):
            ids = "&".join(f"post_ids[]={pid}" for pid in missing[i:i + 20])
            more = self._get(f"/t/{tid}/posts.json?{ids}", ttl=None)
            posts += (more.get("post_stream") or {}).get("posts") or []
        posts.sort(key=lambda p: p.get("post_number", 0))
        title = t.get("title") or t.get("fancy_title") or ""
        tags = [x["name"] if isinstance(x, dict) else x for x in t.get("tags") or []]
        root = f"{self.ctx.source.id}:t:{tid}"
        for p in posts:
            if p.get("post_type", 1) != 1 or p.get("hidden"):
                continue  # skip moderator actions / small notices and hidden posts
            first = p.get("post_number") == 1
            label = p.get("user_title") or p.get("primary_group_name") or p.get("flair_name") or ""
            groups = " ".join(filter(None, [p.get("primary_group_name"), p.get("flair_name"), p.get("user_title")]))
            yield Document(
                doc_id=root if first else f"{self.ctx.source.id}:p:{p['id']}",
                source=self.ctx.source.id, source_type=self.ctx.source.source_type,
                kind="question" if first else ("answer" if p.get("accepted_answer") else "reply"),
                url=f"{self.base}/t/{t.get('slug', 'topic')}/{tid}/{p.get('post_number', 1)}",
                title=title, content_md=html_to_markdown(p.get("cooked", ""), base_url=self.base + "/"),
                post_date=_ts(p.get("created_at")), updated_date=_ts(p.get("updated_at") or p.get("created_at")),
                author=p.get("username"), author_id=str(p["user_id"]) if p.get("user_id") is not None else None,
                author_reputation=p.get("trust_level"), score=_likes(p),
                is_accepted=bool(p.get("accepted_answer")),
                is_vendor_staff=bool(self.vendor and groups and self.vendor.search(groups)),
                parent_id=None if first else root, tags=tags,
                extra={"author_label": label or None,
                       "recognized_expert": bool(p.get("staff") or (p.get("trust_level") or 0) >= self.expert_tl),
                       "category_id": t.get("category_id"), "views": t.get("views") if first else None,
                       "truncated_to": self.max_posts if truncated and first else None},
            )
