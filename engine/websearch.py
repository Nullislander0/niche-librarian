"""Live web search backends for web watches (needle alerts across the open web).

Brave Search API terms (checked 2026-11-05): results may not be stored, cached or kept in
a database beyond transient use, and may not be used to train or improve AI models. So
web watch results are only shown, never written to the index or report files. "Seen"
tracking keeps one-way SHA-256 fingerprints of URLs, not the results themselves.

Key: BRAVE_API_KEY in the environment (https://api-dashboard.search.brave.com).

Quota: every Brave response carries the plan's limits in X-RateLimit-Limit / -Remaining /
-Reset ("per second, per month"). The latest figures are kept in data/websearch_usage.json
(counts only). Agent searches (the MCP `web_search` tool) stop once the month's remaining
quota falls below the share kept back for watches: LIBRARIAN_WEB_AGENT_SHARE, default 0.75,
leaves the last quarter for watches. Calls made elsewhere with the same key are counted too,
because the numbers come from Brave.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import re
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "niche-librarian/0.1 (personal research index)"
AGENT_SHARE = float(os.environ.get("LIBRARIAN_WEB_AGENT_SHARE", "0.75"))


class WebSearchError(Exception):
    pass


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def fingerprint(url: str) -> str:
    """Same page, same fingerprint: ignores scheme, "www.", a trailing slash and #fragment."""
    p = urllib.parse.urlsplit(url.strip())
    h = (p.hostname or "").removeprefix("www.")
    key = f"{h}{p.path.rstrip('/')}{'?' + p.query if p.query else ''}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


_LAST_CALL: dict[str, float] = {}   # per quota key: Brave allows ~1 request/second per key


class BraveSearch:
    name = "brave"
    quota = "brave"   # the key's monthly quota, shared by every Brave endpoint
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, key: str | None = None, min_interval: float = 1.1):
        self.key = key or os.environ.get("BRAVE_API_KEY")
        self.min_interval = min_interval

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, count: int = 20, freshness: str | None = None,
               site: str | None = None) -> list[dict]:
        """freshness: pd / pw / pm / py (past day/week/month/year) or YYYY-MM-DDtoYYYY-MM-DD."""
        q = f"{query} site:{site}" if site else query
        params = {"q": q, "count": min(count, 20), "result_filter": "web,discussions"}
        if freshness:
            params["freshness"] = freshness
        data = self._get(params)
        out = []
        for kind in ("discussions", "web"):
            for r in (data.get(kind) or {}).get("results") or []:
                self._add(out, r, "discussion" if kind == "discussions" else "web")
        return out

    def _add(self, out: list, r: dict, kind: str) -> None:
        url = r.get("url")
        if url and not any(o["url"] == url for o in out):
            out.append({"kind": kind, "url": url, "title": _clean(r.get("title")),
                        "snippet": _clean(r.get("description")),
                        "age": r.get("age") or r.get("page_age") or "", "backend": self.name})
            if r.get("page_age"):
                out[-1]["published"] = r["page_age"]
            if (r.get("data") or {}).get("forum_name"):
                out[-1]["forum"] = r["data"]["forum_name"]

    def _get(self, params: dict) -> dict:
        if not self.key:
            raise WebSearchError("BRAVE_API_KEY is not set")
        req = urllib.request.Request(f"{self.endpoint}?{urllib.parse.urlencode(params)}", headers={
            "Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": UA,
            "X-Subscription-Token": self.key})
        for attempt in (1, 2):
            wait = _LAST_CALL.get(self.quota, 0) + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    record_usage(self.quota, r.headers)
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 2:
                    self._fail(e)
                time.sleep(2)   # per-second limit: one retry
            except OSError as e:
                raise WebSearchError(f"Brave API unreachable: {e}")
            finally:
                _LAST_CALL[self.quota] = time.time()
        return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)

    @staticmethod
    def _fail(e) -> None:
        raise WebSearchError(f"Brave API HTTP {e.code}"
                             + (" (check BRAVE_API_KEY / plan)" if e.code in (401, 403) else "")
                             + (" (rate limited; try again shortly)" if e.code == 429 else ""))


class BraveNews(BraveSearch):
    """Brave's news index: articles, vendor blogs and advisories, with publish dates. Same key
    and the same monthly quota as web search."""
    name = "brave-news"
    endpoint = "https://api.search.brave.com/res/v1/news/search"

    def search(self, query: str, count: int = 20, freshness: str | None = None,
               site: str | None = None) -> list[dict]:
        params = {"q": f"{query} site:{site}" if site else query, "count": min(count, 20)}
        if freshness:
            params["freshness"] = freshness
        out = []
        for r in self._get(params).get("results") or []:
            self._add(out, r, "news")
        return out


def _monthly(value: str | None) -> int | None:
    """'1, 15000' (per second, per month) -> 15000."""
    try:
        return int(str(value).split(",")[-1].strip()) if value else None
    except ValueError:
        return None


def _usage_path():
    from engine.config import DATA_DIR
    return DATA_DIR / "websearch_usage.json"


def load_usage() -> dict:
    try:
        return json.loads(_usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_usage(backend_name: str, headers, now: float | None = None) -> None:
    limit, remaining = _monthly(headers.get("X-RateLimit-Limit")), _monthly(headers.get("X-RateLimit-Remaining"))
    if limit is None or remaining is None:
        return
    now = now or time.time()
    reset = _monthly(headers.get("X-RateLimit-Reset"))
    usage = load_usage()
    usage[backend_name] = {"limit": limit, "remaining": remaining, "checked": int(now),
                           "resets_at": int(now + reset) if reset else None}
    try:
        p = _usage_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(usage, indent=2), encoding="utf-8")
    except OSError:
        pass


def agent_budget(backend_name: str = "brave", share: float = AGENT_SHARE, now: float | None = None) -> tuple[bool, str]:
    """May an agent search now? Keeps (1 - share) of the monthly quota for watches."""
    u = load_usage().get(backend_name)
    if not u:
        return True, "quota not known yet"
    now = now or time.time()
    if u.get("resets_at") and now >= u["resets_at"]:
        return True, "quota period has reset"
    floor = int(u["limit"] * (1 - share))
    msg = f"{u['remaining']:,} of {u['limit']:,} searches left this month; {floor:,} kept for watches"
    return u["remaining"] > floor, msg


BACKENDS = {"brave": BraveSearch, "brave-news": BraveNews}


def backend(name: str):
    if name not in BACKENDS:
        raise WebSearchError(f"unknown web backend {name!r}; available: {sorted(BACKENDS)}")
    return BACKENDS[name]()
