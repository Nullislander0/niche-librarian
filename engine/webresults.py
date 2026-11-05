"""Keep web search results in the index: ONLY for search plans that include storage rights.

Brave's standard API terms forbid storing results or building a database from them; plans
"with storage rights" allow it. Declare yours in the pack's sources.yaml:

    web:
      storage_rights: true     # my search API plan includes the right to store results

With it on, new results from web watches are indexed as the `web_results` source (title,
snippet, URL, date: what the API returned, nothing fetched), searchable by agents and
counted by `librarian signals`. With it off (the default) results are only shown.
`librarian web-results purge` deletes every stored result, e.g. if the plan ends.
"""
from __future__ import annotations

from datetime import datetime, timezone

from engine.config import Pack
from engine.parsers.base import Document

SOURCE_ID = "web_results"


def enabled(pack: Pack) -> bool:
    return bool((pack.web or {}).get("storage_rights"))


def _ts(s: str | None) -> int | None:
    try:
        dt = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()) if dt else None


def to_document(hit: dict, query: str, watch: str | None = None) -> Document:
    from engine.websearch import fingerprint
    from engine.webpages import host
    return Document(
        doc_id=f"{SOURCE_ID}:{fingerprint(hit['url'])}", source=SOURCE_ID, source_type="web",
        kind=hit.get("kind") or "web", url=hit["url"], title=hit.get("title") or hit["url"],
        content_md=hit.get("snippet") or "", post_date=_ts(hit.get("published")),
        tags=[host(hit["url"])],
        extra={"query": query, "watch": watch, "backend": hit.get("backend"), "age": hit.get("age"),
               "forum": hit.get("forum")})


def store(pack: Pack, hits: list[dict], query: str, watch: str | None = None, store=None,
          embed: bool = True) -> int:
    """Index results if the pack declares storage rights. Returns how many were stored."""
    if not enabled(pack) or not hits:
        return 0
    from engine.indexer import embed_missing, index_document, open_store
    from engine.versions import VersionTagger
    own = store is None
    store = store or open_store(pack)
    try:
        tagger = VersionTagger(pack.versions)
        n = sum(1 for h in hits if h.get("url") and index_document(store, tagger, to_document(h, query, watch)))
        if embed and n:
            embed_missing(store)
        return n
    finally:
        if own:
            store.close()


def purge(pack: Pack, store=None) -> int:
    from engine.indexer import open_store
    own = store is None
    store = store or open_store(pack)
    try:
        ids = [r[0] for r in store.db.execute("SELECT doc_id FROM documents WHERE source=?", (SOURCE_ID,))]
        for d in ids:
            store.delete_document(d)
        store.db.execute("DELETE FROM signal_labels WHERE doc_id LIKE ?", (f"{SOURCE_ID}:%",)) \
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='signal_labels'").fetchone() else None
        store.db.commit()
        return len(ids)
    finally:
        if own:
            store.close()
