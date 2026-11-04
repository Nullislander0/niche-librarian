"""Query expansion from the pack's synonyms.yaml (keyword search only; embeddings handle the rest).

Two entry forms:
  [a, b, c]       two-way group: any term adds the others
  "a, b -> c"     one-way: a or b adds c, but c adds nothing
One-way is the safer default: expanding a common word ("portal") into long phrases
drags in generic results, while expanding an abbreviation ("ags") rarely hurts.
"""
from __future__ import annotations

import re


def _pat(t: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(t)}(?!\w)")


class Synonyms:
    def __init__(self, entries: list | None):
        self.rules: list[tuple[list[str], list[str]]] = []  # (triggers, additions)
        for e in entries or []:
            if isinstance(e, str) and "->" in e:
                left, right = e.split("->", 1)
                self.rules.append(([t.strip().lower() for t in left.split(",") if t.strip()],
                                   [t.strip().lower() for t in right.split(",") if t.strip()]))
            elif isinstance(e, list) and len(e) > 1:
                g = [t.lower() for t in e]
                self.rules.append((g, g))
        self._pats = [[_pat(t) for t in trig] for trig, _ in self.rules]

    def expand(self, query: str) -> list[str]:
        """Other spellings/names for terms in the query, e.g. 'ags' -> ['arcgis server']."""
        q = query.lower()
        extra: list[str] = []
        for (triggers, additions), pats in zip(self.rules, self._pats):
            hit = [t for t, p in zip(triggers, pats) if p.search(q)]
            if hit:
                extra += [t for t in additions if t not in hit and t not in extra and not _pat(t).search(q)]
        return extra
