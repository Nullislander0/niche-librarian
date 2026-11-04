"""Polite HTTP fetcher: robots.txt, per-host throttle, identifying user agent, disk cache.

Every network request a parser makes goes through here. If a site refuses us
(robots.txt, 401/403, CAPTCHA page) we raise BlockedError and the crawl for that
source stops. We never retry around a block.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

_CHALLENGE = re.compile(r"captcha|cf-chl-|challenge-platform|are you a robot|unusual traffic", re.I)


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class BlockedError(FetchError):
    """The site refused the crawler. Stop and report; do not work around it."""


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        min_interval: float = 1.0,
        cache_ttl: int = 86400,
        timeout: int = 30,
        max_retries: int = 3,
        respect_robots: bool = True,
    ):
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir)
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.network_requests = 0
        self._next_allowed: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # -- public -----------------------------------------------------------

    def get_text(self, url: str, params: dict | None = None, ttl: int | None = None) -> str:
        full = build_url(url, params)
        ttl = self.cache_ttl if ttl is None else ttl
        if ttl > 0:
            cached = self._cache_read(full, ttl)
            if cached is not None:
                return cached
        self._check_robots(full)
        body = self._request(full)
        self._cache_write(full, body)
        return body

    def get_json(self, url: str, params: dict | None = None, ttl: int | None = None):
        return json.loads(self.get_text(url, params, ttl))

    def pause_host(self, url: str, seconds: float) -> None:
        """Honour a server-requested backoff (e.g. Stack Exchange's `backoff` field)."""
        host = urllib.parse.urlsplit(url).netloc
        self._next_allowed[host] = max(self._next_allowed.get(host, 0), time.monotonic() + seconds)
        log.info("backing off %s for %.0fs at the server's request", host, seconds)

    # -- internals --------------------------------------------------------

    def _throttle(self, host: str) -> None:
        wait = self._next_allowed.get(host, 0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._next_allowed[host] = time.monotonic() + self._host_interval.get(host, self.min_interval)

    def _request(self, url: str) -> str:
        host = urllib.parse.urlsplit(url).netloc
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        )
        delay = 5.0
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            self.network_requests += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = _decode(resp.read(), resp.headers)
                    ctype = resp.headers.get("Content-Type", "")
                if "html" in ctype and _CHALLENGE.search(body[:20000]):
                    raise BlockedError(f"challenge/CAPTCHA page served for {url}", 200)
                return body
            except urllib.error.HTTPError as e:
                body = _decode(e.read(), e.headers) if e.fp else ""
                if e.code in (401, 403, 407):
                    raise BlockedError(f"HTTP {e.code} for {url} (blocked or login required)", e.code, body)
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = e.headers.get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else delay
                    log.warning("HTTP %s from %s; retrying in %.0fs", e.code, host, wait)
                    time.sleep(wait)
                    delay *= 2
                    continue
                raise FetchError(f"HTTP {e.code} for {url}", e.code, body)
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    log.warning("network error for %s (%s); retrying in %.0fs", host, e.reason, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FetchError(f"network error for {url}: {e.reason}")
        raise FetchError(f"gave up on {url}")

    def _check_robots(self, url: str) -> None:
        if not self.respect_robots:
            return
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc
        rp = self._robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                rp.parse(self._request(robots_url).splitlines())
            except BlockedError:
                rp.disallow_all = True
            except FetchError as e:
                if e.status and 400 <= e.status < 500:
                    rp.allow_all = True  # no robots.txt: everything allowed
                else:
                    raise BlockedError(f"could not read {robots_url}: {e}")
            delay = rp.crawl_delay(self.user_agent)
            if delay:
                self._host_interval[host] = max(self.min_interval, float(delay))
            self._robots[host] = rp
        if not rp.can_fetch(self.user_agent, url):
            raise BlockedError(f"robots.txt disallows {url}")

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_read(self, url: str, ttl: int) -> str | None:
        path = self._cache_path(url)
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if time.time() - entry["fetched_at"] > ttl:
            return None
        return entry["body"]

    def _cache_write(self, url: str, body: str) -> None:
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "fetched_at": time.time(), "body": body}))


def build_url(url: str, params: dict | None) -> str:
    if not params:
        return url
    query = urllib.parse.urlencode({k: v for k, v in sorted(params.items()) if v is not None})
    return f"{url}{'&' if '?' in url else '?'}{query}"


def _decode(raw: bytes, headers) -> str:
    enc = (headers.get("Content-Encoding") or "").lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
    return raw.decode(charset or "utf-8", errors="replace")
