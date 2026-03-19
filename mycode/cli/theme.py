"""Semantic color tokens and UI symbols for the terminal CLI.

All colors use ANSI base-16 names so terminal themes (dark/light) remap them
automatically.  Avoid hardcoded RGB or 256-color values.
"""

from rich.style import Style

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------
ACCENT = Style(color="blue", bold=True)
MUTED = Style(dim=True)
SUCCESS = Style(color="green")
ERROR = Style(color="red")
WARNING = Style(color="yellow")
TOOL_NAME = Style(color="cyan")
THINKING = Style(dim=True, italic=True)
STATS = Style(dim=True)
PROVIDER = Style(color="cyan")

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
PROMPT_CHAR = "❯"
TOOL_MARKER = "⏺"
TOOL_BORDER = "│"
TOOL_END = "⎿"
ERROR_MARKER = "✕"
