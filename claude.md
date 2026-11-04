# CLAUDE.md

## Project

A local, targeted search index ("niche librarian") built as a research tool for AI agents. It crawls a hand-picked list of sources for one niche topic, stores them with useful metadata, and exposes fast search to an agent over MCP. Built for personal use first; sharing on GitHub is optional.

First topic: Esri ArcGIS Enterprise architecture, administration, and real-world experience from other practitioners.

## Architecture

Engine and topic are separate.

- **Engine** (generic): polite crawler, text extractor, storage, hybrid search, ranking, MCP server.
- **Topic pack** (per niche): a config folder containing the source list, one parser per site, the fields to extract, and ranking weights.

Adding a new topic should mean writing a new pack, not touching the engine.

```
engine/
  crawl/        # throttled fetcher, robots.txt, user agent, cache
  parsers/      # reusable: discourse, stackexchange, github, reddit, generic
  index/        # SQLite FTS5 + local vector store
  rank/         # rerank step using pack-defined signals
  mcp_server/   # search + fetch tools
packs/
  esri_enterprise/
    sources.yaml
    parsers/    # site-specific overrides
    ranking.yaml
```

## Sources (Esri pack)

Priority: forum threads, blog posts, conference talks, and docs.

- Esri documentation, release notes, compatibility matrices
- Esri Community
- GIS Stack Exchange (use the official API)
- GitHub issues and discussions (use the official API)
- Reddit r/gis and related subs (use the official API; treat as supplemental)
- Blogs and conference talks where a transcript exists

Prefer an official API or data export over scraping wherever one exists. Write a custom parser only for sites without one.

## Fields stored per item

Source, URL, stable chunk ID, title, post date, product version tags, author, author reputation, account age, accepted-answer or top-vote flag, vote count, content as clean markdown.

## Search and ranking

- Hybrid retrieval: keyword (FTS) plus embeddings, then a rerank step.
- Rank on: version match with the version being asked about, recency where version-sensitive, source trust (vendor staff replies, accepted answers), first-hand evidence (error text, log excerpts, "this fixed it"), and author reputation plus account age weighted per site.
- Deliberately excluded: backlink weighting, keyword-stuffing rewards, SEO-driven freshness bias.
- Source discovery may use links and citations, but only from vetted communities, not links from anywhere.

## Agent interface

- Expose as an MCP server with a search tool and a separate fetch tool.
- Search returns compact results: short snippet, URL, date, version tag, score, stable chunk ID.
- Fetch returns full text as clean markdown.
- Filters: date range, product version, source type.
- Every result must be citable by chunk ID.

## Rules

- Sanctioned access only. Respect robots.txt, throttle by default, identify with a proper user agent.
- Do not bypass anti-bot measures, CAPTCHAs, or login walls. If a site blocks the crawler, stop and report it rather than working around it.
- Out of scope: LinkedIn, X, Discord, and anything behind a login.
- Do not redistribute crawled content. Share the recipe (code, configs, source lists), and let users run the crawl themselves.
- No client data, client names, hostnames, portal URLs, or credentials anywhere in the repo, configs, or test fixtures. Develop and run on a personal machine.

## Conventions

- Python. Prefer stdlib and small, well-known dependencies.
- Each site parser is small and self-contained. Keep one worked example that can be copied to add a new source.
- Keep a README that walks through adding a new source end to end.
- Config-driven: no topic-specific logic in the engine.

## Decisions

- Vector store: sqlite-vec, in the same SQLite file as FTS5 (one .db per pack under data/).
- Embeddings: local Ollama, nomic-embed-text (768-dim).
- Ranking: retrieval ** w * (1 + sum of signal boosts), so quality signals scale relevance rather than replace it.
- Stack Exchange runs keyless for now (~300 requests/day); the crawl pauses and resumes the next day.

## Open items

- Reproduce the Claude in Chrome crash on a known page in a fresh session and determine whether it is a bug or a bot block before relying on browser-based fetching.
- Decide whether a second, broader or funnier example pack (e.g. home lab) is worth shipping alongside the Esri pack.