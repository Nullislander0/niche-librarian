"""Recognise product versions in text, driven entirely by the pack's versions.yaml."""
from __future__ import annotations

import re


class VersionTagger:
    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.pattern = re.compile(cfg["pattern"]) if cfg.get("pattern") else None
        self.context = re.compile(cfg["context"], re.I) if cfg.get("context") else None
        self.window = int(cfg.get("window", 60))
        self.aliases = {str(k): str(v) for k, v in (cfg.get("aliases") or {}).items()}
        self.query_context_required = bool(cfg.get("query_context_required", False))

    def _canon(self, v: str) -> str:
        return self.aliases.get(v, v)

    def _find(self, text: str, need_context: bool) -> list[str]:
        if not self.pattern or not text:
            return []
        found: list[str] = []
        for m in self.pattern.finditer(text):
            if need_context and self.context:
                lo, hi = max(0, m.start() - self.window), m.end() + self.window
                if not self.context.search(text[lo:hi]):
                    continue
            v = self._canon(m.group(1) if m.groups() else m.group(0))
            if v not in found:
                found.append(v)
        return found

    def tag(self, text: str) -> list[str]:
        """Versions mentioned in a document, requiring a nearby product term."""
        return sorted(self._find(text, need_context=True), key=version_key)

    def detect_query(self, text: str) -> list[str]:
        """Versions mentioned in a search query (context optional: queries are short)."""
        return self._find(text, need_context=self.query_context_required)


def version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))


def version_matches(tag: str, wanted: str) -> bool:
    """'11' matches 11.x; '10.9' matches 10.9 and 10.9.1."""
    return tag == wanted or tag.startswith(wanted + ".")


def family(v: str) -> str:
    return v.split(".")[0]
