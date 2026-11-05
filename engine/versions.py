"""Recognise product versions in text, driven entirely by the pack's versions.yaml.

Single product: `pattern` (group 1 = version) plus optional `context`, `aliases`, ...
Several products: a `products` list, each entry a single-product config plus a `prefix`;
tags then read "<prefix> <version>" (e.g. "pve 8.2", "truenas 24.10"), so versions of
different products never collide and "pve 8" matches every 8.x.
"""
from __future__ import annotations

import re


class VersionTagger:
    def __init__(self, cfg: dict):
        cfg = cfg or {}
        # (prefix, tagger, only_sources): a product rule with `only_sources` applies to those
        # sources alone, e.g. a bare "25.10" means TrueNAS inside the TrueNAS forums only.
        self.products = [(str(p["prefix"]),
                          VersionTagger({k: v for k, v in p.items() if k not in ("prefix", "only_sources")}),
                          set(p.get("only_sources") or []))
                         for p in cfg.get("products", [])]
        self.pattern = re.compile(cfg["pattern"]) if cfg.get("pattern") else None
        self.context = re.compile(cfg["context"], re.I) if cfg.get("context") else None
        self.window = int(cfg.get("window", 60))
        self.aliases = {str(k): str(v) for k, v in (cfg.get("aliases") or {}).items()}
        self.query_context_required = bool(cfg.get("query_context_required", False))
        self.strip_patch_zero = bool(cfg.get("strip_patch_zero", False))
        # Numbers right after these terms belong to something else (e.g. "PostgreSQL 12.2").
        self.exclude_before = re.compile(cfg["exclude_before"] + r"\W{0,3}$", re.I) \
            if cfg.get("exclude_before") else None
        self.exclude_window = int(cfg.get("exclude_window", 25))

    def _canon(self, v: str) -> str:
        v = self.aliases.get(v, self.aliases.get(v.lower(), v))
        if self.strip_patch_zero and v.count(".") == 2 and v.endswith(".0"):
            v = v[:-2]  # 11.5.0 -> 11.5
        return v

    def _find(self, text: str, need_context: bool) -> list[str]:
        if not self.pattern or not text:
            return []
        found: list[str] = []
        for m in self.pattern.finditer(text):
            if self.exclude_before and self.exclude_before.search(text[max(0, m.start() - self.exclude_window):m.start()]):
                continue
            if need_context and self.context:
                lo, hi = max(0, m.start() - self.window), m.end() + self.window
                if not self.context.search(text[lo:hi]):
                    continue
            v = self._canon(m.group(1) if m.groups() else m.group(0))
            if v not in found:
                found.append(v)
        return found

    def tag(self, text: str, source: str | None = None) -> list[str]:
        """Versions mentioned in a document, requiring a nearby product term."""
        found = self._find(text, need_context=True) + \
            [f"{pre} {v}" for pre, t, only in self.products if not only or source in only for v in t.tag(text)]
        return sorted(set(found), key=version_key)

    def detect_query(self, text: str) -> list[str]:
        """Versions mentioned in a search query (context optional: queries are short)."""
        return self._find(text, need_context=self.query_context_required) + \
            [f"{pre} {v}" for pre, t, only in self.products if not only for v in t.detect_query(text)]


def version_key(v: str) -> tuple:
    """Sort key: product prefix first (if any), then numeric parts. "pve 8.10" > "pve 8.2"."""
    prefix, _, num = v.rpartition(" ")
    return (prefix, *(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", num)))


def version_matches(tag: str, wanted: str) -> bool:
    """'11' matches 11.x; '10.9' matches 10.9 and 10.9.1."""
    return tag == wanted or tag.startswith(wanted + ".")


def family(v: str) -> str:
    return v.split(".")[0]
