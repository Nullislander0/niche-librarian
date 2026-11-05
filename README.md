# niche-librarian

Give your AI assistant a librarian for one niche: a **local search index of the docs and
forums that practitioners actually use**, served to the agent over MCP.

General web search and a model's own memory are weakest exactly where specialist work
lives: version-specific details, error messages, "this broke after the upgrade", and the
difference between an official fix and a forum guess. niche-librarian crawls a hand-picked
set of sources for one topic, through official APIs wherever they exist. It records who
said what, on which version, with what standing (vendor docs, staff reply, accepted answer,
or just a post), then ranks results with that in mind. Every result is citable back to the
original post or page.

This repo ships the **recipe** (code, configs and source lists), never crawled content. You
run the crawl yourself, and the data stays on your machine in `data/`, which git ignores.

## Topic packs included

| Pack | What it covers | Sources | Good first pack? |
|---|---|---|---|
| **`homelab`** | Running your own home server: Proxmox VE, TrueNAS and ZFS, Docker and Compose, and the Linux underneath | Proxmox docs and wiki, TrueNAS docs and forums, Docker docs and forums, Arch Wiki, Level1Techs, LinuxServer.io | **Yes:** broad audience, all public APIs, no keys needed |
| `esri_enterprise` | Esri ArcGIS Enterprise administration: installs, upgrades, federation, Data Store, Web Adaptor | GIS Stack Exchange, Esri Community, Esri docs, knowledge base and bug database | For GIS administrators |
| `dr650` | Suzuki DR650 builds, mods, maintenance and long-term ownership | ADVrider (the DR650 thread, `dr650`-tagged and title-matched threads) | An example of a forum-only pack |

Each pack is independent: its own database, its own MCP server, its own ranking. Writing a
pack for your own niche is the point. See [Writing your own pack](#writing-your-own-pack).

## Quick start (home server pack)

You need Python 3.11+ and [Ollama](https://ollama.com), which provides the local embeddings
for semantic search. Without Ollama, search still works in keyword-only mode.

**1. Install** (Linux / macOS):

```bash
git clone https://github.com/Nullislander0/niche-librarian.git
cd niche-librarian
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,rerank]"
ollama pull nomic-embed-text
.venv/bin/pytest            # optional: should pass
```

On Windows (PowerShell), use `py -3 -m venv .venv` and `.venv\Scripts\` in place of
`.venv/bin/` throughout.

**2. Pick the pack and check its sources:**

```bash
export LIBRARIAN_PACK=homelab     # or pass --pack homelab to each command
.venv/bin/librarian sources       # how each source is accessed, and when that was checked
```

**3. Crawl a sample first.** `--sample 50` stops each source after 50 threads or pages: a
walk around the block that proves every source works, in about 15 minutes. Run the full
crawl once you're happy with it.

```bash
.venv/bin/librarian crawl --sample 50   # quick end-to-end test of the pack
.venv/bin/librarian crawl               # full first crawl (see "What to expect" below)
```

**4. Search from the terminal**, to see what you got:

```bash
.venv/bin/librarian outline
.venv/bin/librarian search "Proxmox VE 8 GPU passthrough to VM not working" --explain
```

**5. Connect your AI assistant**, as covered under [Connect an agent (MCP)](#connect-an-agent-mcp)
below. Then ask your assistant real questions. It searches the index, reads the best
threads and cites them.

### What to expect from a first crawl
- **Time:** the docs and wikis take an hour or two. The forums take most of a night,
  because the crawler paces itself (`min_interval_seconds` in the pack's `sources.yaml`).
  Leave it running. It keeps the machine awake while it works, and it resumes where it
  stopped if interrupted.
- **Disk:** packs end up in the hundreds of megabytes up to about 1 GB each, plus a raw
  response cache in `data/cache/`.
- **Later runs** only fetch what changed: new topics, edited wiki pages, new posts in
  threads you already have.

## How it works

```
sources ──► parsers ──► version tagging ──► chunks ──► SQLite (FTS5 + sqlite-vec) ──► rerank ──► MCP
            (per site)   (versions.yaml)                (one .db per pack)            (ranking.yaml)
```

- **The engine** (`engine/`) is generic: a polite fetcher, parsers, storage, hybrid search,
  ranking and the MCP server.
- **A topic pack** (`packs/<name>/`) is configuration: the source list, version patterns,
  synonyms, ranking weights and any site-specific parser overrides. To add a topic, write
  a pack. The engine shouldn't need changes.

Search combines keyword search (SQLite FTS5, BM25) with semantic search (Ollama
`nomic-embed-text` vectors in sqlite-vec), merged with reciprocal rank fusion. A small
cross-encoder then re-reads the top candidates against the query (FastEmbed, ONNX Runtime,
no PyTorch). On the Esri pack's 200-question test, that raised top-1 accuracy from 73% to
about 80%. Last, results are reranked using the pack's signals:
- Version match with the version being asked about, e.g. `pve 8.2` or `truenas 24.10`.
- Recency, only when the question is version-sensitive.
- Authority: vendor docs, knowledge base and bug reports outrank forum opinion.
- Accepted answers, vendor staff, recognized experts, and votes or likes.
- First-hand evidence: logs, configs, "after upgrading…", "this fixed it".
- Author reputation and account age, where the site provides them.

These signals scale relevance rather than replace it, so a popular answer to a different
question can't outrank the right one. There's deliberately no backlink weighting, no
keyword-stuffing reward and no blanket freshness bias.

## Setup details

**Platforms.** It's developed on Linux, and the tests run on Linux, Windows and macOS in CI.
- **Sleep:** long jobs (`crawl`, `rebuild`, `embed`, `eval`) keep the machine awake while
  they run, and the machine sleeps normally afterwards. On Linux this uses a desktop idle
  inhibit plus `systemd-inhibit`, on Windows `SetThreadExecutionState`, and on macOS
  `caffeinate`. Set `LIBRARIAN_NO_STAY_AWAKE=1` to turn this off.
- **macOS:** semantic search needs a Python that can load SQLite extensions. Homebrew's
  Python (`brew install python`) can; Apple's bundled Python, and likely the python.org
  installer, can't. In that case the index runs keyword-only (full-text search plus the
  reranker) and logs a warning.

**Settings** come from environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `LIBRARIAN_PACK` | `esri_enterprise` | Default pack for every command |
| `LIBRARIAN_DATA` | `./data` | Databases, caches, models, test sets |
| `LIBRARIAN_OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `LIBRARIAN_EMBED_MODEL` / `LIBRARIAN_EMBED_DIM` | `nomic-embed-text` / `768` | Embedding model. Run `librarian embed --reset` after changing it |
| `LIBRARIAN_CONTACT` | (empty) | A URL or email appended to the crawler's user agent |
| `STACKEXCHANGE_KEY` | (none) | Optional, for the Esri pack: raises the Stack Exchange quota from ~300 to 10,000 requests a day ([register an app](https://stackapps.com/apps/oauth/register)) |

Keep keys in your shell environment, never in the repo.

## Everyday commands

```bash
librarian crawl [--source ID] [--sample N] [--max-requests N]   # fetch and index; resumable
librarian sources                                  # sources and their access records
librarian outline                                  # what the index covers: sources, boards, tags, versions
librarian search "query" [--version 11.3] [--explain]
librarian fetch "<chunk_id>" [--scope chunk|document|thread] [--max-tokens N]
librarian rebuild                                  # re-parse saved responses (no network)
librarian retag                                    # reapply versions.yaml to the whole index (seconds)
librarian eval-build / eval                        # build and run the pack's retrieval test
```

Every raw response is kept in `data/cache/`, so `rebuild` can re-parse everything after you
change a parser or the chunking, without touching the network.

## Saved searches and alerts

Save a query once, and get only what's **new** since you last checked:

```bash
librarian watch add pve-upgrades "Proxmox VE upgrade problems" --min-score 0.6
librarian crawl --watch --notify        # crawl, then report new matches (desktop notice)
librarian watch run                     # or on its own; writes a report to data/watches/
librarian watch list | remove NAME
librarian search "query" --new-since 2026-11-01     # ad hoc: only recently indexed material
```

Each document records when it first entered the index, so "new" means genuinely new, not
re-crawled. Watches are personal and live in `data/watches/`, outside the repo. Agents can ask
the same thing through MCP: `search(..., new_since="YYYY-MM-DD")`.

**Web watches** run a query against the live web instead of your index, catching rare
mentions on sites you don't crawl:

```bash
export BRAVE_API_KEY=...                # https://api-dashboard.search.brave.com
librarian watch add vfio "GPU passthrough regression" --web --freshness pw --site forum.proxmox.com
librarian watch run --web-only
```

**Release alerts** use Brave's news index: list your products under `web.releases` in the
pack's `sources.yaml`, and get one watch per product for new versions, patches, security
advisories and end-of-support notices:

```yaml
web:
  releases:
    - {name: Proxmox VE, match: 'Proxmox'}      # match: must appear in the title or snippet
    - {name: TrueNAS, match: 'TrueNAS'}
```

```bash
librarian watch add-releases            # creates/updates release-proxmox-ve, release-truenas
librarian watch run --web-only --notify
librarian watch add fw "OPNsense firmware" --web --backend brave-news --require 'OPNsense'
```

Brave's API terms don't allow storing results, so web results are **shown, never saved**.
Only fingerprints of URLs already seen are kept, so later runs show only new links. Your
plan's monthly limit is read from Brave's response headers (see the quota line below).

## Beyond the index: web search and single pages

Your index holds what you could crawl. For the rest (very recent posts, sites that block
crawlers, one-off pages) the agent gets three more MCP tools:

| Tool | What it does | Writes anything? |
|---|---|---|
| `web_search` | live Brave search (`news=true` for the news index), results marked `[indexed]` / `[host crawled by source …]` / `[blocked host]`. Only offered when `BRAVE_API_KEY` is set | no |
| `read_url` | fetches one page politely (robots.txt, throttle, challenge detection) and returns markdown for this answer | no |
| `index_url` | **proposes** a page for the index | a queue entry only |

Nothing an agent finds goes into your index until you approve it:

```bash
librarian pages list                    # what agents proposed, and why
librarian pages approve [URL ...]       # approve (all pending by default) and index
librarian pages reject [URL ...]        # won't be proposed again
librarian pages add URL --note "..."    # add one yourself
librarian pages remove URL
librarian web "query" --site reddit_gis # search the web yourself; prints the quota
```

Approved pages become the `web_pages` source, ranked below the vendor docs and forums your
recipe crawls. Pages under a part of a site one of your sources already crawls are refused,
because that source's parser keeps richer metadata. What's stored comes from the page's own
site, never from search results.

Each pack can name site shortcuts and hosts never to fetch in `sources.yaml`:

```yaml
web:
  sites: {reddit_homelab: reddit.com/r/homelab, reddit_proxmox: reddit.com/r/Proxmox}
  blocked_hosts: [example-forum.com]     # e.g. sits behind a bot challenge
```

**Quota:** agent searches stop when the month's remaining Brave quota falls below a quarter,
so your watches always have room (`LIBRARIAN_WEB_AGENT_SHARE=0.75` to change it), and at 50
per server session (`LIBRARIAN_WEB_SESSION_CAP`). Keys can live in
`~/.config/niche-librarian/env` (`export NAME=value` lines): the engine reads that file
itself, so MCP clients that don't start from your shell still find them.

## Connect an agent (MCP)

Each pack is its own MCP server (`librarian serve --pack <pack>`), so register one per topic
you want an assistant to use. Use absolute paths.

**Claude Code**

```bash
# Linux / macOS
claude mcp add homelab-librarian --scope user -- /ABS/PATH/.venv/bin/librarian serve --pack homelab
# Windows
claude mcp add homelab-librarian --scope user -- C:\ABS\PATH\.venv\Scripts\librarian.exe serve --pack homelab
```

Then start a new session. To make the assistant reach for it without being asked, add a
line like this to your `CLAUDE.md`: *"For home server, Proxmox, TrueNAS or Docker questions,
search the homelab-librarian MCP before answering, and cite chunk IDs."*

**Claude Desktop.** Edit `claude_desktop_config.json` (Settings → Developer → Edit Config).
The file lives in `~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on
Windows, and `~/.config/Claude/` on Linux.

```json
{
  "mcpServers": {
    "homelab-librarian": {
      "command": "/ABS/PATH/.venv/bin/librarian",
      "args": ["serve", "--pack", "homelab"]
    }
  }
}
```

On Windows, use `"command": "C:\\ABS\\PATH\\.venv\\Scripts\\librarian.exe"`. JSON needs the
backslashes doubled.

**Tools the agent gets:**

| Tool | What it returns |
|---|---|
| `outline(source)` | Map of the index: sources, boards, tags, date ranges, version coverage. Read this first |
| `search(query, limit, version, version_mode, date_from, date_to, source_type, source, explain)` | Compact results: chunk_id, date, versions, source, flags (official, accepted, staff, expert), score, approximate tokens, URL, snippet |
| `fetch(chunk_id, scope, max_tokens)` | Full markdown with citation metadata. `scope` is `chunk`, `document` or `thread`. Threads come back accepted answer first, then staff, experts and top-voted posts, up to the token budget |
| `index_info()` | Raw statistics |

The tool descriptions coach the agent: one topic per search, pass the user's version, stop
after a few empty searches, and say when sources disagree.

## Writing your own pack

A pack is a folder under `packs/` holding a few YAML files. Copy `packs/homelab/` as a
starting point. The full field guide, covering access rules, scoping and platform quirks,
is [docs/SOURCING.md](docs/SOURCING.md).

### Building a recipe, step by step

1. **Pick a narrow niche.** "Home servers" or "one product's administration" works; "all of
   tech" doesn't. Write one sentence on who the pack is for. It becomes the pack's
   `description`, which agents read.
2. **List where practitioners actually go:** vendor docs, the vendor's community, the
   independent forum everyone recommends, the relevant Stack Exchange tags, a wiki. Ask
   someone who does the work.
3. **Check each site's access** before writing any config: robots.txt, one plain request,
   whether there's an API or sitemap, and its rough size. See
   [the checklist](docs/SOURCING.md#2-checking-a-site-before-adding-it). Drop anything that
   blocks or challenges the crawler.
4. **Match each source to a parser** (table below). If one fits, the source is config only.
   If none does, write a parser (see below) and test it against a hand-written fixture.
5. **Scope each source:** categories or tags, a `since:` date, slug filters, caps for
   megathreads. Estimate requests × pacing before you run anything.
6. **Write `versions.yaml` and `synonyms.yaml`** for the niche's version numbers, codenames
   and abbreviations. Check them with `librarian retag` and a few searches.
7. **Run `librarian crawl --sample 50`.** About 15 minutes proves every source works end to
   end. Read a few results with `librarian search ... --explain` and `librarian fetch`.
8. **Measure, then tune.** If a source has accepted answers, build a retrieval test
   (`eval-build`, `eval`) and change ranking or synonyms only when the numbers improve.
9. **Run the full crawl,** then share the recipe. The repo never includes the data.

| File | What it holds |
|---|---|
| `sources.yaml` | Pack name and description, example queries for agents, request pacing, and the sources: each with a `parser`, `source_type`, `trust`, parser `options`, and an `access:` record |
| `versions.yaml` | How to spot versions. Use a single `pattern`, or several `products` with prefixes, aliases (e.g. codenames) and source-scoped rules |
| `synonyms.yaml` | Query expansion. `"ags -> arcgis server"` is one-way; `[a, b]` is two-way. One-way is usually safer |
| `ranking.yaml` | Signal weights, which source types count as authoritative, first-hand evidence patterns, reranker settings |

**Parsers already available.** If your sources run on one of these, a new pack is config only:

| Parser | For |
|---|---|
| `discourse` | Any Discourse forum, through its built-in JSON API |
| `mediawiki` | Any MediaWiki site, through `api.php` |
| `sitemap_html` | Docs sites: pages from sitemaps, explicit URLs or an index page, with a content selector |
| `stackexchange` | Any Stack Exchange site, through the official API |
| `vanilla` | Vanilla Forums communities, through API v2 |
| `xenforo1` | XenForo 1.x forums (HTML; respects robots.txt) |
| `xenforo2` | XenForo 2.x forums (HTML): forum listings by latest activity, thread pages, staff and user-panel fields |

**Record how you checked access.** Every source gets an `access:` block. A source whose
verdict is `blocked` or `needs-key` is never crawled:

```yaml
access: {checked: 2026-11-05, robots: allowed, method: Discourse JSON API, auth: none, verdict: ok}
```

Before adding a site, check its robots.txt and whether a plain request gets real content or
a bot challenge. Prefer an official API or export. If a site challenges or blocks the
crawler, mark it blocked and move on; never work around it.

**Test your pack's search quality.** `librarian eval-build` picks threads that have accepted
answers and rewrites each question in different words. By default it uses a local Ollama
model (`LIBRARIAN_EVAL_MODEL`), which should be one that answers directly rather than
"thinking" at length; `--rewrite none` leaves the queries blank so you can write them
yourself. `librarian eval` then reports how often search re-finds the original thread
(top-1, top-5, top-10). Run it before and after any ranking or synonym change. A pack needs
a source with accepted answers for this.

### A new kind of site needs a parser
Copy `engine/parsers/stackexchange.py`, the most thoroughly commented example, or the closest
existing parser:

```python
# engine/parsers/mysite.py  (or packs/<pack>/parsers/mysite.py for a pack-only parser)
from engine.parsers.base import CrawlContext, Document

class Parser:
    POSITION_KEY_PREFIXES = ("cursor",)          # state keys that hold crawl position

    def __init__(self, ctx: CrawlContext):
        self.ctx = ctx                            # ctx.options = this source's options

    def crawl(self):
        st = self.ctx.state.get("cursor") or {"page": 1}   # resumable position
        while True:
            self.ctx.check_budget()               # respects --max-requests
            data = self.ctx.fetcher.get_json("https://api.example.org/posts",
                                             {"page": st["page"]})   # robots/throttle/cache/UA
            for post in data["posts"]:
                yield Document(
                    doc_id=f"{self.ctx.source.id}:{post['id']}",     # stable forever
                    source=self.ctx.source.id, source_type=self.ctx.source.source_type,
                    kind="post", url=post["url"], title=post["title"],
                    content_md=post["markdown"], post_date=post["created"],
                    author=post["author"], score=post.get("votes"),
                )
            if not data["has_more"]:
                break
            st["page"] += 1
            self.ctx.state.set("cursor", st)      # save progress after each page
```

Rules for parsers:
- Make every request through `ctx.fetcher`, which handles robots.txt, throttling, caching,
  block detection and the user agent.
- `doc_id` must be stable. Chunk IDs (`<doc_id>#c<n>`) are what agents cite.
- Return content as markdown, plus whatever metadata the site really provides (votes,
  accepted answers, staff status, reputation). Leave a field `None` rather than guess.
- Replies set `parent_id` to the thread root. That makes `fetch(scope="thread")` work, and
  replies inherit the thread's version tags.
- Parsers don't do version tagging, chunking or ranking. The engine handles those from
  pack config.
- Add a synthetic fixture and test under `tests/`, written by hand, never copied from
  crawled content.

## Rules this project follows

- **Sanctioned access only.** It obeys the robots.txt rules that address it or all crawlers
  (`*`), including any Crawl-delay, paces requests by default, and identifies itself with an
  honest user agent.
- **It never works around a block:** no bypassing bot challenges, CAPTCHAs or login walls.
  A blocked source is reported and skipped.
- **Recipe, not data.** Crawled content is never committed or redistributed.
- **Nothing private in the repo:** no client data, hostnames, internal URLs or credentials,
  in code, configs or fixtures.

The reasoning behind these rules, the access-check checklist, scoping advice and per-platform
notes are in [docs/SOURCING.md](docs/SOURCING.md).

## Layout

```
engine/
  crawl/fetcher.py      polite HTTP: robots.txt, pacing, cache, retries, block detection
  parsers/              base.py, stackexchange (worked example), vanilla, discourse, mediawiki,
                        sitemap_html, xenforo1, html_md (HTML -> markdown)
  index/                store (FTS5 + sqlite-vec), chunking, Ollama embeddings
  rank/                 rerank.py (fusion + pack signals), cross.py (cross-encoder)
  versions.py           version tagging from versions.yaml
  synonyms.py           query expansion from synonyms.yaml
  indexer.py            crawl -> tag -> chunk -> store -> embed
  search.py             hybrid search, fetch, outline (shared by CLI and MCP)
  evaluate.py           retrieval test: build and run
  awake.py              keep the machine awake during long jobs
  mcp_server/server.py  MCP tools
  cli.py                the `librarian` command
packs/
  homelab/  esri_enterprise/  dr650/
tests/
```

## License

MIT. See [LICENSE](LICENSE). This covers the code and configs only: crawled content belongs
to its authors and sites, and is never part of this repository.
