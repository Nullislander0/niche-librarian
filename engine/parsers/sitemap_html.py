"""Documentation sites listed in an XML sitemap: one Document per page.

For vendor docs, release notes and other static pages that have no API. Pages are
found only through the site's own sitemap (no link-following), filtered by URL
patterns, and re-fetched only after `refresh_days`.

Options (sources.yaml):
  sitemaps: [https://docs.example.com/en/product/sitemap.xml]   # sitemap or sitemap index
  urls: [https://docs.example.com/faq.html]                     # extra pages, listed explicitly
  index_pages: [https://docs.example.com/]                      # pages whose links are followed
  index_link_pattern: 'chapter-[\w-]+\.html$'                    # ...one level deep, if they match
  follow_depth: 1                                                  # hops of links to follow from index pages
  max_pages: 2000                                                  # cap on pages found that way
  include: ['^https://docs\\.example\\.com/en/product/']         # regexes; all if empty
  exclude: ['/release-notes/archive/']
  content: {tag: article, id: main}      # element holding the page body (or class:)
  title_strip: ' \\| .*documentation$'    # regex removed from the page title
  version_meta: version                  # <meta name="version"> -> explicit version tag
  version_from_url: '/(\d+\.\d+)/'        # or take it from the page URL (group 1)
  version_prefix: pro                    # ...tagged "pro 3.7" (for packs with several products)
  version_prefix_by:                     # or choose the prefix from another meta tag, first match wins
    - {meta: product, match: 'ArcGIS Pro', prefix: pro}
  date_meta: last-modified               # <meta name=...> -> post/updated date
  refresh_days: 30                       # re-fetch pages older than this
  meta_filter: {product: 'Enterprise|Portal'}  # keep a page only if every listed meta tag
                                         # matches its regex (pages are still fetched once)
  extra_meta: [product, article-type]    # meta tags copied into extra (shown in fetch headers)
  extract: {version_fixed: 'Version Fixed\s*\|\s*([^|\n]+?)\s*\|'}  # regexes on the page markdown
  versions_from: [version_fixed]         # extracted/meta fields that are product versions
  title_format: '{title}: {h1}'          # fields: title (cleaned <title>/meta title), h1, meta_<name>
  sort: {pattern: '-(\d+)$', descending: true}   # crawl order, e.g. newest ids first
  strip_patterns: ['<div class="toc".*?</div>']   # extra regexes removed from the content HTML
  kind: page

<nav> elements (breadcrumbs, in-page tables of contents) are always removed from content.

Mark the source authoritative by listing its source_type under
`authoritative_source_types` in ranking.yaml.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import time
from datetime import datetime, timezone
from typing import Iterator

from engine.crawl.fetcher import FetchError
from engine.parsers.base import CrawlContext, Document
from engine.parsers.html_md import html_to_markdown

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def meta_tags(page: str) -> dict[str, str]:
    out = {}
    for tag in _META.findall(page):
        a = dict((k.lower(), v) for k, v in _ATTR.findall(tag))
        name = a.get("name") or a.get("property")
        if name and "content" in a:
            out.setdefault(name.lower(), html.unescape(a["content"]))
    return out


def extract_element(page: str, tag: str, id: str | None = None, cls: str | None = None) -> str | None:
    """Inner HTML of the first <tag> matching id/class, honouring nested tags of the same name."""
    if id:
        attr = rf"""\bid\s*=\s*["']?{re.escape(id)}(?=["'\s>])"""
    elif cls:
        attr = rf"""\bclass\s*=\s*["']?[^"'>]*\b{re.escape(cls)}\b"""
    else:
        attr = ""
    start = re.search(rf"<{tag}\b[^>]*{attr}[^>]*>", page, re.I)
    if not start:
        return None
    depth, pos = 1, start.end()
    opener, closer = re.compile(rf"<{tag}\b", re.I), re.compile(rf"</{tag}\s*>", re.I)
    while depth:
        c = closer.search(page, pos)
        if not c:
            return page[start.end():]
        o = opener.search(page, pos, c.start())
        if o:
            depth, pos = depth + 1, o.end()
        else:
            depth, pos = depth - 1, c.end()
    return page[start.end():c.start()]


def _date(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%m/%d/%Y", "%d %B %Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


class Parser:
    POSITION_KEY_PREFIXES = ("page:",)  # per-page fetch times; a rebuild resets these

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx
        o = ctx.options
        self.sitemaps = o.get("sitemaps", [])
        self.extra_urls = o.get("urls", [])
        self.index_pages = o.get("index_pages", [])
        self.index_link = re.compile(o["index_link_pattern"]) if o.get("index_link_pattern") else None
        self.follow_depth = int(o.get("follow_depth", 1))
        self.max_pages = int(o.get("max_pages", 2000))
        self.include = [re.compile(p) for p in o.get("include", [])]
        self.exclude = [re.compile(p) for p in o.get("exclude", [])]
        self.content = o.get("content", {"tag": "main"})
        self.title_strip = re.compile(o["title_strip"]) if o.get("title_strip") else None
        self.version_meta = o.get("version_meta")
        self.version_url = re.compile(o["version_from_url"]) if o.get("version_from_url") else None
        self.version_prefix = o.get("version_prefix")
        self.version_prefix_by = [(r["meta"].lower(), re.compile(r["match"], re.I), r["prefix"])
                                  for r in o.get("version_prefix_by", [])]
        self.date_meta = o.get("date_meta")
        self.refresh = int(o.get("refresh_days", 30)) * 86400
        self.kind = o.get("kind", "page")
        self.meta_filter = {k.lower(): re.compile(v, re.I) for k, v in (o.get("meta_filter") or {}).items()}
        self.extra_meta = [m.lower() for m in o.get("extra_meta", [])]
        self.extract = {k: re.compile(v, re.I) for k, v in (o.get("extract") or {}).items()}
        self.versions_from = [v.lower() for v in o.get("versions_from", [])]
        self.title_format = o.get("title_format")
        self.sort = o.get("sort")
        self.strip = [re.compile(p, re.S | re.I) for p in [r"<nav\b.*?</nav>", *o.get("strip_patterns", [])]]
        self.skipped = 0

    def crawl(self) -> Iterator[Document]:
        now = time.time()
        for url in self._urls():
            seen = self.ctx.state.get(f"page:{url}")
            if seen and now - seen < self.refresh:
                continue
            self.ctx.check_budget()
            try:
                page = self.ctx.fetcher.get_text(url, ttl=self.refresh)
            except FetchError as e:
                if e.status not in (404, 410):  # listed in the sitemap but since removed
                    raise
                self.ctx.log.warning("gone (HTTP %s), skipping: %s", e.status, url)
                self.ctx.state.set(f"page:{url}", now)
                continue
            doc = self._page(url, page)
            if doc:
                yield doc
            self.ctx.state.set(f"page:{url}", now)

    def _urls(self) -> list[str]:
        urls, queue, seen = [], list(self.sitemaps), set()
        while queue:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            self.ctx.check_budget()
            body = self.ctx.fetcher.get_text(sm, ttl=86400)
            for loc in _LOC.findall(body):
                loc = html.unescape(loc)
                if "<sitemapindex" in body[:2000] or loc.endswith(".xml"):
                    queue.append(loc)
                elif (not self.include or any(p.search(loc) for p in self.include)) \
                        and not any(p.search(loc) for p in self.exclude):
                    urls.append(loc)
        urls += self.extra_urls
        # Index pages, then (follow_depth > 1) the pages they link to, breadth first, keeping to
        # links that match index_link_pattern. Pages read here are cached for the content pass.
        frontier, found = list(self.index_pages), set()
        for depth in range(self.follow_depth):
            nxt = []
            for idx in frontier:
                if len(found) >= self.max_pages:
                    break
                self.ctx.check_budget()
                try:
                    body = self.ctx.fetcher.get_text(idx, ttl=self.refresh if depth else 86400)
                except FetchError as e:
                    if e.status not in (404, 410):
                        raise
                    continue
                for href in re.findall(r'href="([^"#]+)"', body):
                    full = urllib.parse.urljoin(idx, html.unescape(href))
                    if full in found or len(found) >= self.max_pages:
                        continue
                    if (not self.index_link or self.index_link.search(full)) and \
                            not any(p.search(full) for p in self.exclude):
                        found.add(full)
                        urls.append(full)
                        nxt.append(full)
            frontier = nxt
        self.ctx.log.info("%d pages listed", len(urls))
        urls = sorted(set(urls))
        if self.sort:
            pat = re.compile(self.sort["pattern"])
            def key(u):
                m = pat.search(u)
                return int(m.group(1)) if m and m.group(1).isdigit() else (m.group(1) if m else "")
            urls.sort(key=key, reverse=bool(self.sort.get("descending")))
        return urls

    def _page(self, url: str, page: str) -> Document | None:
        meta = meta_tags(page)
        if any(not p.search(meta.get(k, "")) for k, p in self.meta_filter.items()):
            self.skipped += 1
            return None
        body = extract_element(page, self.content.get("tag", "main"), self.content.get("id"),
                               self.content.get("class"))
        if body is None:
            self.ctx.log.warning("no content element in %s", url)
            return None
        for pat in self.strip:
            body = pat.sub("", body)
        md = html_to_markdown(body, base_url=url)
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        title = meta.get("title") or (html.unescape(m.group(1)).strip() if m else url)
        if self.title_strip:
            title = self.title_strip.sub("", title).strip()
        if self.title_format:
            h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
            fields = {"title": title, "h1": html.unescape(re.sub(r"<[^>]+>|\s+", " ", h1.group(1))).strip() if h1 else "",
                      **{f"meta_{k.replace('-', '_')}": v for k, v in meta.items()}}
            try:
                title = self.title_format.format(**fields).strip(" :")[:250]
            except KeyError:
                pass
        date = _date(meta.get(self.date_meta)) if self.date_meta else None
        details = {m: meta.get(m) for m in self.extra_meta if meta.get(m)}
        for k, p in self.extract.items():
            m = p.search(md)
            if m:
                details[k] = m.group(1).strip()
        versions = [meta[self.version_meta]] if self.version_meta and meta.get(self.version_meta) else []
        if self.version_url and (m := self.version_url.search(url)):
            versions.append(m.group(1))
        versions += [details[k] for k in self.versions_from
                     if re.fullmatch(r"\d+(\.\d+)+", details.get(k, "") or "")]
        versions = [v.strip().lstrip("vV") for v in versions]
        versions = [v for v in versions if re.fullmatch(r"\d+(\.\d+)+", v)]   # drop "June 2026" etc.
        prefix = next((pre for m, pat, pre in self.version_prefix_by if pat.search(meta.get(m, ""))),
                      self.version_prefix)
        if prefix:
            versions = [f"{prefix} {v}" for v in versions]
        return Document(
            doc_id=f"{self.ctx.source.id}:{re.sub(r'^https?://[^/]+/', '', url)}",
            source=self.ctx.source.id, source_type=self.ctx.source.source_type, kind=self.kind,
            url=url, title=title, content_md=md, post_date=date, updated_date=date,
            extra={"versions": sorted(set(versions)), "description": meta.get("description"),
                   "product": meta.get("product"), "details": details},
        )
