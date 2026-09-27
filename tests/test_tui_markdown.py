import random
from io import StringIO

import pytest
from rich.console import Console

from mycode_cli.tui.markdown import Chunk, MarkdownBlock, MarkdownStream, markdown_height

WIDTH = 40
HEIGHT = 8

_WORDS = ["the", "quick", "brown", "fox", "jumps", "over", "a", "lazy", "dog", "while", "streaming", "keeps"]


def _prose(words: int, seed: int) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(_WORDS) for _ in range(words)) + "."


def _code(lines: int) -> str:
    body = [f"    value_{i} = compute({i}, 'x' * {i % 7})" if i % 9 else "" for i in range(lines)]
    return "```python\ndef main():\n" + "\n".join(body) + "\n"


STRUCTURAL = {
    "headings_and_paragraphs": "# Title\n\nIntro paragraph.\n\n## Section\n\nBody text here.\n\n### Small\n\nMore.\n",
    "multi_screen_prose": "\n\n".join(_prose(30, seed) for seed in range(8)) + "\n",
    "code_fence": "Before.\n\n```python\ndef f():\n\n    return 1\n```\n\nAfter.\n",
    "quotes": "> first quote\n> continues\n\n> second quote\n\nText after.\n",
    "table_with_long_cells": (
        "Intro.\n\n| name | notes |\n| --- | --- |\n| a | short |\n"
        + "".join(f"| row{i} | {_prose(12, i)} |\n" for i in range(4))
        + "\nAfter table.\n"
    ),
    "tight_list": "Items:\n\n- one\n- two\n- three\n\nDone.\n",
    "loose_list": "- one\n\n- two\n\n- three\n\nParagraph.\n\n1. first\n\n2. second\n",
    "nested_list": "- parent\n  - child\n\n  - child two\n\n- sibling\n\n  continuation\n\nEnd.\n",
    "no_trailing_newline": "First paragraph.\n\nSecond paragraph without newline",
    "wide_characters": "# 标题 🎉\n\n这是一段中文内容，包含表情符号 😀 和混合 English words。\n\n- 项目一 ✅\n- 项目二 🚀\n",
    "empty": "",
    "whitespace_only": "  \n\n   \n",
}

OVERFLOW = {
    "long_paragraph": _prose(420, 1),
    "long_cjk_paragraph": "流式渲染的中文段落没有空格，" * 40,
    "long_loose_list": "\n\n".join(f"- item {i}: {_prose(6, i)}" for i in range(30)) + "\n",
    "long_prose_lines": "\n".join(_prose(8, i) for i in range(40)) + "\n",
}


def _stream(doc: str, seed: int = 0, max_step: int = 7) -> list[Chunk]:
    rng = random.Random(seed)
    stream = MarkdownStream()
    chunks: list[Chunk] = []
    pos = 0
    while pos < len(doc):
        step = rng.randint(1, max_step)
        stream.feed(doc[pos : pos + step])
        pos += step
        chunks += stream.commit(width=WIDTH, height=HEIGHT)
        tail = stream.tail
        if not tail.lstrip().startswith("|"):
            assert markdown_height(tail, WIDTH) <= HEIGHT, tail
    chunks += stream.flush()
    assert stream.tail == ""
    return chunks


def _console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, force_terminal=False, color_system=None, width=WIDTH), out


def _print_chunks(chunks: list[Chunk]) -> str:
    console, out = _console()
    for index, chunk in enumerate(chunks):
        if index and not chunk.continues:
            console.print()
        console.print(MarkdownBlock(chunk.source))
    return out.getvalue()


def _print_whole(doc: str) -> str:
    console, out = _console()
    if doc.strip():
        console.print(MarkdownBlock(doc))
    return out.getvalue()


def _assert_sources_in_order(doc: str, chunks: list[Chunk]) -> None:
    pos = 0
    for chunk in chunks:
        at = doc.index(chunk.source, pos)
        assert not doc[pos:at].strip(), doc[pos:at]
        pos = at + len(chunk.source)
    assert not doc[pos:].strip()


@pytest.mark.parametrize("doc", STRUCTURAL.values(), ids=STRUCTURAL.keys())
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_structural_chunks_render_like_whole_document(doc: str, seed: int) -> None:
    chunks = _stream(doc, seed)

    _assert_sources_in_order(doc, chunks)
    joined = "\n\n".join(chunk.source for chunk in chunks)
    assert [line for line in joined.split("\n") if line.strip()] == [line for line in doc.split("\n") if line.strip()]
    assert _print_chunks(chunks) == _print_whole(doc)


def test_blocks_leave_the_tail_only_when_it_would_overflow() -> None:
    stream = MarkdownStream()
    stream.feed("First.\n\nSecond.\n\nThird")

    assert stream.commit(width=WIDTH, height=HEIGHT) == []
    assert stream.commit(width=WIDTH, height=3) == [Chunk("First.", False)]
    assert stream.tail == "Second.\n\nThird"


def test_structural_chunks_commit_before_the_end() -> None:
    chunks = _stream(STRUCTURAL["multi_screen_prose"])

    assert len(chunks) > 1
    assert not any(chunk.continues for chunk in chunks)


@pytest.mark.parametrize("doc", OVERFLOW.values(), ids=OVERFLOW.keys())
def test_overflow_keeps_tail_short_without_losing_text(doc: str) -> None:
    chunks = _stream(doc)

    assert len(chunks) > 3
    assert all(chunk.continues for chunk in chunks[1:])
    _assert_sources_in_order(doc, chunks)


@pytest.mark.parametrize("name", ["long_paragraph", "long_cjk_paragraph", "long_loose_list"])
def test_overflow_splits_at_wrap_points(name: str) -> None:
    doc = OVERFLOW[name]

    assert _print_chunks(_stream(doc)) == _print_whole(doc)


def test_unclosed_code_fence_splits_with_reopened_fence() -> None:
    doc = _code(300)
    chunks = _stream(doc, max_step=40)  # larger deltas keep the 300-line fixture fast

    assert len(chunks) > 10
    assert all(chunk.source.startswith("```python\n") for chunk in chunks)
    assert all(chunk.source.endswith("\n```") for chunk in chunks[:-1])
    assert _print_chunks(chunks) == _print_whole(doc)


def test_closed_fence_arriving_whole_still_splits() -> None:
    doc = "```python\n" + "\n".join(f"line{i} = {i}" for i in range(24)) + "\n```\n"
    stream = MarkdownStream()
    stream.feed(doc)

    chunks = stream.commit(width=WIDTH, height=HEIGHT)

    assert len(chunks) == 1
    assert markdown_height(stream.tail, WIDTH) <= HEIGHT
    assert _print_chunks(chunks + stream.flush()) == _print_whole(doc)


def test_fence_split_never_drops_blank_code_lines() -> None:
    # Every split point touches a blank line, so the block is not split at all rather than trimmed.
    doc = "```python\n" + "\n\n".join(f"x{i} = {i}" for i in range(24)) + "\n```\n"
    stream = MarkdownStream()
    chunks: list[Chunk] = []
    for line in doc.splitlines(keepends=True):
        stream.feed(line)
        chunks += stream.commit(width=WIDTH, height=HEIGHT)
    chunks += stream.flush()

    assert _print_chunks(chunks) == _print_whole(doc)


def test_fence_split_after_structural_blocks_keeps_order() -> None:
    doc = "Intro.\n\n" + _code(40) + "```\n\nAfter.\n"
    chunks = _stream(doc)

    assert chunks[0] == Chunk("Intro.", False)
    assert chunks[-1] == Chunk("After.", False)
    assert _print_chunks(chunks) == _print_whole(doc)


def test_short_content_stays_in_tail_until_flush() -> None:
    stream = MarkdownStream()
    for word in ["Hello", " there", ", partial", " line"]:
        stream.feed(word)
        assert stream.commit(width=WIDTH, height=HEIGHT) == []
        assert not stream.tail_continues

    assert stream.tail == "Hello there, partial line"
    assert stream.flush() == [Chunk("Hello there, partial line", False)]


def test_blank_line_between_list_items_is_not_a_block_boundary() -> None:
    stream = MarkdownStream()
    stream.feed("- one\n\n- t")

    # Over budget without a boundary: the list is split as a continuation, not as two blocks.
    assert stream.commit(width=WIDTH, height=1) == [Chunk("- one", False)]
    assert stream.tail_continues

    stream.feed("wo\n\nText")
    assert stream.commit(width=WIDTH, height=1) == [Chunk("- two", True)]
    assert stream.tail == "Text"
    assert not stream.tail_continues


def test_markdown_block_strips_blank_edges() -> None:
    assert markdown_height("", WIDTH) == 0
    assert markdown_height("# Title", WIDTH) == 1
    assert markdown_height("- a\n- b", WIDTH) == 2
    assert markdown_height("> quote", WIDTH) == 1


def test_finished_long_line_still_splits() -> None:
    stream = MarkdownStream()
    stream.feed(_prose(200, 3) + "\n")

    assert len(stream.commit(width=WIDTH, height=HEIGHT)) == 1
    assert markdown_height(stream.tail, WIDTH) <= HEIGHT
    assert stream.tail_continues
