"""Run a pack's parsers and index what they yield. No topic-specific logic lives here."""
from __future__ import annotations

import json
import logging

from engine.config import EMBEDDING, Pack
from engine.crawl.fetcher import BlockedError, CacheMiss, FetchError, Fetcher
from engine.index.chunk import chunk_markdown
from engine.index.embed import EmbeddingUnavailable, OllamaEmbedder
from engine.index.store import Store
from engine.parsers.base import CrawlContext, Document, StopCrawl, load_parser
from engine.versions import VersionTagger, version_key

log = logging.getLogger("indexer")


def open_store(pack: Pack, reset_embeddings: bool = False) -> Store:
    return Store(pack.db_path, EMBEDDING["dim"], reset_embeddings=reset_embeddings)


def make_fetcher(pack: Pack, offline: bool = False) -> Fetcher:
    c = pack.crawler
    return Fetcher(
        user_agent=pack.user_agent,
        cache_dir=pack.cache_dir,
        min_interval=float(c["min_interval_seconds"]),
        cache_ttl=int(c["cache_ttl_seconds"]),
        timeout=int(c["timeout_seconds"]),
        offline=offline,
    )


def index_document(store: Store, tagger: VersionTagger, doc: Document) -> bool:
    text = "\n".join([doc.title, doc.content_md, " ".join(doc.tags)])
    versions = set(tagger.tag(text)) | set(doc.extra.get("versions") or [])  # parser-declared, e.g. docs
    if doc.parent_id:  # replies inherit the thread's version context
        versions |= set(store.get_version_tags(doc.parent_id))
    return store.upsert(doc, sorted(versions, key=version_key), chunk_markdown(doc.content_md))


class _MemoryState(dict):
    def set(self, key, value):
        self[key] = value


def crawl(pack: Pack, source_ids: list[str] | None = None, max_requests: int | None = None,
          embed: bool = True, offline: bool = False) -> dict:
    """Fetch and index. offline=True replays saved API responses from data/cache (a rebuild):
    no network, and crawl state starts fresh in memory so the real resume points are untouched.
    Unchanged documents keep their chunks and embeddings."""
    store = open_store(pack)
    fetcher = make_fetcher(pack, offline=offline)
    tagger = VersionTagger(pack.versions)
    report: dict = {}
    try:
        for src in pack.sources:
            if source_ids and src.id not in source_ids:
                continue
            if not src.enabled and not source_ids:
                continue
            parser_cls = load_parser(src.parser, pack.path)
            state = store.state_for(src.id)
            if offline:  # replay from the start, keeping non-position state (API filter ids, profiles)
                prefixes = getattr(parser_cls, "POSITION_KEY_PREFIXES", ("",))
                state = _MemoryState({k: v for k, v in state.items() if not k.startswith(tuple(prefixes))})
            ctx = CrawlContext(source=src, fetcher=fetcher, state=state,
                               max_requests=max_requests, lookup=store.get_document,
                               log=logging.getLogger(f"crawl.{src.id}"))
            seen = changed = 0
            status = "complete"
            try:
                for doc in parser_cls(ctx).crawl():
                    seen += 1
                    changed += index_document(store, tagger, doc)
                    if seen % 200 == 0:
                        log.info("%s: %d documents (%d new/changed)", src.id, seen, changed)
            except StopCrawl as e:
                status = f"paused: {e}"
            except CacheMiss as e:
                status = f"replayed until first uncached request ({e})"
            except BlockedError as e:
                status = f"BLOCKED: {e}. Stopped this source; not retrying."
                log.error("%s blocked: %s", src.id, e)
            except FetchError as e:
                status = f"error: {e}"
                log.error("%s failed: %s", src.id, e)
            store.refresh_top_voted(src.id)
            report[src.id] = {"documents_seen": seen, "new_or_changed": changed,
                              "network_requests": ctx.requests_made, "status": status}
            log.info("%s: %s", src.id, report[src.id])
        if embed:
            report["embedded_chunks"] = embed_missing(store)
    finally:
        store.close()
    return report


def retag(pack: Pack) -> dict:
    """Recompute version tags from stored content (after editing versions.yaml). No network."""
    store = open_store(pack)
    tagger = VersionTagger(pack.versions)
    db = store.db
    changed = 0
    try:
        # Roots first so replies can inherit their thread's tags.
        rows = db.execute("SELECT doc_id, parent_id, title, content_md, tags, version_tags FROM documents "
                          "ORDER BY parent_id IS NOT NULL").fetchall()
        for r in rows:
            text = "\n".join([r["title"] or "", r["content_md"] or "", " ".join(json.loads(r["tags"] or "[]"))])
            versions = set(tagger.tag(text))
            if r["parent_id"]:
                versions |= set(store.get_version_tags(r["parent_id"]))
            new = json.dumps(sorted(versions, key=version_key))
            if new != r["version_tags"]:
                db.execute("UPDATE documents SET version_tags=? WHERE doc_id=?", (new, r["doc_id"]))
                changed += 1
        db.commit()
    finally:
        store.close()
    return {"documents": len(rows), "retagged": changed}


def embed_missing(store: Store, batch: int = 256) -> int | str:
    embedder = OllamaEmbedder.from_settings(EMBEDDING)
    if not embedder.available():
        return "skipped (Ollama unavailable); run `librarian embed` later"
    total = 0
    while rows := store.chunks_missing_embeddings(batch):
        texts = [f"{r['title']}\n\n{r['text']}" for r in rows]
        try:
            vecs = embedder.embed_documents(texts)
        except EmbeddingUnavailable as e:
            log.error("embedding stopped: %s", e)
            break
        store.put_embeddings([(r["id"], v) for r, v in zip(rows, vecs)])
        total += len(rows)
        log.info("embedded %d chunks", total)
    return total
