"""Pseudonymize authors at indexing time, for packs about people (e.g. patient forums).

    privacy:
      authors: pseudonymize        # keep (default) | pseudonymize | drop

With `pseudonymize`, a post's author becomes a stable pseudonym such as "P-7f3a91": a keyed
hash (HMAC-SHA256) of the source and the author's id, so the same person gets the same
pseudonym every week and in every thread, and the handle itself is never stored. Handles in
the text are replaced too: quote attributions ("[name said:](...)"), @mentions, and names of
other authors seen in the same crawl. Emails and phone numbers are removed. Clinical and
demographic details in the text (age, conditions, treatments) are kept: they are the signal.
Profile numbers are coarsened (reputation and post counts to bands, join date to a year).

Handles are only held in memory during a run. A live crawl scrubs bare mentions of authors it has
seen in that run (quotes, @mentions, greetings and attributions always); `librarian rebuild`
replays the cache twice, learning every handle first.

The key lives in data/secrets/pseudonym.key (git-ignored, created on first use). Back it
up: a new key gives everyone new pseudonyms. Pseudonymized is not anonymous: post text and
URLs can lead back to the original post.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timezone

from engine.config import DATA_DIR

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_QUOTE = re.compile(r"\[([^\]\n]{1,60}?) said:\]")
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_.-]{2,40})")
# Links to member profiles: "[name](https://forum/member/15180-name)", "/members/name.123/", "/u/name".
_PROFILE_LINK = re.compile(r"\[([^\]\n]{1,60})\]\((https?://[^)\s]*/(?:members?|users?|u|profile|profiles)/[^)\s]*)\)")
_PROFILE_URL = re.compile(r"https?://[^\s)\]]*/(?:members?|users?|u|profile|profiles)/[^\s)\]]+")
_GREETING = (r"(?:Hi|Hey|Hello|Welcome|Thanks|Thank you|Dear|Congrats|Originally [Pp]osted by|Quote from|"
             r"Posted by),?\s+(?:\*\*)?")
_ATTRIBUTION = r"\s+(?:added \d|asked|wrote|posted|replied|says|said)\b"
_CHEMICAL = re.compile(r"\d.*-|-.*\d")           # 5-HT2A, 2C-B: notation, not handles
_WORDS: set[str] | None = None


def _dictionary() -> set[str]:
    """System word list (Linux/macOS /usr/share/dict/words), for telling handles from words."""
    global _WORDS
    if _WORDS is None:
        try:
            with open("/usr/share/dict/words", encoding="utf-8", errors="ignore") as fh:
                _WORDS = {w.strip().lower() for w in fh}
        except OSError:
            _WORDS = set()
    return _WORDS


def _handle_shaped(handle: str, text: str) -> bool:
    """Safe to replace wherever it appears as a word: not an ordinary word, not chemical notation,
    not the text of a link to a topic (e.g. a drug-wiki link like [haloperidol](.../wiki/...))."""
    words = _dictionary()
    shaped = len(handle) >= 5 or (len(handle) >= 3 and bool(re.search(r"[a-z][A-Z]|[A-Z]{2}[a-z]|\d|_", handle)))
    return (shaped and bool(words) and handle.lower() not in words and not _CHEMICAL.search(handle)
            and not re.search(rf"\[{re.escape(handle)}\]\(", text, re.I))


def key_path():
    return DATA_DIR / "secrets" / "pseudonym.key"


def load_key() -> bytes:
    env = os.environ.get("LIBRARIAN_PSEUDONYM_KEY")
    if env:
        return env.encode()
    p = key_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return p.read_text(encoding="utf-8").strip().encode()


def band(n: int | None) -> int | None:
    """0, 10, 100, 1000, 10000: the order of magnitude, enough to tell newcomers from veterans."""
    if n is None:
        return None
    return 0 if n < 10 else 10 ** min(4, len(str(int(n))) - 1)


class Pseudonymizer:
    def __init__(self, key: bytes | None = None):
        self.key = key or load_key()
        self.seen: dict[str, dict[str, str]] = {}    # source -> handle -> pseudonym (this run only)

    def pseudonym(self, source: str, ident: str) -> str:
        return "P-" + hmac.new(self.key, f"{source}\x00{ident}".encode(), hashlib.sha256).hexdigest()[:6]

    def learn(self, source: str, handle: str) -> str:
        """Remember a handle for this run (in memory only) so later text can be scrubbed of it."""
        pn = self.pseudonym(source, "name:" + handle.lower())
        self.seen.setdefault(source, {})[handle] = pn
        return pn

    def _for_handle(self, source: str, handle: str) -> str:
        known = self.seen.get(source, {})
        return known.get(handle) or self.pseudonym(source, "name:" + handle.lower())

    def scrub(self, source: str, text: str) -> str:
        if not text:
            return text
        text = _QUOTE.sub(lambda m: f"[{self._for_handle(source, m.group(1).strip())} said:]", text)
        text = _PROFILE_LINK.sub(lambda m: m.group(1) if m.group(1).startswith("P-")
                                 else self._for_handle(source, m.group(1).strip().lstrip("@")), text)
        text = _PROFILE_URL.sub("[profile link removed]", text)
        # Known handles first (they may contain spaces: "@Luis Pereira"), longest first:
        # @mentions, greetings ("Hi, name,"), attributions ("name asked", "name added 5 minutes
        # later"), and bare uses of names that can't be ordinary words.
        for handle in sorted(self.seen.get(source, {}), key=len, reverse=True):
            pn, h = self.seen[source][handle], re.escape(handle)
            text = re.sub(rf"(?<![\w@])@{h}(?!\w)", "@" + pn, text, flags=re.I)
            text = re.sub(rf"({_GREETING}){h}(?!\w)", rf"\g<1>{pn}", text, flags=re.I)
            text = re.sub(rf"(?<![\w@]){h}(?={_ATTRIBUTION})", pn, text, flags=re.I)
            text = re.sub(rf"(?im)^([\s*_-]*){h}([\s*_.!-]*)$", rf"\g<1>{pn}\g<2>", text)          # sign-off line
            text = re.sub(rf"(?<![\w@]){h}(?=['’]s\s+(?:original\s+)?(?:post|posting|reply|thread|comment|message|question|story)\b)",
                          pn, text, flags=re.I)                                                    # "name's post"
            if _handle_shaped(handle, text):
                text = re.sub(rf"(?<![\w@/\[-]){h}(?![\w-])", pn, text)
        text = _MENTION.sub(lambda m: m.group(0) if m.group(1).startswith("P-")
                            else "@" + self._for_handle(source, m.group(1)), text)
        text = _EMAIL.sub("[email removed]", text)
        return _PHONE.sub("[phone removed]", text)

    def apply(self, doc, mode: str = "pseudonymize"):
        """Pseudonymize (or drop) a Document's author in place, and scrub its text."""
        if mode not in ("pseudonymize", "drop"):
            return doc
        src = doc.source
        if doc.author or doc.author_id:
            # Keyed on the handle, so quotes and @mentions of a person match their own posts.
            ident = "name:" + doc.author.lower() if doc.author else f"id:{doc.author_id}"
            p = self.pseudonym(src, ident) if mode == "pseudonymize" else None
            if doc.author and p:
                self.seen.setdefault(src, {})[doc.author] = p
            doc.author, doc.author_id = p, p
        doc.author_reputation = band(doc.author_reputation)
        if doc.author_created:
            doc.author_created = int(datetime(datetime.fromtimestamp(doc.author_created, timezone.utc).year, 1, 1,
                                              tzinfo=timezone.utc).timestamp())
        doc.content_md = self.scrub(src, doc.content_md)
        doc.title = self.scrub(src, doc.title)
        for k in ("post_count", "author_posts", "messages"):
            if isinstance(doc.extra.get(k), int):
                doc.extra[k] = band(doc.extra[k])
        doc.extra["pseudonymized"] = mode
        return doc


def apply_to_store(pack, store, tagger=None) -> dict:
    """Pseudonymize documents already in the index (e.g. after turning the setting on).
    Changed documents are re-chunked; run `librarian embed` afterwards."""
    import json
    from engine.indexer import index_document
    from engine.parsers.base import Document
    from engine.versions import VersionTagger
    mode = pack.author_mode
    if mode == "keep":
        return {"changed": 0, "note": "privacy.authors is 'keep' for this pack"}
    tagger = tagger or VersionTagger(pack.versions)
    p = Pseudonymizer()
    rows = store.db.execute("SELECT * FROM documents WHERE author IS NOT NULL OR author_id IS NOT NULL "
                            "ORDER BY post_date").fetchall()
    rows = [dict(r) for r in rows if '"pseudonymized"' not in (r["extra"] or "")]
    for r in rows:                         # learn every handle first, so mentions anywhere match
        if r["author"]:
            p.seen.setdefault(r["source"], {})[r["author"]] = p.pseudonym(r["source"], "name:" + r["author"].lower())
    fields = set(Document.__dataclass_fields__)
    changed = 0
    for r in rows:
        d = {k: v for k, v in r.items() if k in fields}
        d["tags"], d["extra"] = json.loads(d["tags"] or "[]"), json.loads(d["extra"] or "{}")
        for b in ("is_accepted", "is_top_voted", "is_vendor_staff"):
            d[b] = bool(d[b])
        doc = p.apply(Document(**d), mode)
        changed += 1
        index_document(store, tagger, doc)
    return {"pseudonymized": changed, "mode": mode}


def _leak_patterns(handle: str, text: str) -> list[re.Pattern]:
    """How a handle could still appear: as a bare word if it can't be an ordinary word, and in
    the forms the scrubber handles (mentions, greetings, attributions, sign-offs, possessives)."""
    h = re.escape(handle)
    pats = [rf"(?<![\w@])@{h}(?!\w)", rf"{_GREETING}{h}(?!\w)", rf"(?<![\w@]){h}(?={_ATTRIBUTION})",
            rf"(?m)^[\s*_-]*{h}[\s*_.!-]*$", rf"(?<![\w@]){h}['’]s\s+(?:original\s+)?(?:post|posting|reply|thread)\b",
            rf"\[{h} said:\]"]
    if _handle_shaped(handle, text):
        pats.append(rf"(?<![\w@/\[-]){h}(?![\w-])")
    return [re.compile(p, re.I) for p in pats]


def audit(pack, store, sources: list[str] | None = None, offline_fetcher=None) -> dict:
    """Check a pseudonymized pack's index for anything that identifies a poster.

    Original handles are recovered by replaying the crawl from the local cache (no network)
    and held in memory only. Reports counts and document ids, never the handles themselves.
    """
    from engine.indexer import _MemoryState, make_fetcher
    from engine.parsers.base import CrawlContext, load_parser
    if pack.author_mode == "keep":
        return {"checked": 0, "leaks": 0, "note": "privacy.authors is 'keep' for this pack"}
    fetcher = offline_fetcher or make_fetcher(pack, offline=True)
    report: dict = {"sources": {}, "leaks": 0}
    for src in pack.sources:
        if src.source_type == "data" or (sources and src.id not in sources):
            continue
        rows = store.db.execute("SELECT doc_id, title, content_md FROM documents WHERE source=?", (src.id,)).fetchall()
        if not rows:
            continue
        handles: set[str] = set()
        try:
            for d in load_parser(src.parser, pack.path)(CrawlContext(source=src, fetcher=fetcher,
                                                                     state=_MemoryState())).crawl():
                if d.author:
                    handles.add(d.author)
        except Exception:      # replay stops at the first page not in the cache; what was read still counts
            pass
        chunks: dict[str, list[str]] = {}
        for doc_id, text in store.db.execute(
                "SELECT c.doc_id, c.text FROM chunks c JOIN documents d ON d.doc_id = c.doc_id WHERE d.source=?", (src.id,)):
            chunks.setdefault(doc_id, []).append(text)
        found: dict[str, list[str]] = {}
        for doc_id, title, content in rows:
            text = "\n".join([title or "", content or "", *chunks.get(doc_id, [])])
            kinds = []
            if _PROFILE_URL.search(text) or _PROFILE_LINK.search(text):
                kinds.append("profile link")
            if _EMAIL.search(text):
                kinds.append("email")
            if _PHONE.search(text):
                kinds.append("phone")
            if any(p.search(text) for h in handles for p in _leak_patterns(h, text)):
                kinds.append("handle")
            if kinds:
                found[doc_id] = kinds
        authors = store.db.execute("SELECT COUNT(*) FROM documents WHERE source=? AND author IS NOT NULL "
                                   "AND author NOT LIKE 'P-%'", (src.id,)).fetchone()[0]
        report["sources"][src.id] = {"documents": len(rows), "handles_known": len(handles),
                                     "raw_author_fields": authors, "documents_with_leaks": len(found),
                                     "leaks": {d: k for d, k in list(found.items())[:20]}}
        report["leaks"] += len(found) + authors
    return report
