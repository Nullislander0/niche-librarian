"""Load a topic pack and engine settings.

A pack is a folder under packs/ with:
  sources.yaml   pack metadata + the source list
  ranking.yaml   rerank weights and signal patterns
  versions.yaml  how to recognise product versions in text
  parsers/       optional site-specific parser overrides
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = Path(os.environ.get("LIBRARIAN_PACKS", ROOT / "packs"))
DATA_DIR = Path(os.environ.get("LIBRARIAN_DATA", ROOT / "data"))

# Engine-level settings, overridable by environment variables.
EMBEDDING = {
    "url": os.environ.get("LIBRARIAN_OLLAMA_URL", "http://localhost:11434"),
    "model": os.environ.get("LIBRARIAN_EMBED_MODEL", "nomic-embed-text"),
    "dim": int(os.environ.get("LIBRARIAN_EMBED_DIM", "768")),
    # nomic-embed-text expects task prefixes; set both to "" for models that don't.
    "doc_prefix": os.environ.get("LIBRARIAN_EMBED_DOC_PREFIX", "search_document: "),
    "query_prefix": os.environ.get("LIBRARIAN_EMBED_QUERY_PREFIX", "search_query: "),
    "batch_size": 32,
}

CRAWLER = {
    "user_agent": "niche-librarian/0.1 (personal research index; respects robots.txt)",
    "contact": os.environ.get("LIBRARIAN_CONTACT", ""),  # optional URL or email appended to the UA
    "min_interval_seconds": 1.0,
    "cache_ttl_seconds": 86400,
    "timeout_seconds": 30,
}


@dataclass
class SourceConfig:
    id: str
    parser: str
    source_type: str
    trust: float = 0.5
    enabled: bool = True
    options: dict = field(default_factory=dict)


@dataclass
class Pack:
    id: str
    name: str
    description: str
    path: Path
    sources: list[SourceConfig]
    ranking: dict
    versions: dict
    crawler: dict

    @property
    def db_path(self) -> Path:
        return DATA_DIR / f"{self.id}.db"

    @property
    def cache_dir(self) -> Path:
        return DATA_DIR / "cache" / self.id

    def source(self, source_id: str) -> SourceConfig:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(f"unknown source {source_id!r} in pack {self.id!r}")

    @property
    def user_agent(self) -> str:
        ua = self.crawler["user_agent"]
        return f"{ua} +{self.crawler['contact']}" if self.crawler.get("contact") else ua


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def list_packs() -> list[str]:
    return sorted(p.name for p in PACKS_DIR.iterdir() if (p / "sources.yaml").exists())


def load_pack(pack_id: str) -> Pack:
    path = PACKS_DIR / pack_id
    meta = _read_yaml(path / "sources.yaml")
    if not meta:
        raise FileNotFoundError(f"no pack at {path} (expected sources.yaml); available: {list_packs()}")
    sources = [
        SourceConfig(
            id=s["id"],
            parser=s["parser"],
            source_type=s.get("source_type", "other"),
            trust=float(s.get("trust", 0.5)),
            enabled=bool(s.get("enabled", True)),
            options=s.get("options", {}) or {},
        )
        for s in meta.get("sources", [])
    ]
    return Pack(
        id=pack_id,
        name=meta.get("name", pack_id),
        description=meta.get("description", ""),
        path=path,
        sources=sources,
        ranking=_read_yaml(path / "ranking.yaml"),
        versions=_read_yaml(path / "versions.yaml"),
        crawler={**CRAWLER, **(meta.get("crawler") or {})},
    )
