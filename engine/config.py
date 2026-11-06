"""Load a topic pack and engine settings.

A pack is a folder under packs/ with:
  sources.yaml   pack metadata + the source list
  ranking.yaml   rerank weights and signal patterns
  versions.yaml  how to recognise product versions in text
  synonyms.yaml  equivalent terms for keyword search (optional)
  parsers/       optional site-specific parser overrides
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

def _load_env_file() -> None:
    """API keys from ~/.config/niche-librarian/env (KEY=value or export KEY=value lines), for
    processes that don't start from your shell, such as the MCP server launched by a client.
    Variables already set in the environment win."""
    path = Path(os.environ.get("LIBRARIAN_ENV_FILE", Path.home() / ".config" / "niche-librarian" / "env"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()

ROOT = Path(__file__).resolve().parent.parent
PACKS_DIR = ROOT / "packs"   # packs that ship with the repo (public recipes)
# Where packs are looked up, first match wins: your private packs folder (outside the repo, so
# private recipes can never be committed or pushed), then the repo's own packs. Override with
# LIBRARIAN_PACKS, a list of folders separated by os.pathsep (":" on Linux/macOS, ";" on Windows).
PRIVATE_PACKS_DIR = Path.home() / ".config" / "niche-librarian" / "packs"
PACK_DIRS = [Path(p) for p in os.environ["LIBRARIAN_PACKS"].split(os.pathsep) if p] \
    if os.environ.get("LIBRARIAN_PACKS") else [PRIVATE_PACKS_DIR, PACKS_DIR]
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
    "user_agent": "niche-librarian/0.1 (personal research index; polite crawler)",
    "contact": os.environ.get("LIBRARIAN_CONTACT", ""),  # optional URL or email appended to the UA
    "min_interval_seconds": 1.0,
    "cache_ttl_seconds": 86400,
    "timeout_seconds": 30,
    # robots.txt: Crawl-delay is always honoured (pacing). Disallow rules are not legally
    # binding in the US, so they're enforced only for EU/EEA-hosted sites (country-code
    # domains), where a machine-readable opt-out has legal force (EU Copyright Directive,
    # Art. 4). A pack can set `crawler: {respect_robots: true}` to enforce them everywhere.
    "respect_robots": False,
    "robots_eu": True,
}


@dataclass
class SourceConfig:
    id: str
    parser: str
    source_type: str
    trust: float = 0.5
    enabled: bool = True
    options: dict = field(default_factory=dict)
    # How access was verified: {checked, robots, method, auth, verdict: ok|blocked|needs-key, notes}.
    # A source with verdict "blocked" is never crawled.
    access: dict = field(default_factory=dict)


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
    synonyms: list = field(default_factory=list)
    agent: dict = field(default_factory=dict)  # hints for MCP tool descriptions (example queries)
    web: dict = field(default_factory=dict)  # web_search site shortcuts and hosts never to fetch
    signals: dict = field(default_factory=dict)  # terms and categories for `librarian signals`
    privacy: dict = field(default_factory=dict)  # {authors: keep|pseudonymize|drop} (engine/privacy.py)

    @property
    def author_mode(self) -> str:
        return str((self.privacy or {}).get("authors", "keep"))

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
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_packs() -> list[str]:
    return sorted({p.name for d in PACK_DIRS if d.is_dir() for p in d.iterdir() if (p / "sources.yaml").exists()})


def pack_path(pack_id: str) -> Path:
    for d in PACK_DIRS:
        if (d / pack_id / "sources.yaml").exists():
            return d / pack_id
    return PACK_DIRS[-1] / pack_id


def load_pack(pack_id: str) -> Pack:
    path = pack_path(pack_id)
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
            access=s.get("access", {}) or {},
        )
        for s in meta.get("sources", [])
    ]
    if not any(s.id == "web_pages" for s in sources):  # pages you approved one by one (engine/webpages.py)
        sources.append(SourceConfig(
            id="web_pages", parser="web_pages", source_type="web", trust=0.4, options={"pack": pack_id},
            access={"method": "single pages you approved (librarian pages)", "robots": "checked per page",
                    "auth": "none", "verdict": "ok"}))
    web = meta.get("web") or {}
    if web.get("storage_rights") and not any(s.id == "web_results" for s in sources):
        sources.append(SourceConfig(  # stored search results (engine/webresults.py); never crawled
            id="web_results", parser="web_results", source_type="web", trust=0.3,
            access={"method": "web search API results (plan with storage rights)", "robots": "n/a",
                    "auth": "API key", "verdict": "ok"}))
    return Pack(
        id=pack_id,
        name=meta.get("name", pack_id),
        description=meta.get("description", ""),
        path=path,
        sources=sources,
        ranking=_read_yaml(path / "ranking.yaml"),
        versions=_read_yaml(path / "versions.yaml"),
        synonyms=_read_yaml(path / "synonyms.yaml").get("groups", []),
        agent=meta.get("agent") or {},
        web=web,
        signals=meta.get("signals") or {},
        privacy=meta.get("privacy") or {},
        crawler={**CRAWLER, **(meta.get("crawler") or {})},
    )
