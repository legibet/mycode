"""Semantic color tokens and UI symbols for the terminal CLI.

All colors use ANSI base-16 names so terminal themes (dark/light) remap them
automatically.  Avoid hardcoded RGB or 256-color values.
"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty

from rich.style import Style


def _query_terminal_bg_luminance() -> float | None:
    """Query terminal background color via OSC 11 escape sequence.

    Sends ESC]11;?BEL and reads back rgb:RRRR/GGGG/BBBB.
    Returns perceived luminance in [0, 1], or None if detection fails.
    Works on iTerm2, Kitty, Alacritty, WezTerm, macOS Terminal, and any
    terminal that implements xterm's OSC color query protocol.
    """
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return None

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\033]11;?\007")
        sys.stdout.flush()

        ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        if not ready:
            return None

        buf = ""
        while len(buf) < 64:
            ch = sys.stdin.read(1)
            buf += ch
            if ch == "\007" or buf.endswith("\033\\"):
                break
    except Exception:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    m = re.search(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", buf)
    if not m:
        return None

    def _normalize_hex_component(value: str) -> float:
        # Handles 1-, 2-, or 4-digit hex components
        return int(value, 16) / (16 ** len(value) - 1)

    r, g, b = (
        _normalize_hex_component(m.group(1)),
        _normalize_hex_component(m.group(2)),
        _normalize_hex_component(m.group(3)),
    )
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _detect_terminal_theme() -> str:
    """Return `light` or `dark`, with env override and safe fallback."""

    override = os.environ.get("MYCODE_THEME", "").lower()
    if override in ("light", "dark"):
        return override

    luminance = _query_terminal_bg_luminance()
    if luminance is not None:
        return "light" if luminance > 0.5 else "dark"

    return "dark"


# Detect the terminal theme once at import time so render code can stay cheap
# and deterministic during interactive updates. If probing fails, default to
# the dark palette because it is the safest choice across terminals.
TERMINAL_THEME = _detect_terminal_theme()
# friendly: neutral #f0f0f0 background, dark saturated syntax colors — good on light terminals.
# monokai:  classic dark background, vivid colors — good on dark terminals.
CODE_THEME = "friendly" if TERMINAL_THEME == "light" else "monokai"

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------
ACCENT = Style(color="blue", bold=True)
MUTED = Style(dim=True)
SUCCESS = Style(color="green")
ERROR = Style(color="red")
WARNING = Style(color="yellow")
TOOL_NAME = Style(color="cyan")
THINKING = Style(color="blue", dim=True)
STATS = Style(dim=True)
PROVIDER = Style(color="cyan")

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
PROMPT_CHAR = "❯"
THINKING_SYMBOL = "◇"
TOOL_MARKER = "⏺"
TOOL_BORDER = "│"
TOOL_END = "└"
ERROR_MARKER = "✕"
