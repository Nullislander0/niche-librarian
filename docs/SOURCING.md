# Sourcing guide: choosing and crawling sources responsibly

This is the field guide for adding sources to a pack: how to decide whether a site may be
crawled, how to crawl it politely, and the platform quirks that came up while building the
included packs. The engine enforces the mechanical parts (robots.txt, pacing, block
detection). The judgment calls are yours, and this guide is how to make them consistently.

## 1. The rules

**Allowed:**
- Official APIs and data exports. Always prefer these.
- Sitemaps the site publishes in its robots.txt or at `/sitemap.xml`.
- Public HTML pages that robots.txt allows for our crawler or for `*`.

**Never:**
- Bypassing a bot challenge: no headless browser to pass Cloudflare, no solving CAPTCHAs.
- Disguising the crawler: no browser user-agent strings, no rotating IPs or proxies.
- Logging in, or using an account to see more than an anonymous visitor would.
- Committing crawled content, raw responses or derived data to the repo.
- Putting API keys, tokens or credentials anywhere but your shell environment.

**robots.txt policy, as implemented:** the crawler obeys rules addressed to its own user
agent (`niche-librarian`) or to `*`, including any `Crawl-delay`. Rules naming *other*
crawlers (ClaudeBot, GPTBot, CCBot and so on) aren't applied automatically. Many sites now
list AI crawlers by name to opt out of AI training and bulk scraping. If you'd rather treat
those opt-outs as applying to you as well, that's a reasonable, stricter choice. Record it in
the source's `access:` notes and leave the source disabled.

**A block means stop.** HTTP 401/403/407, a challenge page ("Just a moment…", "Attention
Required", Cloudflare `cf_chl` markers) or a login wall ends that source's crawl. The fetcher
raises `BlockedError` and reports it. Mark the source `verdict: blocked` and move on. If the
content matters, ask the site's owner; a short, polite note sometimes gets you an allowlist
entry or an export.

## 2. Checking a site before adding it

Do this for every new site, by hand or with a few lines of Python, about five requests:

1. **robots.txt:** what applies to our agent and to `*`? Is there a `Crawl-delay`? A
   `Sitemap:` line? Which AI crawlers are named, if any (for your notes)?
2. **One plain request** for a real content page: does it return the content, or a challenge
   or error page? Check the page *title* and status code. A page that merely loads a CAPTCHA
   *script* (reCAPTCHA for a contact form) isn't a block.
3. **Is there an API?** Look for platform signs: `/latest.json` and `/categories.json`
   (Discourse), `/api/v2/` (Vanilla), `api.php` (MediaWiki), the Stack Exchange API.
4. **How big is it?** Count the sitemap entries, topics per category, or tags. Decide on a
   scope before crawling, not after.
5. **Record it** in the source's `access:` block:

```yaml
access: {checked: 2026-11-05, robots: allowed, method: Discourse JSON API, auth: none, verdict: ok,
         notes: "anything a future maintainer should know"}
```

`verdict` is `ok`, `blocked` or `needs-key`. Sources marked `blocked` or `needs-key` never
crawl. Print the whole table with `librarian sources`.

## 3. Crawling politely

- **Pace:** the default is 1 request per second per host. Packs that crawl forums set
  `min_interval_seconds: 1.5–2`. A site's `Crawl-delay` always wins if it's longer.
- **Back off when told:** `Retry-After` headers, Stack Exchange's `backoff` field and quota
  limits are honoured. The crawler pauses and resumes later rather than pushing through.
- **Identify yourself:** keep the honest user agent. Set `LIBRARIAN_CONTACT` to a URL or
  email so a site admin can reach you.
- **Fetch once:** every response is cached in `data/cache/`, so `librarian rebuild` re-parses
  without touching the network. Incremental runs only fetch what changed.
- **Start small:** `librarian crawl --sample 50` stops each source after 50 threads or pages.
  Use it to prove a source works before committing to a full crawl.

## 4. Scoping: walk around the block, not ten miles

Most communities are far bigger than a pack needs. Narrow on purpose:

- **By section:** pick categories, boards, tags or forums. Leave out gaming, off-topic and
  "ideas" sections unless they're the point.
- **By date:** Discourse and XenForo 2 sources take `since:`. Recent material matters more
  for fast-moving software.
- **By title or URL:** `include` / `include_slugs` regexes filter sitemap entries, for example
  only topics whose slug mentions your product.
- **Cap the giants:** megathreads (thousands of posts in one topic) are capped to the opening
  post plus the newest replies (`max_posts_per_topic`, `max_pages_per_thread`), or crawled
  newest-page-first in slices (`mega_threads`).
- **Estimate first:** requests × pacing = time. For example, 3,000 requests at 1.5 seconds is
  about 75 minutes, plus embedding.

## 5. Platform field notes

| Platform | Parser | How it's accessed | Gotchas |
|---|---|---|---|
| **Discourse** | `discourse` | `/categories.json`, `/c/<slug>/<id>/l/latest.json`, `/t/<id>.json`, `/t/<id>/posts.json` | Parent categories include subcategories. Pinned topics sit on top regardless of age. Megathreads with 7,000+ posts need `max_posts_per_topic`. Vendor staff show up as a group, flair or user title; set `vendor_groups` |
| **Vanilla Forums** | `vanilla` | `/api/v2/discussions` and `/api/v2/comments`, filtered by category and sorted by date | Listings stop at 10,000 results: the parser pages by date, not page number. The API sometimes can't return one malformed post ("Validation failed"); the parser isolates it and skips it. Comments come in bulk per category, so a `--sample` run shows threads without replies |
| **Stack Exchange** | `stackexchange` | the official API; a free key raises the quota from ~300 to 10,000 requests a day | Without a key you can't page past page 25, so the parser pages by date. Create an API filter to get `body_markdown`. Content is CC BY-SA |
| **MediaWiki** | `mediawiki` | `api.php` (`generator=allpages` plus `action=parse`) | Translated pages are often separate titles ("Page (Español)"): exclude them by regex. Only edited pages (new `lastrevid`) are refetched. Strip edit links and tables of contents |
| **XenForo 1** | `xenforo1` | HTML: tag pages, forum listings, threads | Some forums rename the post-count field (e.g. "Oddometer"). Single-post permalinks are often disallowed in robots.txt, so read whole thread pages |
| **XenForo 2** | `xenforo2` | HTML: forum listings sorted by latest activity, thread pages | User-panel fields are icon tooltips (`title="Joined"`). Staff are marked `username--staff`. `[SOLVED]` is a title label, not a per-post answer. Themes vary, so check selectors against the live site |
| **Docs sites** | `sitemap_html` | the sitemap, an explicit URL list, or links from an index page | Find the content element (`article`, `div#content`, …). Strip `<nav>`, breadcrumbs and "Table of contents" blocks. Exclude print-preview and archive URLs. Sitemaps can list the same page several times. Some docs load sub-pages with JavaScript, so list them explicitly |

**Not yet supported:** phpBB, Invision and vBulletin (HTML only). Reddit and GitHub need keys
and a read of their API terms. Discord, Slack and anything behind a login are out of scope.

## 6. Signals worth capturing

Ranking is only as good as the metadata a parser extracts. When a platform provides these,
capture them; when it doesn't, leave the field `None` rather than guess:

| Signal | Typical source |
|---|---|
| Accepted / best answer | Stack Exchange `is_accepted`, Discourse `accepted_answer`, Vanilla answer status |
| Vendor staff | a staff flag, group, flair or user title. Configure the pattern per source |
| Recognized expert | MVP labels, trust level 4, high-rank titles |
| Votes / likes | score, likes, reactions |
| Reputation, account age | reputation, post count, join date |
| Version | a `<meta name="version">` on docs pages; otherwise `versions.yaml` patterns. Use per-product prefixes in packs that cover several products |

## 7. Content and licensing

- Crawled content belongs to its authors and sites. This project shares the recipe, never
  the data. Results always link back to the original page.
- Note licences in `access:` notes, e.g. CC BY-SA for Stack Exchange and the Arch Wiki.
- Personal use is the intended use. Selling access to crawled content is redistribution,
  and most sites' terms forbid it.
- Test fixtures are written by hand, never copied from crawled pages.
