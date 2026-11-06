#!/usr/bin/env python3
"""Make a structure-only test fixture from pages a parser already read (the local cache).

    python tools/make_fixture.py PACK SOURCE NAME [--roots 3]

Replays SOURCE's parser offline against data/cache/PACK/, records exactly the responses it
reads for the first --roots threads or pages, and writes tests/fixtures/real/NAME.json.gz.

Everything that could identify a site, a person or the content is scrambled. Every word
(text, titles, usernames, URL slugs, JSON string values) becomes a stand-in word of the same
shape, through a mapping keyed by a random secret that is never saved: the fixture can't be
mapped back, not even by trying a dictionary of likely words. Host names become *.example
(except hosts the parser's own code names, such as public APIs). What's kept is the
structure: tags, attributes, class names, JSON keys, numbers and dates, plus the words the
parser's code itself looks for (labels such as "Join Date:", API field names, from the string
literals in its source) and the site template's tag and class names wherever they appear. Scripts, styles and comments are dropped.

The fixture also records what the real parse produced (document count, threads), so a test
fails if a parser change stops reading the real layout correctly.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import hmac
import html
import inspect
import json
import re
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.config import load_pack                                  # noqa: E402
from engine.indexer import _MemoryState, make_fetcher                # noqa: E402
from engine.parsers.base import CrawlContext, load_parser            # noqa: E402

CALENDAR = {w.lower() for w in (
    "January February March April May June July August September October November December "
    "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec Monday Tuesday Wednesday Thursday Friday "
    "Saturday Sunday Mon Tue Wed Thu Fri Sat Sun AM PM UTC GMT Today Yesterday ago").split()}
STRUCTURAL_ATTRS = {"class", "id", "rel", "type", "name", "itemprop", "itemtype", "itemscope", "property",
                    "role", "lang", "dir", "method", "for", "colspan", "rowspan", "width", "height"}
_WORD = re.compile(r"(?<!\\)[A-Za-z][A-Za-z']+")   # not regex escapes such as \b
CREDENTIAL_PARAMS = {"key", "api_key", "apikey", "access_token", "token", "client_secret", "client_id", "auth"}


def strip_credentials(url: str, params: dict | None) -> tuple[str, dict | None]:
    """Never record API keys: drop credential parameters from the URL query and params."""
    import urllib.parse as up
    parts = up.urlsplit(url)
    pairs = up.parse_qsl(parts.query, keep_blank_values=True)
    if any(k.lower() in CREDENTIAL_PARAMS for k, _ in pairs):     # rewrite only when needed
        query = [(k, v) for k, v in pairs if k.lower() not in CREDENTIAL_PARAMS]
        url = up.urlunsplit(parts._replace(query=up.urlencode(query)))
    if params:
        params = {k: v for k, v in params.items() if k.lower() not in CREDENTIAL_PARAMS}
    return url, params
_SYLL = ["ka", "lo", "mi", "te", "ru", "sa", "no", "vi", "pe", "da", "fo", "ze", "bu", "ri", "go", "ha"]


def code_words(parser_cls) -> set[str]:
    """Words in the parser's string literals (not docstrings): what its code matches on."""
    mod = sys.modules[parser_cls.__module__]
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(inspect.getsource(mod))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docs.add(d)
    words = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in docs:
            for tok in re.findall(r"[A-Za-z][A-Za-z_]+", node.value):
                # keep the whole token and its parts: the scrambler sees "include_subcategories"
                # as "include" and "subcategories", and camelCase keys as one word
                words.add(tok.lower())
                words |= {w.lower() for w in re.split(r"_|(?<=[a-z])(?=[A-Z])", tok) if w}
    return words


class Scrambler:
    def __init__(self, keep: set[str], keep_hosts: set[str], handles: set[str] = frozenset()):
        self.secret = secrets.token_bytes(32)          # never saved: the mapping can't be undone
        self.keep, self.keep_hosts = keep, keep_hosts
        self.hosts: dict[str, str] = {}
        # The authors the parser found: replaced first and in full, everywhere, even when a
        # handle is made of kept words ("level1") or single letters ("H-i-v-e").
        names = sorted({h for h in handles if h and h.strip()}, key=len, reverse=True)
        self.handle_re = re.compile("|".join(rf"(?<![A-Za-z0-9]){re.escape(h)}(?![A-Za-z0-9])" for h in names),
                                    re.I) if names else None

    def pseudo_handle(self, h: str) -> str:
        d = hmac.new(self.secret, b"handle:" + h.lower().encode(), hashlib.sha256).digest()
        out = []
        for i, c in enumerate(h):
            b = d[i % len(d)]
            out.append(chr(97 + b % 26).upper() if c.isupper() else chr(97 + b % 26) if c.isalpha()
                       else str(b % 10) if c.isdigit() else c)
        return "".join(out)

    def word(self, w: str) -> str:
        if w.lower() in self.keep or w.lower() in CALENDAR:
            return w
        h = hmac.new(self.secret, w.lower().encode(), hashlib.sha256).digest()
        out, i = "", 0
        while len(out) < len(w):
            out += _SYLL[h[i % len(h)] % len(_SYLL)]
            i += 1
        out = out[:len(w)]
        if w.isupper() and len(w) > 1:
            return out.upper()
        return out.capitalize() if w[0].isupper() else out

    def text(self, s: str) -> str:
        if self.handle_re:
            s = self.handle_re.sub(lambda m: "\x00" + self.pseudo_handle(m.group(0)) + "\x01", s)
            parts = re.split(r"(\x00[^\x01]*\x01)", s)        # don't re-scramble the stand-ins
            return "".join(p[1:-1] if p.startswith("\x00") else _WORD.sub(lambda m: self.word(m.group(0)), p)
                           for p in parts)
        return _WORD.sub(lambda m: self.word(m.group(0)), s)

    def host(self, h: str) -> str:
        key = h.replace("\\", "").lower()          # "docs\.example\.com" in a regex is the same host
        if key in self.keep_hosts or not key:
            return h
        if key not in self.hosts:
            self.hosts[key] = f"site{len(self.hosts) + 1}.example"
        return self.hosts[key].replace(".", "\\.") if "\\." in h else self.hosts[key]

    def url(self, u: str) -> str:
        m = re.match(r"(?i)(https?:)?//([^/?#\s]+)(.*)", u)
        if not m:
            return self.text(u)                    # relative path: scramble the slug words
        return f"{m.group(1) or ''}//{self.host(m.group(2))}{self.text(m.group(3))}"

    def any_url(self, s: str, rest: bool = False) -> str:
        """Rewrite the URLs in s; with rest=True also scramble the text around them (e.g. the
        remainder of a regex that contains a URL)."""
        out, pos = [], 0
        for m in re.finditer(r"https?://[^\s\"'<>)]+", s):
            out += [self.text(s[pos:m.start()]) if rest else s[pos:m.start()], self.url(m.group(0))]
            pos = m.end()
        out.append(self.text(s[pos:]) if rest else s[pos:])
        return "".join(out)

    def json_value(self, v, key: str = ""):
        if isinstance(v, dict):
            return {k: self.json_value(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [self.json_value(x, key) for x in v]
        if isinstance(v, str):
            if re.fullmatch(r"[\d\s:.,/+TZ-]*", v):            # numbers, dates, times
                return v
            if v.lstrip().startswith("<"):                   # HTML inside JSON (post bodies)
                return self.html(v)
            return self.any_url(v, rest=True) if "://" in v else self.text(v)
        return v

    def html(self, page: str) -> str:
        page = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1\s*>", "", page)
        page = re.sub(r"(?s)<!--.*?-->", "", page)
        out, pos = [], 0
        for m in re.finditer(r"<[^>]+>", page):
            out.append(self.text(html.unescape(page[pos:m.start()])).replace("&", "&amp;").replace("<", "&lt;"))
            out.append(self.tag(m.group(0)))
            pos = m.end()
        out.append(self.text(page[pos:]))
        return "".join(out)

    def tag(self, t: str) -> str:
        def attr(m):
            name, q, val = m.group(1), m.group(2), m.group(3)
            low = name.lower()
            if low in STRUCTURAL_ATTRS or low in ("aria-hidden", "aria-expanded") or \
                    low.startswith(("data-node", "data-time", "data-last")):
                return m.group(0)
            raw = html.unescape(val)
            if raw.lstrip().startswith(("{", "[")):          # embedded JSON (e.g. data-props)
                try:
                    new = json.dumps(self.json_value(json.loads(raw)))
                    return f"{name}={q}{html.escape(new, quote=True)}{q}"
                except ValueError:
                    pass
            if low in ("href", "src", "action", "content", "data-url", "srcset") or "://" in raw:
                new = self.any_url(raw) if "://" in raw else (self.text(raw) if low == "content" else self.url(raw))
            else:
                new = self.text(raw)
            return f"{name}={q}{html.escape(new, quote=False).replace(q, '&quot;' if q == chr(34) else '&#39;')}{q}"
        return re.sub(r"""([\w:-]+)=(["'])(.*?)\2""", attr, t, flags=re.S)

    def body(self, text: str) -> str:
        s = text.lstrip()
        if s[:1] in "{[":
            try:
                return json.dumps(self.json_value(json.loads(text)))
            except ValueError:
                pass
        if s.startswith("<?xml") or "<urlset" in s[:300] or "<sitemapindex" in s[:300]:
            return self.any_url(text)
        return self.html(text)


def markup_words(reads: list[dict]) -> set[str]:
    """Tag names and structural attribute values (class, id, ...) in the recorded pages. They are
    kept as they are in the pages, so options that name them (content: {tag, class}) must match."""
    attrs = "|".join(sorted(STRUCTURAL_ATTRS))
    words = set()
    for r in reads:
        b = r["body"]
        words |= {t.lower() for t in re.findall(r"<([A-Za-z][\w-]*)", b)}
        for v in re.findall(rf"""\b(?:{attrs})=["']([^"']*)["']""", b):
            words |= {w.lower() for w in re.findall(r"[A-Za-z]+", v)}
    return words


def record(pack_id: str, source_id: str, roots: int) -> tuple[dict, list[dict], list]:
    pack = load_pack(pack_id)
    src = pack.source(source_id)
    fetcher = make_fetcher(pack, offline=True)
    reads: list[dict] = []
    real_get = fetcher.get_text

    def get_text(url, params=None, ttl=None, **kw):
        body = real_get(url, params, ttl)
        clean_url, clean_params = strip_credentials(url, params)
        reads.append({"url": clean_url, "params": clean_params, "body": body})
        return body

    fetcher.get_text = get_text
    parser_cls = load_parser(src.parser, pack.path)
    docs, n_roots = [], 0
    try:
        for d in parser_cls(CrawlContext(source=src, fetcher=fetcher, state=_MemoryState())).crawl():
            if d.parent_id is None:
                n_roots += 1
                if n_roots > roots:
                    break
            docs.append(d)
    except Exception as e:                       # e.g. the cache runs out: keep what was read
        # Never echo the message: it can contain a full request URL, keys included.
        print(f"replay stopped: {type(e).__name__} after {len(reads)} responses", file=sys.stderr)
    return {"source": src, "parser_cls": parser_cls}, reads, docs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pack")
    ap.add_argument("source")
    ap.add_argument("name")
    ap.add_argument("--roots", type=int, default=3)
    a = ap.parse_args()
    info, reads, docs = record(a.pack, a.source, a.roots)
    src, parser_cls = info["source"], info["parser_cls"]
    if not docs:
        print("nothing parsed from the cache; crawl the source first", file=sys.stderr)
        return 1
    mod_src = inspect.getsource(sys.modules[parser_cls.__module__])
    keep_hosts = set(re.findall(r"https?://([a-z0-9.-]+)", mod_src))
    sc = Scrambler(code_words(parser_cls) | markup_words(reads), keep_hosts,
                   handles={d.author for d in docs if d.author} | {d.author_id for d in docs
                                                                    if d.author_id and not str(d.author_id).isdigit()})
    trim_sitemaps(reads)
    fixture = {
        "parser": src.parser, "source_type": src.source_type, "recorded_at": int(time.time()),
        "options": sc.json_value(src.options),
        "pages": [{"url": sc.url(r["url"]), "params": sc.json_value(r["params"]) if r["params"] else None,
                   "body": sc.body(r["body"])} for r in reads],
        "expect": {"documents": len(docs), "roots": sum(d.parent_id is None for d in docs),
                   "with_author": sum(bool(d.author) for d in docs), "with_date": sum(d.post_date is not None for d in docs),
                   "kinds": sorted({d.kind for d in docs})},
    }
    out = ROOT / "tests" / "fixtures" / "real" / f"{a.name}.json.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(fixture, fh)
    print(f"{out.relative_to(ROOT)}: {len(reads)} responses, {len(docs)} documents, {out.stat().st_size // 1024} KB")
    return 0


def trim_sitemaps(reads: list[dict], extra: int = 0) -> None:
    """Sitemaps list thousands of URLs; keep only the entries the parser went on to read."""
    fetched = {r["url"] for r in reads}
    for r in reads:
        b = r["body"]
        if "<urlset" not in b[:500]:
            continue
        entries = re.findall(r"(?s)<url>.*?</url>", b)
        keep = [e for e in entries if any(u in e for u in fetched)] + \
               [e for e in entries if not any(u in e for u in fetched)][:extra]
        head = b[:b.find("<url>")] if "<url>" in b else b
        r["body"] = head + "".join(keep) + "</urlset>"


if __name__ == "__main__":
    sys.exit(main())
