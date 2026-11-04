from engine.index.chunk import chunk_markdown


def test_short_text_is_one_chunk():
    assert chunk_markdown("hello\n\nworld") == ["hello\n\nworld"]


def test_long_text_splits_on_paragraphs_and_keeps_code_whole():
    code = "```\n" + "\n".join(f"line {i}" for i in range(100)) + "\n```"
    md = "\n\n".join(["intro " * 100, code, "outro " * 300])
    chunks = chunk_markdown(md, target=600, hard_max=1200)
    assert len(chunks) > 1
    assert any(c.startswith("```") and c.endswith("```") for c in chunks)
    assert all(len(c) <= 1200 for c in chunks)


def test_empty():
    assert chunk_markdown("") == []
