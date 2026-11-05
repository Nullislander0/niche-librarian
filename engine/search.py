"""Hybrid search (FTS5 + vectors), rerank, and fetch. Shared by the CLI and the MCP server."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from engine.config import EMBEDDING, Pack
from engine.index.embed import EmbeddingUnavailable, OllamaEmbedder
from engine.index.store import Filters, Store
from engine.rank.cross import CrossReranker
from engine.rank.rerank import Ranker, rrf
from engine.synonyms import Synonyms
from engine.versions import VersionTagger, version_key

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


def tokens(text: str | None) -> int:
    """Rough token estimate (~4 characters per token) so agents can budget context."""
    return (len(text or "") + 3) // 4


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
        self.synonyms = Synonyms(pack.synonyms)
        self.cross = CrossReranker.from_config(pack.ranking.get("reranker"))

    def search(self, query: str, limit: int = 10, version: str | None = None, version_mode: str = "boost",
               date_from: str | None = None, date_to: str | None = None, source_type: str | None = None,
               source: str | None = None, explain: bool = False, new_since: str | int | None = None) -> dict:
        """new_since: only documents first indexed at/after this date (ISO) or unix time."""
        filters = Filters(date_from=parse_date(date_from), date_to=parse_date(date_to, end=True),
                          version=version if version_mode == "filter" else None,
                          source_type=source_type, source=source,
                          first_seen_since=new_since if isinstance(new_since, int) else parse_date(new_since))
        query_versions = [version] if version else self.tagger.detect_query(query)
        k = self.ranker.candidates

        expansions = self.synonyms.expand(query)
        lists = [self.store.fts_search(query, filters, k, expansions)]
        mode = "keyword"
        if self.store.vec_ok and self.embedder and self.embedder.available():
            try:
                lists.append(self.store.vec_search(self.embedder.embed_query(query), filters, k))
                mode = "hybrid"
            except EmbeddingUnavailable:
                pass
        fused = rrf(lists, self.ranker.rrf_k)
        candidates = self.store.load_chunks(list(fused))
        if self.cross:
            fused = self.cross.rescore(query, candidates, fused)
            mode += "+rerank"
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
                "flags": (["official"] if c.get("source_type") in self.ranker.authoritative else [])
                + [f for f in ("accepted", "top_voted", "vendor_staff") if c.get(f"is_{f}")]
                + (["expert"] if (c.get("extra") or {}).get("recognized_expert") else []),
                "snippet": snippet(c["text"], query),
                "tokens": {"chunk": tokens(c["text"]), "document": tokens(c["content_md"])},
            }
            if explain:
                r["explain"] = c["explain"]
            results.append(r)
        return {"query": query, "mode": mode, "query_versions": query_versions,
                "expanded_with": expansions, "results": results}

    def fetch(self, chunk_id: str, scope: str = "document", max_tokens: int | None = None) -> str | None:
        """Full text as markdown. scope: chunk | document | thread.

        max_tokens caps the output (approximate). Threads are ordered question, accepted answer,
        vendor staff, recognized experts, then by votes; posts beyond the budget are listed by id.
        A long document is cut at a chunk boundary and the remaining chunk ids are listed."""
        chunk = self.store.get_chunk(chunk_id)
        doc_id = chunk["doc_id"] if chunk else chunk_id.split("#")[0]
        doc = self.store.get_document(doc_id)
        if not doc:
            return None
        if scope == "chunk" and chunk:
            return _header(doc, chunk_id) + "\n\n" + chunk["text"]
        if scope == "thread":
            root = doc["parent_id"] or doc["doc_id"]
            posts = self.store.get_thread(root)
            parts, used, omitted = [], 0, []
            for d in posts:
                text = _render(d)
                if omitted or (max_tokens and parts and used + tokens(text) > max_tokens):
                    omitted.append(d)  # keep priority order: once over budget, list the rest
                    continue
                parts.append(text)
                used += tokens(text)
            body = "\n\n---\n\n".join(parts)
            head = f"<!-- thread {root}; {len(posts)} posts; showing {len(parts)} (~{used} tokens); requested {chunk_id} -->"
            if omitted:
                body += "\n\n---\n\n" + f"{len(omitted)} more replies not shown (budget {max_tokens} tokens); " \
                        "fetch any by id: " + ", ".join(
                            f"{d['doc_id']}#c0 (score {d['score']}, {fmt_date(d['post_date'])})" for d in omitted[:30])
            return head + "\n\n" + body
        text = _render(doc, chunk_id)
        if max_tokens and tokens(text) > max_tokens:
            chunks = self.store.db.execute("SELECT chunk_id, text FROM chunks WHERE doc_id=? ORDER BY ord",
                                           (doc["doc_id"],)).fetchall()
            out, used, rest = [_header(doc, chunk_id)], tokens(_header(doc, chunk_id)), []
            for cid, ctext in chunks:
                if rest or (used + tokens(ctext) > max_tokens and len(out) > 1):
                    rest.append(cid)
                    continue
                out.append(ctext)
                used += tokens(ctext)
            text = "\n\n".join(out)
            if rest:
                text += f"\n\n[{len(rest)} more sections not shown; fetch with scope='chunk': {', '.join(rest)}]"
        return text

    def outline(self, source: str | None = None) -> str:
        """A map of what the index holds, for an agent to read before searching."""
        db = self.store.db
        names = {}
        for s in self.pack.sources:
            for tier in s.options.get("category_tiers", []):
                for c in tier.get("categories", []):
                    if isinstance(c, dict):
                        names[(s.id, str(c["id"]))] = c.get("name", str(c["id"]))
        lines = [f"# {self.pack.name}", self.pack.description.strip(), ""]
        for s in self.pack.sources:
            if source and s.id != source:
                continue
            row = db.execute("SELECT COUNT(*), MIN(post_date), MAX(post_date) FROM documents WHERE source=?",
                             (s.id,)).fetchone()
            if not row[0]:
                continue
            kinds = ", ".join(f"{k} {n:,}" for k, n in db.execute(
                "SELECT kind, COUNT(*) FROM documents WHERE source=? GROUP BY kind ORDER BY 2 DESC", (s.id,)))
            lines.append(f"## {s.id} ({s.source_type}, trust {s.trust}): {row[0]:,} posts, "
                         f"{fmt_date(row[1])} to {fmt_date(row[2])}")
            lines.append(f"- kinds: {kinds}")
            cats = db.execute("SELECT json_extract(extra,'$.category_id') c, COUNT(*) FROM documents "
                              "WHERE source=? AND c IS NOT NULL GROUP BY c ORDER BY 2 DESC", (s.id,)).fetchall()
            if cats:
                lines.append("- boards: " + "; ".join(f"{names.get((s.id, str(c)), c)} {n:,}" for c, n in cats))
            tags = db.execute("SELECT t.value, COUNT(*) FROM documents d, json_each(d.tags) t "
                              "WHERE d.source=? AND d.parent_id IS NULL GROUP BY t.value ORDER BY 2 DESC LIMIT 15",
                              (s.id,)).fetchall()
            if tags:
                lines.append("- top tags (threads): " + ", ".join(f"{t} {n:,}" for t, n in tags))
            lines.append("")
        vers = db.execute("SELECT v.value, COUNT(*), MAX(d.post_date) FROM documents d, json_each(d.version_tags) v "
                          + ("WHERE d.source=? " if source else "") + "GROUP BY v.value",
                          (source,) if source else ()).fetchall()
        if vers:
            lines.append("## Version coverage (posts mentioning each version; newest post)")
            for v, n, last in sorted(vers, key=lambda r: version_key(r[0]), reverse=True):
                if n >= 20:
                    lines.append(f"- {v}: {n:,} (latest {fmt_date(last)})")
        return "\n".join(lines)

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
        f"- author: {doc['author']}"
        + (f" (reputation {doc['author_reputation']})" if doc.get("author_reputation") else "")
        + (f" [{doc['extra']['author_label']}]" if (doc.get("extra") or {}).get("author_label") else "")
        if doc.get("author") else None,
        f"- score: {doc['score']}" + (f"; {', '.join(flags)}" if flags else "")
        if doc.get("score") is not None else (f"- flags: {', '.join(flags)}" if flags else None),
        f"- versions: {', '.join(doc['version_tags']) or 'none detected'}",
    ]
    details = (doc.get("extra") or {}).get("details") or {}
    if details:
        lines.append("- details: " + "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in details.items()))
    return "\n".join(line for line in lines if line)


def _render(doc: dict, cite: str | None = None) -> str:
    return _header(doc, cite) + "\n\n" + (doc["content_md"] or "").strip()
