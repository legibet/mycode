"""Markdown rendering and block-wise streaming for the terminal CLI.

``MarkdownStream`` buffers streamed markdown and hands out chunks that are safe
to commit to terminal scrollback, keeping only a short unfinished tail for
redrawing. Nothing here touches the terminal.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from io import StringIO
from typing import Any, ClassVar, override

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import CodeBlock as _RichCodeBlock
from rich.markdown import Heading as _RichHeading
from rich.markdown import Markdown, TableElement
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table

from .theme import CODE_THEME


class _LeftHeading(_RichHeading):
    """Left-aligned heading separated from other blocks by the usual single blank line."""

    @override
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text
        text.justify = "left"
        yield text


class _CleanCodeBlock(_RichCodeBlock):
    """Code block highlighted in ANSI colors on the terminal background."""

    @override
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(
            code,
            self.lexer_name,
            theme=CODE_THEME,
            word_wrap=True,
            padding=0,
            background_color="default",
        )


class _EdgelessTable(TableElement):
    """Table without the blank top and bottom edge rows, so it is spaced like other blocks."""

    @override
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        table = next(iter(super().__rich_console__(console, options)))
        assert isinstance(table, Table)
        yield from _render_trimmed(table, console, options)


def _render_trimmed(renderable: RenderableType, console: Console, options: ConsoleOptions) -> RenderResult:
    """Render ``renderable`` line by line without blank lines at its top and bottom."""

    lines = console.render_lines(renderable, options, pad=False)
    filled = [index for index, line in enumerate(lines) if any(segment.text.strip() for segment in line)]
    if not filled:
        return
    for line in lines[filled[0] : filled[-1] + 1]:
        yield from line
        yield Segment.line()


class _LeftMarkdown(Markdown):
    """Markdown subclass with left-aligned headings and clean code blocks."""

    elements: ClassVar[dict[str, type[Any]]] = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
        "fence": _CleanCodeBlock,
        "code_block": _CleanCodeBlock,
        "table_open": _EdgelessTable,
    }


class MarkdownBlock:
    """Render one markdown chunk with left-aligned headings and clean code blocks,
    with leading and trailing blank lines removed."""

    def __init__(self, source: str) -> None:
        self._markdown = _LeftMarkdown(source)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from _render_trimmed(self._markdown, console, options)


_MEASURE_CONSOLE = Console(file=StringIO(), color_system=None, force_terminal=False, highlight=False)


def _plain_lines(source: str, width: int) -> list[str]:
    options = _MEASURE_CONSOLE.options.update_width(width)
    lines = _MEASURE_CONSOLE.render_lines(MarkdownBlock(source), options, pad=False)
    return ["".join(segment.text for segment in line) for line in lines]


def markdown_height(source: str, width: int) -> int:
    """Return the number of lines ``MarkdownBlock(source)`` occupies at ``width``."""

    return len(_plain_lines(source, width))


@dataclass(frozen=True)
class Chunk:
    """A piece of markdown that is final and can be printed to scrollback."""

    source: str  # markdown source to render standalone
    continues: bool  # True when this chunk continues the previous chunk's block (no blank line before it)


_FENCE_OPEN = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})\s*")
_LIST_MARKER = re.compile(r" {0,3}([-*+]|\d{1,9}[.)])(\s|$)")
# A partial last line that may still turn into a list marker.
_LIST_MARKER_PREFIX = re.compile(r" {0,3}([-*+]|\d{1,9}[.)]?)")
# A line that, placed first in the tail, would lose its meaning as a setext underline.
_SETEXT_UNDERLINE = re.compile(r" {0,3}(=+|-+)\s*")
# Text that starts a block construct when it begins a line; never start a word split with it.
_BLOCK_START = re.compile(r"[-*+>#|`~]|\d{1,9}[.)]")


def _fence_owners(lines: Sequence[str]) -> tuple[list[int | None], int | None]:
    """Map each line to the index of the fence opener it belongs to (opener and closer included).

    Also return the opener index of a fence that is still open after the last line.
    """

    owners: list[int | None] = []
    start: int | None = None
    marker = ""
    for index, line in enumerate(lines):
        if start is None:
            match = _FENCE_OPEN.fullmatch(line)
            if match and not (match[2][0] == "`" and "`" in match[3]):
                start, marker = index, match[2]
            owners.append(start)
            continue
        owners.append(start)
        match = _FENCE_CLOSE.fullmatch(line)
        if match and match[1][0] == marker[0] and len(match[1]) >= len(marker):
            start = None
    return owners, start


def _is_list_line(line: str, owner: int | None) -> bool:
    return owner is None and _LIST_MARKER.match(line) is not None


def _block_boundaries(lines: Sequence[str], owners: Sequence[int | None], list_carry: bool) -> list[int]:
    """Return indexes of lines that start a new top-level block after a blank line."""

    boundaries: list[int] = []
    in_list = list_carry or _is_list_line(lines[0], owners[0])
    last = len(lines) - 1
    for index in range(1, len(lines)):
        line = lines[index]
        after_blank = not lines[index - 1].strip() and owners[index - 1] is None
        if after_blank and line.strip() and not line[0].isspace():
            marker = _is_list_line(line, owners[index])
            undecided = index == last and _LIST_MARKER_PREFIX.fullmatch(line) is not None
            if not (in_list and (marker or undecided)):
                boundaries.append(index)
                in_list = False
        in_list = in_list or _is_list_line(line, owners[index])
    return boundaries


def _trimmed(lines: Sequence[str]) -> str:
    """Join lines, dropping blank lines at both ends."""

    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _word_breaks(text: str, start: int) -> list[tuple[int, int]]:
    """Return ``(cut, resume)`` positions where ``text[start:]`` may break: runs of spaces, or next to
    a wide character (CJK text wraps between any two characters)."""

    breaks: list[tuple[int, int]] = []
    index = start + 1
    while index < len(text):
        if text[index] == " ":
            end = index
            while end < len(text) and text[end] == " ":
                end += 1
            if end < len(text):
                breaks.append((index, end))
            index = end + 1
        else:
            if text[index - 1] != " " and (cell_len(text[index - 1]) == 2 or cell_len(text[index]) == 2):
                breaks.append((index, index))
            index += 1
    return breaks


class MarkdownStream:
    """Split streamed markdown into committed chunks and a short unfinished tail."""

    def __init__(self) -> None:
        self._pending = ""
        self._continues = False
        # The pending source continues a list, so a blank line followed by a marker is not a boundary.
        self._list_carry = False

    def feed(self, delta: str) -> None:
        """Append streamed text. Never renders."""

        self._pending += delta

    @property
    def tail(self) -> str:
        """Uncommitted source; ``""`` when blank."""

        return self._pending if self._pending.strip() else ""

    @property
    def tail_continues(self) -> bool:
        """Whether the tail continues the last committed chunk's block."""

        return self._continues

    def commit(self, *, width: int, height: int) -> list[Chunk]:
        """Return the chunks that must leave the tail for it to render within ``height`` lines.

        The tail is kept as full as possible so the redrawn area holds a steady
        height. Complete blocks leave first; only an oversized block is split.
        """

        if not self._pending.strip() or markdown_height(self._pending, width) <= height:
            return []
        chunks = self._commit_blocks(width, height)
        if self._pending.strip() and markdown_height(self._pending, width) > height:
            chunks += self._commit_overflow(width, height)
        return chunks

    def flush(self) -> list[Chunk]:
        """End of stream: return everything left and clear the tail."""

        source = _trimmed(self._pending.split("\n"))
        chunks = [Chunk(source, self._continues)] if source else []
        self._pending = ""
        self._continues = False
        self._list_carry = False
        return chunks

    def _commit_blocks(self, width: int, height: int) -> list[Chunk]:
        """Commit complete top-level blocks from the front until the rest fits ``height``."""

        lines = self._pending.split("\n")
        owners, _ = _fence_owners(lines)
        chunks: list[Chunk] = []
        start = 0
        for boundary in _block_boundaries(lines, owners, self._list_carry):
            source = _trimmed(lines[start:boundary])
            if source:
                chunks.append(Chunk(source, self._continues))
            self._continues = False
            start = boundary
            if markdown_height("\n".join(lines[start:]), width) <= height:
                break
        if start:
            self._pending = "\n".join(lines[start:])
            self._list_carry = False
        return chunks

    def _commit_overflow(self, width: int, height: int) -> list[Chunk]:
        """Move leading source into continuing chunks until the tail fits ``height``."""

        lines = self._pending.split("\n")
        owners, open_fence = _fence_owners(lines)
        first = next(index for index, line in enumerate(lines) if line.strip())
        # A fence heading the tail (closed, arrived whole) or one still streaming anywhere in it.
        opener = open_fence if owners[first] is None else owners[first]
        if opener is not None:
            return self._split_fence(lines, owners, opener, width, height)
        if lines[first].lstrip().startswith("|"):
            return []  # tables are never split; the Terminal shows the last lines
        return self._split_lines(lines, owners, width, height)

    def _emit(self, source: str, rest: str, *, is_list: bool) -> Chunk:
        chunk = Chunk(source, self._continues)
        self._pending = rest
        self._continues = True
        self._list_carry = self._list_carry or is_list
        return chunk

    def _split_fence(
        self, lines: list[str], owners: list[int | None], opener: int, width: int, height: int
    ) -> list[Chunk]:
        """Split a code fence; the tail keeps a copy of the opening fence line."""

        fence = lines[opener]
        match = _FENCE_OPEN.fullmatch(fence)
        assert match is not None
        closer = match[1] + match[2]
        # k is the first body line kept in the tail. The fence's last line (its closer, or a line
        # still streaming) always stays, and a blank line at a split edge would be trimmed by
        # rendering, so only split between two non-blank lines.
        last = max(index for index, owner in enumerate(owners) if owner == opener)
        keeps = [k for k in range(opener + 2, last) if lines[k - 1].strip() and lines[k].strip()]
        if not keeps:
            return []
        # The tail height shrinks as k grows, so bisect for the first k that fits.
        first = bisect_left(keeps, True, key=lambda k: markdown_height("\n".join([fence, *lines[k:]]), width) <= height)
        k = keeps[min(first, len(keeps) - 1)]
        source = "\n".join([*lines[:k], closer])
        is_list = any(_is_list_line(line, None) for line in lines[:opener])
        return [self._emit(source, "\n".join([fence, *lines[k:]]), is_list=is_list)]

    def _split_lines(self, lines: list[str], owners: list[int | None], width: int, height: int) -> list[Chunk]:
        """Split a paragraph, list, or quote at a source line, falling back to a word split."""

        starts = [
            index
            for index in range(1, len(lines))
            if lines[index].strip() and owners[index] is None and not _SETEXT_UNDERLINE.fullmatch(lines[index])
        ]
        first = bisect_left(starts, True, key=lambda s: markdown_height("\n".join(lines[s:]), width) <= height)
        if first < len(starts):
            split = next((s for s in starts[first:] if not lines[s][0].isspace()), starts[first])
            return [self._split_at_line(lines, owners, split)]
        chunks = [self._split_at_line(lines, owners, starts[-1])] if starts else []
        return chunks + self._split_words(width, height)

    def _split_at_line(self, lines: list[str], owners: list[int | None], split: int) -> Chunk:
        is_list = any(_is_list_line(line, owner) for line, owner in zip(lines[:split], owners, strict=False))
        return self._emit(_trimmed(lines[:split]), "\n".join(lines[split:]), is_list=is_list)

    def _split_words(self, width: int, height: int) -> list[Chunk]:
        """Split the last source line between words, preferring a real wrap point so nothing reflows."""

        text = self._pending
        line_start = text.rstrip().rfind("\n") + 1
        breaks = [(cut, resume) for cut, resume in _word_breaks(text, line_start) if text[:cut].strip()]
        breaks = [(cut, resume) for cut, resume in breaks if not _BLOCK_START.match(text, resume)]
        first = bisect_left(breaks, True, key=lambda b: markdown_height(text[b[1] :], width) <= height)
        if first == len(breaks):
            return []
        whole = _plain_lines(text, width)
        near = [b for b in breaks[first:] if b[0] - breaks[first][0] <= 2 * width]
        cut, resume = next(
            (b for b in near if _plain_lines(text[: b[0]], width) + _plain_lines(text[b[1] :], width) == whole),
            breaks[first],
        )
        is_list = _is_list_line(text[line_start:], None)
        return [self._emit(text[:cut], text[resume:], is_list=is_list)]
