"""Rerank fused candidates using pack-defined signals (ranking.yaml).

score = retrieval ** w.retrieval * max(0.1, 1 + sum(w.signal * signal_value))

retrieval is the fused keyword+vector score (0..1); each signal is 0..1 and mismatch is a
penalty. Signals scale relevance rather than add to it, so a well-voted answer to a
different question can't outrank a relevant one.

No backlink weighting, no keyword-density rewards, no blanket freshness bias:
recency only counts when the query is version-sensitive.
"""
from __future__ import annotations

import math
import re
import time

from engine.versions import family, version_matches

DEFAULT_WEIGHTS = {
    "retrieval": 1.0,
    "version_exact": 0.35,
    "version_family": 0.15,
    "version_mismatch": -0.2,
    "recency": 0.15,
    "source_trust": 0.15,
    "accepted_answer": 0.25,
    "top_voted": 0.1,
    "vendor_staff": 0.2,
    "recognized_expert": 0.1,
    "first_hand": 0.2,
    "author_reputation": 0.1,
    "account_age": 0.05,
    "votes": 0.1,
}


def rrf(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal rank fusion, normalised so the best candidate scores 1.0."""
    scores: dict[int, float] = {}
    for ranks in rank_lists:
        for pos, rid in enumerate(ranks):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + pos + 1)
    top = max(scores.values(), default=0) or 1.0
    return {rid: s / top for rid, s in scores.items()}


class Ranker:
    def __init__(self, cfg: dict, source_trust: dict[str, float]):
        cfg = cfg or {}
        self.w = {**DEFAULT_WEIGHTS, **(cfg.get("weights") or {})}
        self.rrf_k = int(cfg.get("rrf_k", 60))
        self.candidates = int(cfg.get("candidates", 60))
        self.max_chunks_per_doc = int(cfg.get("max_chunks_per_doc", 1))
        rec = cfg.get("recency") or {}
        self.half_life = float(rec.get("half_life_days", 730))
        self.sensitive = [t.lower() for t in rec.get("version_sensitive_terms", [])]
        auth = cfg.get("author") or {}
        self.author_default = auth.get("default", {"reputation_ceiling": 10000, "age_ceiling_years": 8})
        self.author_per_source = auth.get("per_source", {})
        self.vote_ceiling = float((cfg.get("votes") or {}).get("ceiling", 50))
        self.first_hand = [re.compile(p, re.I | re.M) for p in cfg.get("first_hand_patterns", [])]
        self.first_hand_saturation = int(cfg.get("first_hand_saturation", 3))
        self.source_trust = source_trust

    def version_sensitive(self, query: str, query_versions: list[str]) -> bool:
        q = query.lower()
        return bool(query_versions) or any(t in q for t in self.sensitive)

    def signals(self, c: dict, query_versions: list[str], sensitive: bool, now: float) -> dict[str, float]:
        s: dict[str, float] = {}
        tags = c.get("version_tags") or []
        if query_versions and tags:
            if any(version_matches(t, v) for t in tags for v in query_versions):
                s["version_exact"] = 1.0
            elif {family(t) for t in tags} & {family(v) for v in query_versions}:
                s["version_family"] = 1.0
            else:
                s["version_mismatch"] = 1.0
        if sensitive and c.get("post_date"):
            age_days = max(0.0, (now - c["post_date"]) / 86400)
            s["recency"] = 0.5 ** (age_days / self.half_life)
        s["source_trust"] = float(self.source_trust.get(c["source"], 0.5))
        if c.get("is_accepted"):
            s["accepted_answer"] = 1.0
        if c.get("is_top_voted"):
            s["top_voted"] = 1.0
        if c.get("is_vendor_staff"):
            s["vendor_staff"] = 1.0
        if (c.get("extra") or {}).get("recognized_expert"):
            s["recognized_expert"] = 1.0
        if self.first_hand:
            hits = sum(1 for p in self.first_hand if p.search(c.get("content_md") or c.get("text") or ""))
            if hits:
                s["first_hand"] = min(1.0, hits / self.first_hand_saturation)
        a = {**self.author_default, **(self.author_per_source.get(c["source"]) or {})}
        rep = c.get("author_reputation")
        if rep and rep > 0:
            s["author_reputation"] = min(1.0, math.log10(rep + 1) / math.log10(a["reputation_ceiling"] + 1))
        if c.get("author_created"):
            years = (now - c["author_created"]) / (365.25 * 86400)
            s["account_age"] = max(0.0, min(1.0, years / a["age_ceiling_years"]))
        if c.get("score") and c["score"] > 0:
            s["votes"] = min(1.0, math.log1p(c["score"]) / math.log1p(self.vote_ceiling))
        return s

    def rank(self, candidates: list[dict], fused: dict[int, float], query: str,
             query_versions: list[str], limit: int, explain: bool = False) -> list[dict]:
        now = time.time()
        sensitive = self.version_sensitive(query, query_versions)
        for c in candidates:
            sig = self.signals(c, query_versions, sensitive, now)
            retrieval = fused.get(c["rowid"], 0.0)
            boost = sum(self.w.get(k, 0) * v for k, v in sig.items())
            c["score_total"] = retrieval ** self.w["retrieval"] * max(0.1, 1 + boost)
            if explain:
                c["explain"] = {"retrieval": round(retrieval, 3), **{k: round(v, 3) for k, v in sig.items()}}
        candidates.sort(key=lambda c: c["score_total"], reverse=True)
        out, per_doc = [], {}
        for c in candidates:
            n = per_doc.get(c["doc_id"], 0)
            if n >= self.max_chunks_per_doc:
                continue
            per_doc[c["doc_id"]] = n + 1
            out.append(c)
            if len(out) >= limit:
                break
        return out
