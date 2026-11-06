"""Polite HTTP fetcher: per-host throttle (incl. robots.txt Crawl-delay), identifying user agent, disk cache.

Every network request a parser makes goes through here. If a site refuses us
(robots.txt, 401/403, CAPTCHA page) we raise BlockedError and the crawl for that
source stops. We never retry around a block.
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import ipaddress
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

# Signs of a bot-challenge / block page (not a normal page that merely loads a CAPTCHA script for
# a form): the challenge's own title, or Cloudflare's challenge parameters.
_CHALLENGE = re.compile(
    r"<title>\s*(just a moment|attention required|access denied|security check|are you a robot|ddos-guard)"
    r"|cf-chl-|cf_chl_opt|window\._cf_chl", re.I)


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class BlockedError(FetchError):
    """The site refused the crawler. Stop and report; do not work around it."""


class CacheMiss(FetchError):
    """Offline mode: the response isn't in the local cache."""


def check_public(url: str) -> None:
    """Refuse URLs whose host is, or resolves to, a private, loopback, link-local or otherwise
    non-public address (your router, localhost services, cloud metadata endpoints). Used for
    URLs that come from agents or web pages rather than from a pack's own config."""
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise BlockedError(f"no host in {url}")
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)}
        except (socket.gaierror, UnicodeError) as e:
            raise FetchError(f"cannot resolve {host}: {e}") from e
    for ip in addrs:
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            raise BlockedError(f"{host} is a private or local address ({ip}); not fetched")


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public(newurl)          # a public page must not redirect us onto the local network
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        min_interval: float = 1.0,
        cache_ttl: int = 86400,
        timeout: int = 30,
        max_retries: int = 3,
        respect_robots: bool = False,
        robots_eu: bool = True,
        public_only: bool = False,
        offline: bool = False,
    ):
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir)
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.robots_eu = robots_eu
        self.public_only = public_only      # refuse private/local addresses, including via redirects
        self._public_opener = urllib.request.build_opener(_PublicRedirects())
        self.offline = offline  # replay from cache only; never touch the network
        self.network_requests = 0
        self._next_allowed: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # -- public -----------------------------------------------------------

    def get_text(self, url: str, params: dict | None = None, ttl: int | None = None,
                 public_only: bool | None = None) -> str:
        """public_only=True (or the fetcher's default) refuses private and local addresses."""
        full = build_url(url, params)
        public = self.public_only if public_only is None else public_only
        if public and not self.offline:
            check_public(full)
        ttl = self.cache_ttl if ttl is None else ttl
        if self.offline:
            cached = self._cache_read(full, float("inf"))
            if cached is None:
                raise CacheMiss(f"not in cache: {full}")
            return cached
        if ttl > 0:
            cached = self._cache_read(full, ttl)
            if cached is not None:
                return cached
        self._check_robots(full)
        body = self._request(full, public)
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

    def _request(self, url: str, public_only: bool = False) -> str:
        host = urllib.parse.urlsplit(url).netloc
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        )
        delay = 5.0
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            self.network_requests += 1
            try:
                opener = self._public_opener.open if public_only else urllib.request.urlopen
                with opener(req, timeout=self.timeout) as resp:
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
            except (OSError, http.client.HTTPException) as e:
                # URLError, timeouts mid-read, resets, TLS errors, truncated bodies (e.g. the
                # network dropping when the machine suspends): retry with backoff.
                reason = getattr(e, "reason", None) or e
                if attempt < self.max_retries:
                    log.warning("network error for %s (%s); retrying in %.0fs", host, reason, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FetchError(f"network error for {url}: {reason}")
        raise FetchError(f"gave up on {url}")

    def _check_robots(self, url: str) -> None:
        """Read the host's robots.txt once. Its Crawl-delay is always honoured (pacing is about
        load). Its Disallow rules are enforced for EU/EEA hosts, or everywhere with
        respect_robots (see CRAWLER in engine/config.py)."""
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
                if (e.status and 400 <= e.status < 500) or not self._enforce(host):
                    rp.allow_all = True  # no robots.txt: everything allowed
                else:
                    raise BlockedError(f"could not read {robots_url}: {e}")
            delay = rp.crawl_delay(self.user_agent)
            if delay:
                self._host_interval[host] = max(self.min_interval, float(delay))
            self._robots[host] = rp
        if self._enforce(host) and not rp.can_fetch(self.user_agent, url):
            raise BlockedError(f"robots.txt disallows {url} (EU/EEA host)" if not self.respect_robots
                               else f"robots.txt disallows {url}")

    def _enforce(self, host: str) -> bool:
        return self.respect_robots or (self.robots_eu and is_eu_host(host))

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _cache_read(self, url: str, ttl: int) -> str | None:
        path = self._cache_path(url)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - entry["fetched_at"] > ttl:
            return None
        return entry["body"]

    def _cache_write(self, url: str, body: str) -> None:
        path = self._cache_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "fetched_at": time.time(), "body": body}), encoding="utf-8")


EU_TLDS = {"at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de", "gr", "hu", "ie", "it", "lv",
           "lt", "lu", "mt", "nl", "pl", "pt", "ro", "sk", "si", "es", "se", "eu", "is", "li", "no"}


def is_eu_host(host: str) -> bool:
    """EU/EEA country-code domains. A heuristic: EU sites on .com aren't detected."""
    return host.split(":")[0].rstrip(".").rsplit(".", 1)[-1].lower() in EU_TLDS


def to_ascii_url(url: str) -> str:
    """Percent-encode non-ASCII characters (e.g. accented slugs); existing %XX escapes are kept."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def build_url(url: str, params: dict | None) -> str:
    url = to_ascii_url(url)
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
