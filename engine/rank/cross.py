"""Optional cross-encoder rerank stage (FastEmbed / ONNX Runtime; no PyTorch).

A cross-encoder reads the query and each candidate passage together, which ranks
the top of the list more sharply than fused keyword+vector scores. It re-scores
only the best `candidates` after fusion; its score is blended with the fused score
and the pack's quality signals are applied afterwards as usual.

ranking.yaml:
  reranker:
    model: Xenova/ms-marco-MiniLM-L-6-v2   # null/absent = off
    candidates: 20       # how many fused results to re-score
    max_chars: 1000      # passage text sent per candidate
    alpha: 0.7           # weight of the cross-encoder vs. the fused score
"""
from __future__ import annotations

import logging

from engine.config import DATA_DIR

log = logging.getLogger(__name__)
_MODELS: dict[str, object] = {}


class CrossReranker:
    def __init__(self, model: str, candidates: int = 20, max_chars: int = 1000, alpha: float = 0.7):
        self.model, self.candidates, self.max_chars, self.alpha = model, candidates, max_chars, alpha

    @classmethod
    def from_config(cls, cfg: dict | None) -> "CrossReranker | None":
        if not cfg or not cfg.get("model"):
            return None
        return cls(cfg["model"], int(cfg.get("candidates", 20)), int(cfg.get("max_chars", 1000)),
                   float(cfg.get("alpha", 0.7)))

    def _encoder(self):
        if self.model not in _MODELS:
            from fastembed.rerank.cross_encoder import TextCrossEncoder  # optional dependency
            _MODELS[self.model] = TextCrossEncoder(model_name=self.model, cache_dir=str(DATA_DIR / "models"))
        return _MODELS[self.model]

    def rescore(self, query: str, candidates: list[dict], fused: dict[int, float]) -> dict[int, float]:
        """Return a new fused-score map with the top candidates blended with cross-encoder scores."""
        top = sorted(candidates, key=lambda c: fused.get(c["rowid"], 0), reverse=True)[: self.candidates]
        if len(top) < 2:
            return fused
        texts = [f"{c['title']}\n{c['text'][: self.max_chars]}" for c in top]
        scores = list(self._encoder().rerank(query, texts))
        lo, hi = min(scores), max(scores)
        span = (hi - lo) or 1.0
        out = {rid: (1 - self.alpha) * s for rid, s in fused.items()}
        for c, s in zip(top, scores):
            out[c["rowid"]] = self.alpha * (s - lo) / span + (1 - self.alpha) * fused.get(c["rowid"], 0)
        return out
