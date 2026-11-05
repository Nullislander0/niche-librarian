"""Scout: find candidate sources for a topic and draft their sources.yaml entries.

    librarian scout "prescription drug side effects"

1. A few web searches (Brave, discussion results first) find where the topic is discussed.
2. Hits are grouped by site and ranked: sites with many forum-thread hits come first.
3. Each top site gets one polite visit through the crawler's Fetcher: robots.txt, then one
   of its thread pages. That identifies the forum software (Discourse, XenForo, Vanilla,
   phpBB, ...) and runs the access check: robots for our user agent, bot challenges, logins.
4. Sites whose software has a parser get a draft `sources.yaml` entry with its `access:`
   block filled in. You still choose the boards or categories and review it before crawling.

Search results are only printed (Brave's terms), and nothing is written to the index.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from collections import defaultdict

from engine.config import CRAWLER, DATA_DIR, Pack
from engine.crawl.fetcher import BlockedError, FetchError, Fetcher
from engine.webpages import host, owning_source

QUERY_TEMPLATES = ["{t} forum", "{t} forum discussion thread", "{t} community message board",
                   "{t} experiences forum"]

# Platforms that can't be crawled without a paid API or a login: report, don't visit.
KNOWN = {
    "reddit.com": "Reddit: paid API (needs-key)",
    "facebook.com": "Facebook: login required", "instagram.com": "Instagram: login required",
    "x.com": "X/Twitter: paid API", "twitter.com": "X/Twitter: paid API",
    "quora.com": "Quora: login wall", "youtube.com": "YouTube: video", "tiktok.com": "TikTok: video",
    "linkedin.com": "LinkedIn: login required", "pinterest.com": "Pinterest",
}

# (platform, regex on the page HTML). First match wins; order matters.
SIGNATURES = [
    ("discourse", r'<meta name="generator" content="Discourse|data-discourse-|discourse-cdn'),
    ("xenforo2", r'<html[^>]+id="XF"|data-xf-init|/js/xf/'),
    ("xenforo1", r'<html[^>]+id="XenForo"|XenForo\.activate|xenforo\.com/'),
    ("vanilla", r'<meta name="generator" content="Vanilla|vanillaforums|"Vanilla"'),
    ("khoros", r'lia-component|lithium|khoros'),
    ("insided", r'insided|data-props="[^"]*repliesProps'),
    ("mediawiki", r'<meta name="generator" content="MediaWiki'),
    ("vbulletin", r'vBulletin|vb_meta|vbulletin'),
    ("phpbb", r'phpBB|viewtopic\.php|viewforum\.php'),
    ("invision", r'ipsLayout|Invision Community|invisioncommunity'),
    ("flarum", r'flarum'),
    ("nodebb", r'<meta name="generator" content="NodeBB|nodebb'),
    ("stackexchange", r'stackexchange\.com|stackoverflow\.com'),
]
HAS_PARSER = {"discourse", "xenforo1", "xenforo2", "vanilla", "insided", "mediawiki", "stackexchange"}
AI_BOTS = ("GPTBot", "ClaudeBot", "anthropic-ai", "CCBot", "Google-Extended", "PerplexityBot")


def queries(topic: str, extra: list[str] | None = None) -> list[str]:
    return [t.format(t=topic) for t in QUERY_TEMPLATES] + list(extra or [])


def group_hits(hits: list[dict], pack: Pack | None = None) -> list[dict]:
    """Group search hits by site; most forum-like first."""
    sites: dict[str, dict] = defaultdict(lambda: {"hits": 0, "discussions": 0, "urls": [], "forum": None})
    for h in hits:
        site = host(h["url"])
        if not site:
            continue
        s = sites[site]
        s["hits"] += 1
        s["discussions"] += h.get("kind") == "discussion"
        s["forum"] = s["forum"] or h.get("forum")
        if h["url"] not in s["urls"]:
            s["urls"].append(h["url"])
    out = []
    for site, s in sites.items():
        known = next((v for k, v in KNOWN.items() if site == k or site.endswith("." + k)), None)
        covered = owning_source(pack, f"https://{site}/") if pack else None
        out.append({"site": site, **s, "known": known, "covered_by": covered,
                    "score": s["hits"] + 2 * s["discussions"]})
    return sorted(out, key=lambda s: -s["score"])


def detect(page: str) -> str | None:
    for name, sig in SIGNATURES:
        if re.search(sig, page[:300000], re.I):
            return name
    return None


def ai_optouts(robots_txt: str) -> list[str]:
    """AI crawlers that robots.txt shuts out entirely (reported for your information)."""
    bots = {b.lower(): b for b in AI_BOTS}
    out, agents, in_rules = set(), [], False
    for line in robots_txt.splitlines():
        k, _, v = line.split("#")[0].partition(":")
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            if in_rules:                       # a new group starts
                agents, in_rules = [], False
            agents.append(v.lower())
        elif k in ("allow", "disallow"):
            in_rules = True
            if k == "disallow" and v == "/":
                out |= {bots[a] for a in agents if a in bots}
    return sorted(out)


def base_url(platform: str, url: str) -> str:
    p = urllib.parse.urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    if platform in ("xenforo1", "xenforo2"):
        m = re.match(r"(.*?/)(threads|forums|index\.php)", p.path)
        return origin + (m.group(1) if m else "/")
    if platform == "mediawiki":
        return origin
    return origin


def check_site(site: dict, fetcher: Fetcher) -> dict:
    """One polite visit: robots.txt, then one thread page. Never raises."""
    url = site["urls"][0]
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    r = {"robots": "?", "ai_optouts": [], "platform": None, "verdict": "?", "notes": "", "sitemaps": []}
    try:
        txt = fetcher.get_text(origin + "/robots.txt", ttl=86400)
        r["ai_optouts"] = ai_optouts(txt)
        r["sitemaps"] = re.findall(r"(?im)^sitemap:\s*(\S+)", txt)[:3]
    except BlockedError as e:
        r.update(robots="blocked", verdict="blocked", notes=str(e))
        return r
    except FetchError as e:
        if not (e.status and 400 <= e.status < 500):
            r.update(verdict="?", notes=f"robots.txt: {e}")
            return r
        txt = ""
    try:
        page = fetcher.get_text(url, ttl=86400)
    except BlockedError as e:
        msg = str(e)
        r["robots"] = "disallows our agent" if "robots.txt" in msg else "allowed"
        r.update(verdict="blocked", notes=msg[:160])
        return r
    except FetchError as e:
        r.update(robots="allowed", verdict="?", notes=str(e)[:160])
        return r
    r["robots"] = "allowed" if txt else "none (all allowed)"
    r["platform"] = detect(page)
    if re.search(r"(?i)(log ?in|sign ?in) to (view|read|continue)|members only", page[:200000]) and \
            len(re.sub(r"<[^>]+>", "", page)) < 5000:
        r.update(verdict="needs-login", notes="thread page asks for a login")
    else:
        r["verdict"] = "ok"
    r["base_url"] = base_url(r["platform"] or "", url)
    return r


def draft_yaml(site: dict, check: dict, today: str) -> str:
    platform = check["platform"]
    sid = re.sub(r"[^a-z0-9]+", "_", site["site"].split(".")[-2] if site["site"].count(".") else site["site"]).strip("_")
    access = (f"{{checked: {today}, robots: \"{check['robots']}\", method: \"{platform} (scouted)\", "
              f"auth: none, verdict: {check['verdict']}"
              + (f", notes: \"AI crawler opt-outs: {', '.join(check['ai_optouts'])}\"" if check["ai_optouts"] else "")
              + "}")
    opts = {
        "discourse": ["base_url: {b}", 'since: "2024-01-01"',
                      "categories: []   # TODO: pick ids from {b}/categories.json"],
        "xenforo2": ["base_url: {b}", 'since: "2024-01-01"', "max_pages_per_thread: 10",
                     "forums: []   # TODO: {{path: forums/<slug>.<id>/, name: ...}} from the forum index"],
        "xenforo1": ["base_url: {b}", "forum_scan: []   # TODO: {{forum: forums/<slug>.<id>/, max_pages: 50}}"],
        "vanilla": ["base_url: {b}", "category_tiers: []   # TODO: category ids"],
        "insided": ["base_url: {b}", "sitemap: {b}/sitemap.xml", "include_slugs: []   # TODO: regexes on topic slugs"],
        "mediawiki": ["api: {b}/api.php   # check: some wikis use /w/api.php", "page_url: {b}/wiki/{{title}}"],
        "stackexchange": ["site: TODO   # API site name, e.g. 'medicalsciences'"],
    }[platform]
    b = check.get("base_url", f"https://{site['site']}")
    lines = [f"  - id: {sid}", f"    parser: {platform}", "    source_type: forum", "    trust: 0.6",
             f"    access: {access}", "    options:"] + [f"      {o.format(b=b.rstrip('/') if platform != 'xenforo2' else b)}" for o in opts]
    return "\n".join(lines)


def run(topic: str, pack: Pack | None = None, client=None, fetcher: Fetcher | None = None,
        max_sites: int = 12, extra_queries: list[str] | None = None, count: int = 20, log=print) -> dict:
    from engine.websearch import backend
    client = client or backend("brave")
    fetcher = fetcher or Fetcher(CRAWLER["user_agent"], DATA_DIR / "cache" / "scout",
                                 min_interval=CRAWLER["min_interval_seconds"], timeout=20, max_retries=1)
    hits = []
    for q in queries(topic, extra_queries):
        log(f"searching: {q}")
        hits += client.search(q, count=count)
    sites = group_hits(hits, pack)
    checked = []
    for s in sites:
        if s["known"] or s["covered_by"]:
            continue
        if len(checked) >= max_sites:
            break
        log(f"checking: {s['site']}")
        s["check"] = check_site(s, fetcher)
        checked.append(s)
    today = time.strftime("%Y-%m-%d")
    drafts = [draft_yaml(s, s["check"], today) for s in checked
              if s["check"]["platform"] in HAS_PARSER and s["check"]["verdict"] == "ok"]
    return {"topic": topic, "queries": len(queries(topic, extra_queries)), "sites": sites,
            "checked": checked, "drafts": drafts}


def report(res: dict) -> str:
    lines = [f"# Scout: {res['topic']}", "",
             "| site | hits | software | robots | verdict | notes |", "|---|---|---|---|---|---|"]
    for s in res["checked"]:
        c = s["check"]
        sw = c["platform"] or ("PDF" if s["urls"][0].lower().endswith(".pdf") else "no forum found")
        sw += "" if c["platform"] in HAS_PARSER or not c["platform"] else " (no parser yet)"
        notes = "; ".join(filter(None, [c["notes"], f"AI opt-outs: {', '.join(c['ai_optouts'])}"
                                        if c["ai_optouts"] else "", f"forum: {s['forum']}" if s["forum"] else ""]))
        hits = f"{s['hits']} ({s['discussions']} threads)" if s["discussions"] else str(s["hits"])
        verdict = c["verdict"] if c["platform"] or c["verdict"] != "ok" else "n/a"
        lines.append(f"| {s['site']} | {hits} | {sw} | {c['robots']} | {verdict} | {notes} |")
    other = [s for s in res["sites"] if s["known"] or s["covered_by"]]
    if other:
        lines += ["", "Not visited: " + "; ".join(
            f"{s['site']} ({s['known'] or 'already a source: ' + s['covered_by']}, {s['hits']} hits)" for s in other)]
    rest = [s for s in res["sites"] if not (s["known"] or s["covered_by"]) and "check" not in s]
    if rest:
        lines += ["", f"Also mentioned ({len(rest)}): " + ", ".join(s["site"] for s in rest[:25])]
    for s in res["checked"][:6]:
        lines += [f"", f"{s['site']} examples: " + "  ".join(s["urls"][:2])]
    if res["drafts"]:
        lines += ["", "## Draft sources.yaml entries (review, pick boards, then `crawl --sample 50`)", "",
                  "```yaml", *res["drafts"], "```"]
    else:
        lines += ["", "No site with a supported forum engine passed the access check."]
    return "\n".join(lines)
