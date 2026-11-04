"""XenForo 1 parser against a synthetic forum."""
from engine.config import load_pack
from engine.parsers.base import CrawlContext
from engine.parsers.xenforo1 import Parser, page_count, parse_posts

from test_pipeline import DictState


def post(pid, author, body, likes=0, when='data-time="1700000000"'):
    like = (f'<span class="LikeText"><a class="username">a</a> and <a>{likes} others</a> like this.</span>'
            if likes else "")
    return f"""<li id="post-{pid}" class="message " data-author="{author}">
      <a href="members/{author}.9{pid}/" class="username">{author}</a> <span class="DateTime" {when}>x</span>
      <em class="userTitle" itemprop="title">Pig owner</em>
      <dl class="pairsJustified"><dt>Joined:</dt> <dd>Feb 20, 2006</dd></dl>
      <dl class="pairsJustified"><dt>Oddometer:</dt> <dd><a href="search">1,234</a></dd></dl>
      <blockquote class="messageText ugc baseHtml">{body}<div class="messageTextEndMarker">&nbsp;</div></blockquote>
      {like}</li>"""


def thread_page(title, n, posts):
    nav = f'<span class="pageNavHeader">Page 1 of {n}</span>' if n > 1 else ""
    return f"<html><title>{title} | Forum</title><h1>{title}</h1>{nav}<ol>{''.join(posts)}</ol></html>"


def test_parse_posts_fields():
    page = thread_page("Shock swap", 1, [
        post(1, "rider1", "<p>Installed a Cogent shock, after 8,000 miles it is great.</p>", likes=4),
        post(2, "rider2", '<div class="bbCodeQuote"><blockquote class="quoteContainer">quoted</blockquote></div><p>agree</p>',
             when='title="May 2, 2006 at 2:28 AM"'),
    ])
    ps = parse_posts(page, ("Oddometer",))
    assert [p["id"] for p in ps] == ["1", "2"]
    assert ps[0]["likes"] == 5 and ps[0]["messages"] == 1234 and ps[0]["date"] == 1700000000
    assert ps[0]["user_title"] == "Pig owner" and ps[0]["joined"] is not None
    assert "messageTextEndMarker" not in ps[0]["body_html"]
    assert ps[1]["date"] is not None and "quoted" in ps[1]["body_html"]
    assert page_count(thread_page("x", 37, [])) == 37


class Site:
    """Mega thread of 5 pages, a tag page with two threads, a forum listing."""

    def __init__(self, mega_pages=5):
        self.network_requests = 0
        self.fetched = []
        self.mega_pages = mega_pages

    def get_text(self, url, params=None, ttl=None):
        self.network_requests += 1
        path = url.split("/f/", 1)[1]
        self.fetched.append(path)
        if path.startswith("threads/mega.1/"):
            p = int(path.rsplit("page-", 1)[1].strip("/")) if "page-" in path else 1
            return thread_page("the DR650 thread", self.mega_pages, [post(1000 + p, "m", f"<p>mega page {p}</p>")])
        if path.startswith("tags/dr650"):
            return '<a href="threads/carb-mods.2/">x</a><a href="threads/mega.1/">y</a>'
        if path.startswith("forums/thumpers.15"):
            return ('<h3 class="title"><a href="threads/dr650-gearing.3/" class="x">DR650 gearing</a></h3>'
                    '<h3 class="title"><a href="threads/klr-stuff.4/" class="x">KLR doohickey</a></h3>')
        tid = path.split(".")[1].split("/")[0]
        return thread_page(f"thread {tid}", 1, [post(int(tid) * 10, "r", f"<p>body {tid}</p>")])


def crawl(site, state, per_run=2):
    pack = load_pack("dr650")
    src = pack.source("advrider")
    src.options = {"base_url": "https://forum.example/f/", "post_count_labels": ["Oddometer"],
                   "mega_threads": [{"thread": "threads/mega.1/", "pages_per_run": per_run}],
                   "tag_pages": ["tags/dr650/"],
                   "forum_scan": [{"forum": "forums/thumpers.15/", "title": "(?i)dr\\s?650", "max_pages": 3}]}
    return list(Parser(CrawlContext(source=src, fetcher=site, state=state)).crawl())


def test_mega_thread_newest_first_in_slices_and_threads_found():
    state = DictState()
    docs = crawl(Site(), state)
    mega_posts = [d for d in docs if d.doc_id.startswith("advrider:p:10")]
    assert [d.extra["page"] for d in mega_posts] == [5, 4]          # newest pages first, 2 per run
    assert mega_posts[0].parent_id == "advrider:t:1:p5"            # each page is its own conversation
    titles = {d.title for d in docs if d.kind == "thread"}
    assert "thread 2" in titles and "thread 3" in titles            # tag page + title scan
    assert "thread 4" not in titles                                 # KLR thread filtered out
    assert not any(t.startswith("thread 1") for t in titles)        # mega not crawled as a normal thread
    p = next(d for d in docs if d.kind == "post" and d.title == "thread 2")
    assert p.author_reputation == 1234 and p.score == 0 and p.url.endswith("#post-20")


def test_mega_thread_resumes_downward_then_picks_up_new_pages():
    state = DictState()
    crawl(Site(), state)                                            # pages 5, 4
    docs = crawl(Site(), state)                                     # re-read newest (5), continue at 3
    assert [d.extra["page"] for d in docs if d.doc_id.startswith("advrider:p:10")] == [5, 3]
    docs = crawl(Site(mega_pages=6), state)                         # page 6 appeared
    assert [d.extra["page"] for d in docs if d.doc_id.startswith("advrider:p:10")] == [5, 6]
