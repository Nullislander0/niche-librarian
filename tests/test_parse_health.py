"""Crawl reports flag parsers that quietly stop extracting content."""
from engine.indexer import ParseHealth
from engine.parsers.base import Document


def doc(i, title="Thread", content="real text here", author="a", root=True, when=1):
    return Document(doc_id=f"s:{i}", source="s", source_type="forum", kind="question", url="u", title=title,
                    content_md=content, author=author, post_date=when, parent_id=None if root else "s:0")


def test_healthy_crawl_has_no_warnings():
    h = ParseHealth("forum")
    for i in range(20):
        h.add(doc(i, title=f"Thread {i}"))
    assert h.warnings() == []


def test_banner_titles_empty_bodies_and_missing_authors_are_flagged():
    h = ParseHealth("forum")
    for i in range(12):
        h.add(doc(i, title="Announcement", content="" if i % 2 else "ok text", author=None))
    w = " | ".join(h.warnings())
    assert "share the title 'Announcement'" in w and "have no content" in w and "have no author" in w


def test_small_samples_and_data_sources_stay_quiet():
    h = ParseHealth("forum")
    for i in range(5):
        h.add(doc(i, content=""))
    assert h.warnings() == []
    d = ParseHealth("data")
    for i in range(12):
        d.add(doc(i, title=f"T{i}", author=None))
    assert d.warnings() == []
