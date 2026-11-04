"""Hybrid search (FTS5 + vectors), rerank, and fetch. Shared by the CLI and the MCP server."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from engine.config import EMBEDDING, Pack
from engine.index.embed import EmbeddingUnavailable, OllamaEmbedder
from engine.index.store import Filters, Store
from engine.rank.rerank import Ranker, rrf
from engine.versions import VersionTagger

SNIPPET_CHARS = 280


def parse_date(s: str | None, end: bool = False) -> int | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end and len(s) == 10:  # whole day
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def fmt_date(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "unknown"


def snippet(text: str, query: str, n: int = SNIPPET_CHARS) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    terms = [t for t in re.findall(r"\w{3,}", query.lower())]
    low = flat.lower()
    pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, pos - n // 4)
    out = flat[start:start + n]
    return ("…" if start else "") + out + ("…" if start + n < len(flat) else "")


class Searcher:
    def __init__(self, pack: Pack, store: Store | None = None, embedder: OllamaEmbedder | None = None):
        self.pack = pack
        self.store = store or Store(pack.db_path, EMBEDDING["dim"])
        self.embedder = embedder if embedder is not None else OllamaEmbedder.from_settings(EMBEDDING)
        self.tagger = VersionTagger(pack.versions)
        self.ranker = Ranker(pack.ranking, {s.id: s.trust for s in pack.sources})

    def search(self, query: str, limit: int = 10, version: str | None = None, version_mode: str = "boost",
               date_from: str | None = None, date_to: str | None = None, source_type: str | None = None,
               source: str | None = None, explain: bool = False) -> dict:
        filters = Filters(date_from=parse_date(date_from), date_to=parse_date(date_to, end=True),
                          version=version if version_mode == "filter" else None,
                          source_type=source_type, source=source)
        query_versions = [version] if version else self.tagger.detect_query(query)
        k = self.ranker.candidates

        lists = [self.store.fts_search(query, filters, k)]
        mode = "keyword"
        if self.embedder and self.embedder.available():
            try:
                lists.append(self.store.vec_search(self.embedder.embed_query(query), filters, k))
                mode = "hybrid"
            except EmbeddingUnavailable:
                pass
        fused = rrf(lists, self.ranker.rrf_k)
        candidates = self.store.load_chunks(list(fused))
        ranked = self.ranker.rank(candidates, fused, query, query_versions, limit, explain)

        results = []
        for c in ranked:
            r = {
                "chunk_id": c["chunk_id"],
                "title": c["title"],
                "url": c["url"],
                "date": fmt_date(c["post_date"]),
                "versions": c["version_tags"],
                "source": c["source"],
                "source_type": c["source_type"],
                "kind": c["kind"],
                "score": round(c["score_total"], 3),
                "flags": [f for f in ("accepted", "top_voted", "vendor_staff") if c.get(f"is_{f}")]
                + (["expert"] if (c.get("extra") or {}).get("recognized_expert") else []),
                "snippet": snippet(c["text"], query),
            }
            if explain:
                r["explain"] = c["explain"]
            results.append(r)
        return {"query": query, "mode": mode, "query_versions": query_versions, "results": results}

    def fetch(self, chunk_id: str, scope: str = "document") -> str | None:
        """Full text as markdown. scope: chunk | document | thread."""
        chunk = self.store.get_chunk(chunk_id)
        doc_id = chunk["doc_id"] if chunk else chunk_id.split("#")[0]
        doc = self.store.get_document(doc_id)
        if not doc:
            return None
        if scope == "chunk" and chunk:
            return _header(doc, chunk_id) + "\n\n" + chunk["text"]
        if scope == "thread":
            root = doc["parent_id"] or doc["doc_id"]
            parts = [_render(d) for d in self.store.get_thread(root)]
            return f"<!-- thread {root}; requested {chunk_id} -->\n\n" + "\n\n---\n\n".join(parts)
        return _render(doc, chunk_id)

    def info(self) -> dict:
        s = self.store.stats()
        s["pack"] = {"id": self.pack.id, "name": self.pack.name, "description": self.pack.description,
                     "sources": {x.id: {"type": x.source_type, "parser": x.parser, "enabled": x.enabled}
                                 for x in self.pack.sources}}
        return s


def _header(doc: dict, cite: str | None = None) -> str:
    flags = [f for f, k in (("accepted answer", "is_accepted"), ("top voted", "is_top_voted"),
                            ("vendor staff", "is_vendor_staff")) if doc.get(k)]
    if (doc.get("extra") or {}).get("recognized_expert"):
        flags.append("recognized expert")
    lines = [
        f"# {doc['title']}" + (f" ({doc['kind']})" if doc["kind"] != "question" else ""),
        f"- cite: {cite or doc['doc_id']}",
        f"- url: {doc['url']}",
        f"- source: {doc['source']} ({doc['source_type']})",
        f"- posted: {fmt_date(doc['post_date'])}; last activity: {fmt_date(doc['updated_date'])}",
        f"- author: {doc['author'] or 'unknown'}"
        + (f" (reputation {doc['author_reputation']})" if doc.get("author_reputation") else "")
        + (f" [{doc['extra']['author_label']}]" if (doc.get("extra") or {}).get("author_label") else ""),
        f"- score: {doc['score']}" + (f"; {', '.join(flags)}" if flags else ""),
        f"- versions: {', '.join(doc['version_tags']) or 'none detected'}",
    ]
    return "\n".join(lines)


def _render(doc: dict, cite: str | None = None) -> str:
    return _header(doc, cite) + "\n\n" + (doc["content_md"] or "").strip()
