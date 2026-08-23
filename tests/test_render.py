"""Markdown reaches the reader rendered, not as its source.

Everything the model writes is markdown, and it arrives a token at a time.
The buffering exists so a block is only rendered once it is whole — a half
a fenced code block is not markdown, it is a stray backtick.
"""

from rich.console import Console

from maajun.render import MarkdownStream, render


def stream_in_chunks(text: str, size: int = 5) -> list[str]:
    """Feed text through the stream the way tokens actually arrive."""
    stream = MarkdownStream()
    blocks = []
    for i in range(0, len(text), size):
        blocks.extend(stream.write(text[i : i + size]))
    blocks.extend(stream.close())
    return blocks


def rendered(text: str, width: int = 70) -> str:
    console = Console(width=width, force_terminal=False)
    with console.capture() as capture:
        for block in stream_in_chunks(text):
            render(console, block)
    return capture.get()


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


def test_a_blank_line_ends_a_block():
    assert stream_in_chunks("one\n\ntwo\n") == ["one\n\n", "two\n"]


def test_a_block_is_held_back_until_it_is_whole():
    """Rendering half a paragraph would re-wrap it at the wrong width."""
    stream = MarkdownStream()
    assert stream.write("a paragraph still being") == []
    assert stream.write(" written\n\n") == ["a paragraph still being written\n\n"]


def test_a_fenced_block_survives_the_blank_lines_inside_it():
    """A blank line inside code is code, not the end of the block."""
    text = "```python\nif x:\n\n    pass\n```\n\nafter\n"
    blocks = stream_in_chunks(text)
    assert blocks[0] == "```python\nif x:\n\n    pass\n```\n"
    assert blocks[1] == "after\n"


def test_an_unclosed_fence_is_still_released_at_the_end():
    """A truncated reply must not vanish into the buffer."""
    assert stream_in_chunks("```python\nx = 1\n") == ["```python\nx = 1\n"]


def test_the_last_block_has_no_blank_line_to_end_on():
    assert stream_in_chunks("only a sentence") == ["only a sentence"]


def test_nothing_is_emitted_for_whitespace():
    assert stream_in_chunks("\n\n   \n\n") == []


def test_close_empties_the_buffer():
    stream = MarkdownStream()
    stream.write("tail")
    assert stream.close() == ["tail"]
    assert stream.close() == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_bold_is_shown_bold_not_as_asterisks():
    out = rendered("Use **Read** for that.\n")
    assert "**" not in out
    assert "Read" in out


def test_headings_lose_their_hashes():
    out = rendered("## Root cause\n\nSomething.\n")
    assert "##" not in out
    assert "Root cause" in out


def test_a_bullet_list_is_shown_as_bullets():
    out = rendered("- first\n- second\n")
    assert "•" in out
    assert "- first" not in out


def test_inline_code_keeps_its_text_and_drops_the_backticks():
    out = rendered("Run `maajun watch` now.\n")
    assert "`" not in out
    assert "maajun watch" in out


def test_a_fenced_block_keeps_its_code_and_drops_the_fence():
    out = rendered("```python\nx = 1\n```\n")
    assert "```" not in out
    assert "x = 1" in out


def test_an_h1_is_not_centred():
    """Rich centres h1 by default, which reads as a banner, not an answer."""
    out = rendered("# A finding\n", width=70)
    assert out.splitlines()[0].startswith("A finding")


def test_text_that_looks_like_rich_markup_is_shown_not_swallowed():
    """Markdown parses its own source, so a [bracketed] word is just a word.

    The old path printed raw and had to pass highlight=False to survive this.
    """
    out = rendered("Set it to [default] or [none].\n")
    assert "[default]" in out
    assert "[none]" in out


def test_a_whole_reply_renders_in_order():
    reply = (
        "Two things.\n\n"
        "## Root cause\n\n"
        "`totals.py:88` reads it directly:\n\n"
        "```python\nd = cart['discount']\n```\n\n"
        "- check `summary.py`\n- check `invoice.py`\n"
    )
    out = rendered(reply)
    positions = [out.index(fragment) for fragment in
                 ("Two things", "Root cause", "totals.py:88", "cart['discount']", "summary.py")]
    assert positions == sorted(positions)
    for source in ("##", "```", "- check"):
        assert source not in out
