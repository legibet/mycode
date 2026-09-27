"""Semantic color tokens and UI symbols for the terminal CLI.

Every color is one of the six ANSI base hues, so the terminal palette picks the
actual shade and one set of styles works on dark and light backgrounds. Colored
text is never bold (many terminals render bold colors in their bright variant),
and bright variants, black, white, and background colors are not used.
"""

from __future__ import annotations

from prompt_toolkit.styles import Style as PromptStyle
from pygments.token import Comment, Error, Generic, Keyword, Name, Number, Operator, String
from rich.style import Style
from rich.syntax import ANSISyntaxTheme
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------
ACCENT = Style(color="blue")  # focus: prompt, selected row, brand
MUTED = Style(dim=True)  # secondary: previews, tool output, stats, hints
THINKING = Style(dim=True, italic=True)
SUCCESS = Style(color="green")
ERROR = Style(color="red")
WARNING = Style(color="yellow")  # needs attention: reviews, retries
TOOL_NAME = Style(bold=True)

# Markdown styles for every console the TUI renders with; these replace rich's
# defaults, which use backgrounds, bright colors, and magenta headings.
MARKDOWN_THEME = Theme(
    {
        "markdown.code": "cyan",
        "markdown.code_block": "none",
        "markdown.block_quote": "dim italic",
        "markdown.list": "none",
        "markdown.item.bullet": "dim",
        "markdown.item.number": "dim",
        "markdown.hr": "dim",
        "markdown.h1": "bold",
        "markdown.h2": "bold",
        "markdown.h3": "bold",
        "markdown.h4": "bold dim",
        "markdown.h5": "bold dim",
        "markdown.h6": "bold dim",
        "markdown.link": "blue",
        "markdown.link_url": "dim underline",
        "markdown.table.border": "dim",
        "markdown.table.header": "bold",
        "markdown.kbd": "bold",
    }
)

# Syntax highlighting for code blocks; tokens not listed use the default color.
CODE_THEME = ANSISyntaxTheme(
    {
        Comment: MUTED,
        Comment.Preproc: Style(color="cyan"),
        Keyword: Style(color="blue"),
        Keyword.Type: Style(color="cyan"),
        Operator.Word: Style(color="magenta"),
        Name.Builtin: Style(color="cyan"),
        Name.Function: Style(color="green"),
        Name.Class: Style(color="green"),
        Name.Namespace: Style(color="cyan"),
        Name.Exception: Style(color="cyan"),
        Name.Decorator: Style(color="magenta"),
        Name.Variable: Style(color="red"),
        Name.Constant: Style(color="red"),
        Name.Attribute: Style(color="cyan"),
        Name.Tag: Style(color="blue"),
        String: Style(color="yellow"),
        Number: Style(color="blue"),
        Generic.Inserted: Style(color="green"),
        Generic.Deleted: Style(color="red"),
        Generic.Heading: Style(bold=True),
        Generic.Subheading: Style(color="magenta"),
        Generic.Prompt: Style(bold=True),
        Generic.Error: Style(color="red"),
        Error: Style(color="red"),
    }
)

# prompt_toolkit styles for the input area; these replace the gray backgrounds
# of its default completion menu.
PROMPT_STYLE = PromptStyle.from_dict(
    {
        "completion-menu": "bg:default fg:default",
        "completion-menu.completion.current": "noreverse bg:default fg:ansiblue",
        "completion-menu.meta.completion": "bg:default fg:default dim",
        "completion-menu.meta.completion.current": "bg:default fg:default dim",
        "scrollbar.background": "bg:default",
        "scrollbar.button": "bg:default reverse",
    }
)

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
PROMPT_CHAR = "❯"
THINKING_SYMBOL = "◇"
TOOL_MARKER = "⏺"
ERROR_MARKER = "✕"
