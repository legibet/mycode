"""Semantic color tokens and UI symbols for the terminal CLI.

All colors use ANSI base-16 names so terminal themes (dark/light) remap them
automatically.  Avoid hardcoded RGB or 256-color values.
"""

import os

from rich.style import Style


def _detect_terminal_theme() -> str:
    """Return 'light' or 'dark' based on terminal background heuristic."""
    # COLORFGBG is set by many terminals: "fg;bg" with ANSI color index
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg:
        try:
            bg = int(colorfgbg.rsplit(";", 1)[-1])
            return "light" if bg >= 7 else "dark"
        except ValueError:
            pass
    # macOS Terminal.app defaults to a light profile
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return "light"
    return "dark"


TERMINAL_THEME = _detect_terminal_theme()
CODE_THEME = "default" if TERMINAL_THEME == "light" else "monokai"

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
