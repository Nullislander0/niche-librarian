"""Small stdlib HTML -> markdown converter for forum and blog bodies.

Handles what forum posts actually contain: paragraphs, line breaks, links, emphasis,
inline code, pre blocks, lists, blockquotes, headings, images, simple tables, and
Vanilla-style link embeds (`data-embedjson`). Anything else is reduced to its text.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from html.parser import HTMLParser

_BLOCK = {"p", "div", "section", "article", "header", "footer", "figure", "figcaption", "table", "tr"}
_SKIP = {"script", "style", "noscript", "iframe", "svg", "button"}


def unwrap_redirect(href: str, param: str = "target") -> str:
    """https://site/home/leaving?target=https%3A%2F%2Fx -> https://x"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    except ValueError:
        return href
    return q[param][0] if param in q and q[param] else href


class _Converter(HTMLParser):
    def __init__(self, redirect_param: str | None, base_url: str | None = None):
        super().__init__(convert_charrefs=True)
        self.redirect_param = redirect_param
        self.base_url = base_url
        self.bufs: list[list[str]] = [[]]  # a new buffer per open blockquote
        self.lists: list[list] = []  # stack of [kind, counter]
        self.pre = 0
        self.skip = 0  # depth inside skipped/embedded elements
        self.links: list[str | None] = []
        self.cell = 0

    # -- helpers ----------------------------------------------------------

    def _href(self, href: str | None) -> str | None:
        if not href or href.startswith(("javascript:", "#")):
            return None
        try:
            if self.base_url:
                href = urllib.parse.urljoin(self.base_url, href)
            return unwrap_redirect(href, self.redirect_param) if self.redirect_param else href
        except ValueError:  # malformed URL in user content (e.g. "http://[bad")
            return None

    def _newline(self, n: int = 1):
        """Ensure the current buffer ends with at least n newlines (none at the very start)."""
        buf = self.bufs[-1]
        text = "".join(buf)
        if not text.strip():
            return
        have = len(text) - len(text.rstrip("\n"))
        if have < n:
            buf.append("\n" * (n - have))

    def _emit(self, s: str):
        self.bufs[-1].append(s)

    def _at_list_marker(self) -> bool:
        tail = "".join(self.bufs[-1][-3:])
        return bool(self.lists) and bool(re.search(r"(^|\n)\s*(- |\d+\. )$", tail))

    # -- parser hooks -----------------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag not in ("br", "img", "hr", "input", "meta", "link"):
                self.skip += 1
            return
        if tag in _SKIP:
            self.skip = 1
            return
        if tag == "span" and "data-embedjson" in a:
            try:
                e = json.loads(a["data-embedjson"])
                url = self._href(e.get("url"))
                name = (e.get("name") or url or "").strip()
                if url:
                    self._emit(f"[{name}]({url})")
                    if e.get("body"):
                        self._emit(f" ({e['body'].strip()[:300]})")
            except ValueError:
                pass
            self.skip = 1
            return
        if tag in ("p", "div", "section", "article", "figure", "table"):
            if self.lists and tag in ("p", "div"):
                if not self._at_list_marker():  # <li><p>a</p><p>b</p></li>: b continues the item
                    self._newline(1)
                    self._emit("   " * len(self.lists))
            else:
                self._newline(2)
        elif tag == "tr":
            self._newline(1)
            self._emit("|")
        elif tag in ("td", "th"):
            self._emit(" ")
        elif tag == "br":
            self._emit("\n" if self.pre else "  \n")
        elif tag == "hr":
            self._newline(2)
            self._emit("---")
            self._newline(2)
        elif re.fullmatch(r"h[1-6]", tag):
            self._newline(2)
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag == "pre":
            self._newline(2)
            self._emit("```\n")
            self.pre += 1
        elif tag == "blockquote":
            self._newline(2)
            self.bufs.append([])
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._newline(1)
        elif tag == "li":
            self._newline(1)
            indent = "  " * max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self._emit(f"{indent}{self.lists[-1][1]}. ")
            else:
                self._emit(f"{indent}- ")
        elif tag == "a":
            href = self._href(a.get("href"))
            self.links.append(href)
            if href:
                self._emit("[")
        elif tag == "img":
            src = a.get("src")
            if src and self.base_url:
                try:
                    src = urllib.parse.urljoin(self.base_url, src)
                except ValueError:
                    src = None
            if src:
                self._emit(f"![{a.get('alt') or 'image'}]({src})")

    def handle_endtag(self, tag):
        if self.skip:
            self.skip -= 1
            return
        if tag in ("p", "div", "section", "article", "figure", "table") or re.fullmatch(r"h[1-6]", tag):
            self._newline(1 if self.lists and tag in ("p", "div") else 2)
        elif tag in ("td", "th"):
            self._emit(" |")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._emit("\n```")
            self._newline(2)
        elif tag == "blockquote" and len(self.bufs) > 1:
            inner = _tidy("".join(self.bufs.pop()))
            if inner:
                self._emit("\n".join(f"> {line}".rstrip() for line in inner.split("\n")))
            self._newline(2)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline(2)
        elif tag == "a":
            href = self.links.pop() if self.links else None
            if href:
                self._emit(f"]({href})")

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self._emit(data)
        else:
            self._emit(re.sub(r"\s+", " ", data))


def _tidy(md: str) -> str:
    md = re.sub(r"\n[ \t]+(?=\n)", "\n", md)  # whitespace-only lines
    md = re.sub(r"\n (?=\S)", "\n", md)  # space left after <br>
    md = re.sub(r"[ \t]+\n", lambda m: "  \n" if m.group(0).startswith("  ") else "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"\[\]\([^)]*\)", "", md)  # empty links
    md = re.sub(r"\*\*\s*\*\*", "", md)  # empty bold
    return md.strip()


def html_to_markdown(html: str, redirect_param: str | None = None, base_url: str | None = None) -> str:
    """base_url resolves relative links and images (e.g. ../install/page.html)."""
    if not html:
        return ""
    c = _Converter(redirect_param, base_url)
    c.feed(html)
    c.close()
    while len(c.bufs) > 1:  # unclosed blockquote
        c.handle_endtag("blockquote")
    return _tidy("".join(c.bufs[0]))
