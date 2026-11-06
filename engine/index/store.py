"""One SQLite file per pack: documents, chunks, FTS5 keyword index, sqlite-vec vectors, crawl state."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import logging

import sqlite_vec

from engine.parsers.base import Document


def _sqlite_module():
    """The SQLite binding to use: Python's own when it can load extensions (needed for
    sqlite-vec), otherwise sqlean.py, a drop-in build with extension loading enabled. The
    python.org, Apple and GitHub Actions Pythons on macOS can't load extensions; sqlean.py is
    installed there automatically (see pyproject.toml). LIBRARIAN_SQLITE=sqlean|sqlite3 forces one."""
    import os
    choice = os.environ.get("LIBRARIAN_SQLITE", "").lower()
    if choice == "sqlite3":
        return sqlite3
    if choice != "sqlean" and hasattr(sqlite3.connect(":memory:"), "enable_load_extension"):
        return sqlite3
    try:
        import sqlean
        return sqlean
    except ImportError:
        return sqlite3


sqlite = _sqlite_module()
from engine.versions import version_matches

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_type TEXT,
    kind TEXT,
    url TEXT,
    title TEXT,
    post_date INTEGER,
    updated_date INTEGER,
    author TEXT,
    author_id TEXT,
    author_reputation INTEGER,
    author_created INTEGER,
    is_accepted INTEGER DEFAULT 0,
    is_top_voted INTEGER DEFAULT 0,
    is_vendor_staff INTEGER DEFAULT 0,
    score INTEGER,
    parent_id TEXT,
    tags TEXT,
    version_tags TEXT,
    content_md TEXT,
    content_hash TEXT,
    extra TEXT,
    indexed_at INTEGER,
    first_seen INTEGER
);
CREATE INDEX IF NOT EXISTS documents_parent ON documents(parent_id);
CREATE INDEX IF NOT EXISTS documents_source ON documents(source);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    chunk_id TEXT UNIQUE NOT NULL,
    doc_id TEXT NOT NULL REFERENCES documents(doc_id),
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS crawl_state (
    source TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (source, key)
);
"""

log = logging.getLogger(__name__)

_STOP = set("a an and are as at be but by can do does for from how i in is it my of on or "
            "the this to was what when where which who why with you".split())


@dataclass
class Filters:
    date_from: int | None = None
    date_to: int | None = None
    version: str | None = None  # strict filter; soft boosting happens in the ranker
    source_type: str | None = None
    source: str | None = None
    first_seen_since: int | None = None  # only documents that entered the index after this time

    def sql(self, alias: str = "d") -> tuple[str, list]:
        where, args = [], []
        if self.date_from is not None:
            where.append(f"{alias}.post_date >= ?")
            args.append(self.date_from)
        if self.date_to is not None:
            where.append(f"{alias}.post_date <= ?")
            args.append(self.date_to)
        if self.source_type:
            where.append(f"{alias}.source_type = ?")
            args.append(self.source_type)
        if self.first_seen_since is not None:
            where.append(f"{alias}.first_seen >= ?")
            args.append(self.first_seen_since)
        if self.source:
            where.append(f"{alias}.source = ?")
            args.append(self.source)
        if self.version:
            where.append(f"EXISTS (SELECT 1 FROM json_each({alias}.version_tags) v "
                         f"WHERE v.value = ? OR v.value LIKE ?)")
            args += [self.version, self.version + ".%"]
        return (" AND ".join(where) or "1"), args


def fts_query(text: str, extra_phrases: list[str] | None = None) -> str:
    """Turn free text into a safe FTS5 OR-query. Dotted tokens (versions) become phrases;
    extra_phrases (e.g. synonym expansions) are added as whole phrases."""
    terms = []
    for tok in re.findall(r"[\w][\w.\-]*", text.lower()):
        tok = tok.strip(".-")
        if not tok or tok in _STOP:
            continue
        parts = [p for p in re.split(r"[.\-]", tok) if p]
        phrase = " ".join(parts)
        if phrase and f'"{phrase}"' not in terms:
            terms.append(f'"{phrase}"')
    for p in extra_phrases or []:
        words = " ".join(re.findall(r"\w+", p.lower()))
        if words and f'"{words}"' not in terms:
            terms.append(f'"{words}"')
    return " OR ".join(terms)


def _load_sqlite_vec(db) -> bool:
    """Load sqlite-vec. If neither Python's SQLite nor sqlean.py can load extensions, the index
    runs keyword-only instead of failing."""
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        return True
    except (AttributeError, sqlite3.OperationalError, sqlite.OperationalError) as e:
        log.warning("sqlite-vec unavailable (%s): semantic search disabled, keyword search only. "
                    "Install sqlean.py (`pip install sqlean.py`) or use a Python whose SQLite can "
                    "load extensions.", e)
        return False


class Store:
    def __init__(self, path: Path | str, dim: int, reset_embeddings: bool = False):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the MCP server calls tools from worker threads (serialised by a lock).
        self.db = sqlite.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite.Row
        self.vec_ok = _load_sqlite_vec(self.db)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(documents)")}
        if "first_seen" not in cols:  # older databases: add it, backfilled from indexed_at
            self.db.execute("ALTER TABLE documents ADD COLUMN first_seen INTEGER")
            self.db.execute("UPDATE documents SET first_seen = indexed_at")
        self.db.execute("CREATE INDEX IF NOT EXISTS documents_first_seen ON documents(first_seen)")
        self.db.execute("CREATE INDEX IF NOT EXISTS documents_url ON documents(url)")
        self.dim = dim
        if reset_embeddings:  # e.g. after switching embedding model
            self.db.execute("DROP TABLE IF EXISTS chunks_vec")
            self.db.execute("UPDATE chunks SET embedded=0")
            self.db.execute("DELETE FROM meta WHERE key='embedding_dim'")
        if not self.vec_ok:
            self.db.commit()
            return
        stored = self.db.execute("SELECT value FROM meta WHERE key='embedding_dim'").fetchone()
        if stored and int(stored[0]) != dim:
            raise RuntimeError(f"index was built with embedding dim {stored[0]}, settings say {dim}; "
                               f"rebuild with `librarian embed --reset`")
        self.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING "
                        f"vec0(embedding float[{dim}] distance_metric=cosine)")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('embedding_dim', ?)", (str(dim),))
        self.db.commit()

    def close(self):
        self.db.close()

    # -- crawl state ------------------------------------------------------

    def state_for(self, source: str) -> "SourceState":
        return SourceState(self, source)

    # -- write ------------------------------------------------------------

    def get_version_tags(self, doc_id: str) -> list[str]:
        row = self.db.execute("SELECT version_tags FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else []

    def upsert(self, doc: Document, version_tags: list[str], chunks: list[str]) -> bool:
        """Insert or update a document. Returns True if its content changed (re-chunked)."""
        content_hash = hashlib.sha256(f"{doc.title}\n{doc.content_md}".encode()).hexdigest()
        old = self.db.execute("SELECT content_hash FROM documents WHERE doc_id=?", (doc.doc_id,)).fetchone()
        d = asdict(doc)
        d.update(
            tags=json.dumps(doc.tags), extra=json.dumps(doc.extra), version_tags=json.dumps(version_tags),
            is_accepted=int(doc.is_accepted), is_top_voted=int(doc.is_top_voted),
            is_vendor_staff=int(doc.is_vendor_staff), content_hash=content_hash, indexed_at=int(time.time()),
            first_seen=int(time.time()),
        )
        cols = list(d)
        self.db.execute(
            f"INSERT INTO documents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(doc_id) DO UPDATE SET "
            f"{','.join(f'{c}=excluded.{c}' for c in cols if c not in ('doc_id', 'first_seen'))}",
            [d[c] for c in cols],
        )
        changed = not old or old[0] != content_hash
        if changed:
            self._delete_chunks(doc.doc_id)
            for i, text in enumerate(chunks):
                cur = self.db.execute("INSERT INTO chunks (chunk_id, doc_id, ord, text) VALUES (?,?,?,?)",
                                      (f"{doc.doc_id}#c{i}", doc.doc_id, i, text))
                self.db.execute("INSERT INTO chunks_fts (rowid, title, text) VALUES (?,?,?)",
                                (cur.lastrowid, doc.title, text))
        self.db.commit()
        return changed

    def _delete_chunks(self, doc_id: str) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        for rid in ids:
            self.db.execute("DELETE FROM chunks_fts WHERE rowid=?", (rid,))
            if self.vec_ok:
                self.db.execute("DELETE FROM chunks_vec WHERE rowid=?", (rid,))
        self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    def delete_document(self, doc_id: str) -> bool:
        self._delete_chunks(doc_id)
        n = self.db.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,)).rowcount
        self.db.commit()
        return bool(n)

    def find_urls(self, urls: list[str]) -> dict[str, str]:
        """Which of these URLs are already indexed: {url: doc_id}. Ignores a trailing slash."""
        out = {}
        for u in urls:
            base = u.split("#")[0].rstrip("/")
            r = self.db.execute("SELECT doc_id FROM documents WHERE url IN (?, ?) ORDER BY parent_id IS NOT NULL "
                                "LIMIT 1", (base, base + "/")).fetchone()
            if r:
                out[u] = r[0]
        return out

    def refresh_top_voted(self, source: str) -> None:
        """A reply is top voted if it has the thread's highest positive score."""
        self.db.execute("""
            UPDATE documents SET is_top_voted = (
                parent_id IS NOT NULL AND COALESCE(score, 0) > 0 AND score = (
                    SELECT MAX(d2.score) FROM documents d2 WHERE d2.parent_id = documents.parent_id))
            WHERE source = ?""", (source,))
        self.db.commit()

    def chunks_missing_embeddings(self, limit: int = 256) -> list:
        return self.db.execute(
            "SELECT c.id, c.text, d.title FROM chunks c JOIN documents d USING(doc_id) "
            "WHERE c.embedded=0 ORDER BY c.id LIMIT ?", (limit,)).fetchall()

    def put_embeddings(self, rows: list[tuple[int, list[float]]]) -> None:
        if not self.vec_ok:
            return
        for rid, vec in rows:
            self.db.execute("DELETE FROM chunks_vec WHERE rowid=?", (rid,))
            self.db.execute("INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                            (rid, sqlite_vec.serialize_float32(vec)))
            self.db.execute("UPDATE chunks SET embedded=1 WHERE id=?", (rid,))
        self.db.commit()

    # -- read -------------------------------------------------------------

    def fts_search(self, query: str, filters: Filters, k: int, extra_phrases: list[str] | None = None) -> list[int]:
        q = fts_query(query, extra_phrases)
        if not q:
            return []
        where, args = filters.sql()
        rows = self.db.execute(
            f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid JOIN documents d USING(doc_id) "
            f"WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts, 2.0, 1.0) LIMIT ?",
            [q, *args, k]).fetchall()
        return [r[0] for r in rows]

    def vec_search(self, vec: list[float], filters: Filters, k: int) -> list[int]:
        if not self.vec_ok:
            return []
        # vec0 KNN can't join on our filters, so over-fetch and filter afterwards.
        overfetch = k * 4 if any(asdict(filters).values()) else k
        hits = self.db.execute(
            "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(vec), overfetch)).fetchall()
        ids = [h[0] for h in hits]
        if not ids or not any(asdict(filters).values()):
            return ids[:k]
        where, args = filters.sql()
        keep = {r[0] for r in self.db.execute(
            f"SELECT c.id FROM chunks c JOIN documents d USING(doc_id) "
            f"WHERE c.id IN ({','.join('?' * len(ids))}) AND {where}", [*ids, *args])}
        return [i for i in ids if i in keep][:k]

    def load_chunks(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        rows = self.db.execute(
            f"SELECT c.id AS rowid, c.chunk_id, c.ord, c.text, d.* FROM chunks c JOIN documents d USING(doc_id) "
            f"WHERE c.id IN ({','.join('?' * len(ids))})", ids).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            d["version_tags"] = json.loads(d["version_tags"] or "[]")
            d["extra"] = json.loads(d["extra"] or "{}")
            out.append(d)
        return out

    def get_chunk(self, chunk_id: str) -> dict | None:
        r = self.db.execute("SELECT id FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        return self.load_chunks([r[0]])[0] if r else None

    def get_document(self, doc_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["version_tags"] = json.loads(d["version_tags"] or "[]")
        d["extra"] = json.loads(d["extra"] or "{}")
        return d

    def get_thread(self, root_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT doc_id FROM documents WHERE doc_id=? OR parent_id=? "
            "ORDER BY doc_id=? DESC, is_accepted DESC, is_vendor_staff DESC, "
            "COALESCE(json_extract(extra, '$.recognized_expert'), 0) DESC, COALESCE(score, 0) DESC, post_date",
            (root_id, root_id, root_id)).fetchall()
        return [self.get_document(r[0]) for r in rows]

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchall()
        versions: dict[str, int] = {}
        for (vt,) in q("SELECT version_tags FROM documents"):
            for v in json.loads(vt or "[]"):
                versions[v] = versions.get(v, 0) + 1
        return {
            "documents": q("SELECT COUNT(*) FROM documents")[0][0],
            "chunks": q("SELECT COUNT(*) FROM chunks")[0][0],
            "embedded_chunks": q("SELECT COUNT(*) FROM chunks WHERE embedded=1")[0][0],
            "by_source": {r[0]: {"documents": r[1], "first": r[2], "last": r[3]} for r in q(
                "SELECT source, COUNT(*), date(MIN(post_date),'unixepoch'), date(MAX(post_date),'unixepoch') "
                "FROM documents GROUP BY source")},
            "by_kind": {r[0]: r[1] for r in q("SELECT kind, COUNT(*) FROM documents GROUP BY kind")},
            "untagged_version": q("SELECT COUNT(*) FROM documents WHERE version_tags='[]'")[0][0],
            "versions": dict(sorted(versions.items(), key=lambda kv: -kv[1])),
        }


class SourceState:
    """Key/value crawl state scoped to one source (implements parsers.base.StateStore)."""

    def __init__(self, store: Store, source: str):
        self.store, self.source = store, source

    def get(self, key: str, default=None):
        r = self.store.db.execute("SELECT value FROM crawl_state WHERE source=? AND key=?",
                                  (self.source, key)).fetchone()
        return json.loads(r[0]) if r else default

    def items(self) -> list[tuple[str, object]]:
        return [(k, json.loads(v)) for k, v in self.store.db.execute(
            "SELECT key, value FROM crawl_state WHERE source=?", (self.source,))]

    def set(self, key: str, value) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO crawl_state VALUES (?,?,?)",
                              (self.source, key, json.dumps(value)))
        self.store.db.commit()


__all__ = ["Filters", "Store", "fts_query", "version_matches"]
