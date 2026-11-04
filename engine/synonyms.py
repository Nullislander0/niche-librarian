"""Query expansion from the pack's synonyms.yaml (keyword search only; embeddings handle the rest)."""
from __future__ import annotations

import re


class Synonyms:
    def __init__(self, groups: list[list[str]] | None):
        self.groups = [[t.lower() for t in g] for g in (groups or []) if len(g) > 1]
        self._patterns = [[re.compile(rf"(?<!\w){re.escape(t)}(?!\w)") for t in g] for g in self.groups]

    def expand(self, query: str) -> list[str]:
        """Other spellings/names for any term in the query, e.g. 'ags' -> ['arcgis server']."""
        q = query.lower()
        extra: list[str] = []
        for group, pats in zip(self.groups, self._patterns):
            hit = [t for t, p in zip(group, pats) if p.search(q)]
            if hit:
                extra += [t for t in group if t not in hit and t not in extra]
        return extra
