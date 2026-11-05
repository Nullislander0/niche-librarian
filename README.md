# niche-librarian

A local, targeted search index for AI agents. It crawls a hand-picked list of sources for
one niche topic, stores them with useful metadata, and gives an agent fast hybrid search
over MCP.

Two topic packs are included:
- **`esri_enterprise`:** Esri ArcGIS Enterprise architecture, administration and practitioner
  experience, from GIS Stack Exchange, Esri Community, and Esri's docs, knowledge base and bug
  database.
- **`dr650`:** Suzuki DR650 builds, mods and long-term ownership, from ADVrider.

This repo ships the recipe (code, configs and source lists), never crawled content. You run
the crawl yourself, and the data stays in `data/`, which is gitignored.

## How it works

```
sources ──► parsers ──► version tagging ──► chunks ──► SQLite (FTS5 + sqlite-vec) ──► rerank ──► MCP
            (per site)   (versions.yaml)                (one .db per pack)            (ranking.yaml)
```

- **The engine** (`engine/`) is generic: a polite fetcher, parsers, storage, hybrid search,
  ranking and the MCP server.
- **A topic pack** (`packs/<name>/`) holds the source list, any parser overrides, version
  patterns and ranking weights. To add a topic, write a pack. The engine shouldn't need changes.

Search combines keyword search (SQLite FTS5, BM25) with semantic search (Ollama
`nomic-embed-text` vectors in sqlite-vec). The two lists are merged with reciprocal rank
fusion, then reranked using signals from `ranking.yaml`:
- Version match with the version being asked about.
- Recency, only when the query is version-sensitive.
- Source trust.
- Accepted and top-voted answers.
- First-hand evidence: error codes, logs, "this fixed it".
- Author reputation and account age.

Those signals multiply relevance rather than add to it. Optionally, a small
cross-encoder (FastEmbed, ONNX Runtime) re-reads the top 20 candidates against the query
first. On the pack's eval, it raised top-1 accuracy from 73% to about 80%. It's configured
under `reranker:` in `ranking.yaml`, and the model downloads to `data/models/` on first use.
Compare models with `librarian eval --reranker <model>`. The ranking deliberately has no
backlink weighting, no keyword-stuffing rewards and no blanket freshness bias.

## Setup

Requires Python 3.11+ and [Ollama](https://ollama.com) for embeddings. Without Ollama,
search still works in keyword-only mode. It's developed on Linux; the tests also run on
Windows and macOS in CI.

**Linux / macOS**

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,rerank]"   # rerank = optional cross-encoder (ONNX, no PyTorch)
ollama pull nomic-embed-text
.venv/bin/pytest
```

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install -e ".[dev,rerank]"
ollama pull nomic-embed-text
.venv\Scripts\pytest
```

The examples below use the Linux and macOS path `.venv/bin/librarian`. On Windows, use
`.venv\Scripts\librarian.exe` instead.

Platform notes:
- **Sleep:** long jobs keep the machine awake automatically. On Linux this uses the desktop
  idle inhibit plus `systemd-inhibit`, on Windows `SetThreadExecutionState`, and on macOS
  `caffeinate`. See `engine/awake.py`.
- **macOS:** the python.org and Homebrew builds of Python support the SQLite extensions that
  `sqlite-vec` needs. Apple's bundled `/usr/bin/python3` may not, so use a python.org or
  Homebrew Python.

## Crawl and search

```bash
.venv/bin/librarian crawl                    # all enabled sources in the default pack
.venv/bin/librarian crawl --max-requests 20  # cap network requests per source this run
.venv/bin/librarian stats
.venv/bin/librarian outline                  # what the index covers: sources, boards, tags, versions
.venv/bin/librarian search "federation fails after upgrade" --version 11.3 --explain
.venv/bin/librarian fetch "gis_se:a:12345#c0" --scope thread
```

Crawls are incremental and resumable. Each source saves its position, so if a run stops
(request budget, API quota, Ctrl-C), the next run continues from that point.

Every raw API response is kept in `data/cache/`. It counts as fresh for a day, and is
never deleted. That allows rebuilding without the network:

```bash
.venv/bin/librarian rebuild   # re-parse saved responses after changing a parser or chunking
.venv/bin/librarian retag     # recompute version tags after editing versions.yaml (seconds)
```

Long jobs (`crawl`, `rebuild`, `embed`, `eval`) ask the desktop to stay awake while they
run, the way a video player does, and release the request when they finish. The machine
then sleeps normally afterwards. Set `LIBRARIAN_NO_STAY_AWAKE=1` to turn this off.

`rebuild` replays from the start, using throwaway crawl state, so your real resume points
stay untouched. Unchanged posts keep their embeddings.

**Stack Exchange quota.** Without a key, the API allows about 300 requests per IP per day.
The crawler stops when fewer than 25 remain, and you rerun it the next day. To get 10,000
requests a day, register an app at <https://stackapps.com/apps/oauth/register> and
`export STACKEXCHANGE_KEY=...`. Keep the key out of the repo.

Settings come from environment variables:

| Variable | Default |
|---|---|
| `LIBRARIAN_DATA` | `./data` |
| `LIBRARIAN_OLLAMA_URL` | `http://localhost:11434` |
| `LIBRARIAN_EMBED_MODEL` / `LIBRARIAN_EMBED_DIM` | `nomic-embed-text` / `768` |
| `LIBRARIAN_CONTACT` | (empty), a URL or email appended to the crawler's user agent |

If you change the embedding model, run `librarian embed --reset` afterwards.

## Connect an agent (MCP)

The server runs over stdio: `librarian serve --pack <pack>`. Each pack is its own server,
with its own database and tools, so register one per topic you want an agent to use. Use
absolute paths in the configs below.

**Claude Code**

```bash
# Linux / macOS
claude mcp add esri-librarian --scope user -- /ABS/PATH/.venv/bin/librarian serve --pack esri_enterprise
# Windows
claude mcp add esri-librarian --scope user -- C:\ABS\PATH\.venv\Scripts\librarian.exe serve --pack esri_enterprise
```

**Claude Desktop.** Edit `claude_desktop_config.json` (Settings → Developer → Edit Config).
The file lives in `~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on
Windows, and `~/.config/Claude/` on Linux.

```json
{
  "mcpServers": {
    "esri-librarian": {
      "command": "/ABS/PATH/.venv/bin/librarian",
      "args": ["serve", "--pack", "esri_enterprise"]
    },
    "dr650-librarian": {
      "command": "/ABS/PATH/.venv/bin/librarian",
      "args": ["serve", "--pack", "dr650"]
    }
  }
}
```

On Windows, use `"command": "C:\\ABS\\PATH\\.venv\\Scripts\\librarian.exe"`. JSON needs the
backslashes doubled.

Tools:

| Tool | What it returns |
|---|---|
| `search(query, limit, version, version_mode, date_from, date_to, source_type, source, explain)` | Compact results: chunk_id, date, versions, source, flags, score, approximate token count, URL, snippet |
| `outline(source)` | Map of the index (sources, boards, tags, date ranges, version coverage). Read this first |
| `fetch(chunk_id, scope)` | Full markdown with citation metadata. `scope` is `chunk`, `document` or `thread` |
| `index_info()` | Sources, counts, date ranges, version tag counts |

`version_mode="boost"` (the default) ranks matching versions higher. `"filter"` keeps only
documents tagged with that version; `"11"` matches every 11.x.

## Adding a new source, end to end

Here's how you'd add a new Stack Exchange site, then a new kind of site.

### 1. Add it to the pack

If an existing parser fits (here, a second Stack Exchange site), you only need config.
Add this to `packs/esri_enterprise/sources.yaml`:

```yaml
  - id: serverfault
    parser: stackexchange
    source_type: forum
    trust: 0.5
    options:
      site: serverfault
      tag_tiers:
        - {tier: 1, tags: [arcgis]}
```

Run `librarian crawl --source serverfault --max-requests 5` and check `librarian stats`.

### 2. A new kind of site needs a parser

Always prefer an official API or data export. Only scrape when neither exists, and stop if
the site blocks the crawler. Copy `engine/parsers/stackexchange.py` as the worked example:

```python
# engine/parsers/mysite.py  (or packs/<pack>/parsers/mysite.py for a pack-only parser)
from engine.parsers.base import CrawlContext, Document

class Parser:
    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx                                    # ctx.options = this source's options

    def crawl(self):
        st = self.ctx.state.get("cursor") or {"page": 1}  # resumable position
        while True:
            self.ctx.check_budget()                       # respects --max-requests
            data = self.ctx.fetcher.get_json("https://api.example.org/posts",
                                             {"page": st["page"]})  # robots/throttle/cache/UA
            for post in data["posts"]:
                yield Document(
                    doc_id=f"{self.ctx.source.id}:{post['id']}",  # stable forever
                    source=self.ctx.source.id, source_type=self.ctx.source.source_type,
                    kind="post", url=post["url"], title=post["title"],
                    content_md=post["markdown"], post_date=post["created"],
                    author=post["author"], score=post.get("votes"),
                )
            if not data["has_more"]:
                break
            st["page"] += 1
            self.ctx.state.set("cursor", st)              # save progress after each page
```

Rules for parsers:
- Make every request through `ctx.fetcher`. Never call `urllib`/`requests` directly.
- `doc_id` must be stable. Chunk IDs (`<doc_id>#c<n>`) are what agents cite.
- Return content as markdown, plus whatever metadata the site really provides (votes,
  accepted, staff, reputation). Leave a field `None` rather than guess.
- Replies set `parent_id` to the thread root. That makes `fetch(scope="thread")` work, and
  replies inherit the thread's version tags.
- Parsers don't do version tagging, chunking or ranking. The engine handles those from
  pack config.
- Don't build a parser for anything behind a login, or for sites that block crawlers.

### 3. Test it

Add a synthetic fixture under `tests/fixtures/`, written by hand rather than copied from
crawled content, and a test modelled on `tests/test_pipeline.py`.

### 4. Synonyms

`packs/<pack>/synonyms.yaml` lists equivalent terms: renames, abbreviations and spellings,
e.g. `[arcgis server, ags]`. If a query contains one term from a group, keyword search also
looks for the others. Search output shows which terms were added ("also searched").

### 5. Tune ranking

Use `librarian search "..." --explain` to see each signal's contribution, then adjust
`ranking.yaml`. Per-source trust is set in `sources.yaml`.

## Rules this project follows

- Sanctioned access only. It respects robots.txt, throttles by default (1 request per
  second per host, longer if robots.txt asks), and identifies itself with a proper user agent.
- It never bypasses anti-bot measures, CAPTCHAs or login walls. A blocked source is reported
  and skipped.
- It doesn't redistribute crawled content.
- No client data, client names, hostnames, portal URLs or credentials go in the repo,
  configs or fixtures.

## Layout

```
engine/
  crawl/fetcher.py      polite HTTP: robots.txt, throttle, cache, block detection
  parsers/base.py       Document, CrawlContext, parser loading
  parsers/stackexchange.py   worked example (official API)
  parsers/vanilla.py    Vanilla Forums API v2 (Esri Community)
  parsers/html_md.py    stdlib HTML -> markdown
  synonyms.py           query expansion from synonyms.yaml
  index/                store (FTS5 + sqlite-vec), chunking, Ollama embeddings
  rank/rerank.py        RRF fusion + pack-driven rerank
  versions.py           version tagging from versions.yaml
  indexer.py            crawl → tag → chunk → store → embed
  search.py             hybrid search + fetch (shared by CLI and MCP)
  mcp_server/server.py  MCP tools
  cli.py                `librarian` command
packs/esri_enterprise/
  sources.yaml  versions.yaml  ranking.yaml  synonyms.yaml  parsers/
tests/
```
