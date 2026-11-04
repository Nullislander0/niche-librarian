"""Run a pack's parsers and index what they yield. No topic-specific logic lives here."""
from __future__ import annotations

import logging

from engine.config import EMBEDDING, Pack
from engine.crawl.fetcher import BlockedError, FetchError, Fetcher
from engine.index.chunk import chunk_markdown
from engine.index.embed import EmbeddingUnavailable, OllamaEmbedder
from engine.index.store import Store
from engine.parsers.base import CrawlContext, Document, StopCrawl, load_parser
from engine.versions import VersionTagger, version_key

log = logging.getLogger("indexer")


def open_store(pack: Pack, reset_embeddings: bool = False) -> Store:
    return Store(pack.db_path, EMBEDDING["dim"], reset_embeddings=reset_embeddings)


def make_fetcher(pack: Pack) -> Fetcher:
    c = pack.crawler
    return Fetcher(
        user_agent=pack.user_agent,
        cache_dir=pack.cache_dir,
        min_interval=float(c["min_interval_seconds"]),
        cache_ttl=int(c["cache_ttl_seconds"]),
        timeout=int(c["timeout_seconds"]),
    )


def index_document(store: Store, tagger: VersionTagger, doc: Document) -> bool:
    text = "\n".join([doc.title, doc.content_md, " ".join(doc.tags)])
    versions = set(tagger.tag(text))
    if doc.parent_id:  # replies inherit the thread's version context
        versions |= set(store.get_version_tags(doc.parent_id))
    return store.upsert(doc, sorted(versions, key=version_key), chunk_markdown(doc.content_md))


def crawl(pack: Pack, source_ids: list[str] | None = None, max_requests: int | None = None,
          embed: bool = True) -> dict:
    store = open_store(pack)
    fetcher = make_fetcher(pack)
    tagger = VersionTagger(pack.versions)
    report: dict = {}
    try:
        for src in pack.sources:
            if source_ids and src.id not in source_ids:
                continue
            if not src.enabled and not source_ids:
                continue
            ctx = CrawlContext(source=src, fetcher=fetcher, state=store.state_for(src.id),
                               max_requests=max_requests, lookup=store.get_document,
                               log=logging.getLogger(f"crawl.{src.id}"))
            seen = changed = 0
            status = "complete"
            try:
                for doc in load_parser(src.parser, pack.path)(ctx).crawl():
                    seen += 1
                    changed += index_document(store, tagger, doc)
                    if seen % 200 == 0:
                        log.info("%s: %d documents (%d new/changed)", src.id, seen, changed)
            except StopCrawl as e:
                status = f"paused: {e}"
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
