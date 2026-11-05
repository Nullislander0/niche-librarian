"""Scout: grouping search hits by site, identifying forum software, robots AI opt-outs."""
from engine import scout
from engine.config import load_pack
from engine.crawl.fetcher import BlockedError


def hit(url, kind="discussion", forum=None):
    return {"url": url, "kind": kind, "title": "t", "snippet": "s", "forum": forum}


def test_group_hits_ranks_forums_and_skips_known_platforms():
    pack = load_pack("esri_enterprise")
    sites = scout.group_hits([
        hit("https://forum.example.org/t/a/1", forum="Example Forum"), hit("https://forum.example.org/t/b/2"),
        hit("https://news.example.com/x", kind="web"), hit("https://www.reddit.com/r/x/comments/1"),
        hit("https://gis.stackexchange.com/questions/1")], pack)
    assert sites[0]["site"] == "forum.example.org" and sites[0]["forum"] == "Example Forum"
    by = {s["site"]: s for s in sites}
    assert by["reddit.com"]["known"].startswith("Reddit") and by["gis.stackexchange.com"]["covered_by"] == "gis_se"


def test_detect_platforms():
    assert scout.detect('<meta name="generator" content="Discourse 3.2">') == "discourse"
    assert scout.detect('<html id="XF" data-app="public">') == "xenforo2"
    assert scout.detect('<a href="./viewtopic.php?t=5">') == "phpbb"
    assert scout.detect("<p>plain site</p>") is None


def test_ai_optouts():
    txt = "User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin\n"
    assert scout.ai_optouts(txt) == ["CCBot", "GPTBot"]
    assert scout.ai_optouts("User-agent: *\nDisallow: /\n") == []


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url, params=None, ttl=None):
        p = self.pages.get(url, "")
        if isinstance(p, Exception):
            raise p
        return p


def test_run_drafts_entries_for_supported_forums():
    class Client:
        def search(self, q, count=20):
            return [hit("https://forum.example.org/t/topic/1"), hit("https://board.example.net/viewtopic.php?t=9"),
                    hit("https://blocked.example.com/threads/x.1/")]
    page = '<meta name="generator" content="Discourse 3.2">' + "<p>post</p>" * 2000
    f = FakeFetcher({"https://forum.example.org/robots.txt": "User-agent: *\nDisallow: /admin\n",
                     "https://forum.example.org/t/topic/1": page,
                     "https://board.example.net/robots.txt": "",
                     "https://board.example.net/viewtopic.php?t=9": "phpBB" + "<p>x</p>" * 2000,
                     "https://blocked.example.com/robots.txt": "",
                     "https://blocked.example.com/threads/x.1/": BlockedError("challenge/CAPTCHA page served", 200)})
    res = scout.run("widgets", client=Client(), fetcher=f, log=lambda m: None)
    by = {s["site"]: s["check"] for s in res["checked"]}
    assert by["forum.example.org"]["platform"] == "discourse" and by["forum.example.org"]["verdict"] == "ok"
    assert by["board.example.net"]["platform"] == "phpbb"
    assert by["blocked.example.com"]["verdict"] == "blocked"
    assert len(res["drafts"]) == 1 and "parser: discourse" in res["drafts"][0]
    assert "base_url: https://forum.example.org" in res["drafts"][0]
    assert "(no parser yet)" in scout.report(res)
