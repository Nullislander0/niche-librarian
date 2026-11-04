"""Embeddings from a local Ollama server."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    pass


class OllamaEmbedder:
    def __init__(self, url: str, model: str, dim: int, doc_prefix: str = "", query_prefix: str = "",
                 batch_size: int = 32, max_chars: int = 6000, timeout: int = 120):
        self.url = url.rstrip("/")
        self.model = model
        self.dim = dim
        self.doc_prefix = doc_prefix
        self.query_prefix = query_prefix
        self.batch_size = batch_size
        self.max_chars = max_chars
        self.timeout = timeout
        self._ok: bool | None = None

    @classmethod
    def from_settings(cls, s: dict) -> "OllamaEmbedder":
        return cls(s["url"], s["model"], s["dim"], s.get("doc_prefix", ""), s.get("query_prefix", ""),
                   s.get("batch_size", 32))

    def available(self) -> bool:
        if self._ok is None:
            try:
                self._post([self.query_prefix + "ping"])
                self._ok = True
            except EmbeddingUnavailable as e:
                log.warning("embeddings unavailable, falling back to keyword search: %s", e)
                self._ok = False
        return self._ok

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post([self.doc_prefix + t[: self.max_chars] for t in texts[i:i + self.batch_size]]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._post([self.query_prefix + text[: self.max_chars]])[0]

    def _post(self, inputs: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": inputs}).encode()
        req = urllib.request.Request(f"{self.url}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                vecs = json.loads(resp.read())["embeddings"]
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            raise EmbeddingUnavailable(f"{self.url} model {self.model}: {e}") from e
        if vecs and len(vecs[0]) != self.dim:
            raise EmbeddingUnavailable(f"model returned dim {len(vecs[0])}, expected {self.dim}")
        return vecs
