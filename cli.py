"""CLI for mycode.

This is intentionally small. The canonical implementation is the backend agent loop.
The CLI reuses the same agent + session store.

Usage:

  uv run python cli.py

Environment:
  MODEL     e.g. anthropic:claude-sonnet-4-5
  BASE_URL  optional (OpenAI-compatible base URL)

API keys:
  Any-LLM supports passing api_key directly, but the CLI keeps things minimal and
  relies on provider env vars (ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY).
"""

from __future__ import annotations

import asyncio
import os
import re

from app.agent.core import Agent
from app.config import get_settings
from app.session import SessionStore

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, RED = "\033[34m", "\033[36m", "\033[32m", "\033[31m"


def separator() -> str:
    try:
        width = min(os.get_terminal_size().columns, 120)
    except OSError:
        width = 120
    return f"{DIM}{'─' * width}{RESET}"


def render_markdown(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)


async def chat_loop(agent: Agent, *, store: SessionStore, session_id: str) -> None:
    while True:
        try:
            print(separator())
            user_input = await asyncio.to_thread(input, f"{BOLD}{BLUE}❯{RESET} ")
            user_input = user_input.strip()

            if not user_input:
                continue
            if user_input in ("/q", "exit", "quit"):
                print(f"{DIM}Goodbye!{RESET}")
                return
            if user_input == "/c":
                await store.clear_session(session_id)
                agent.clear()
                print(f"{GREEN}✓{RESET} Conversation cleared")
                continue

            print(separator())

            async def on_persist(message: dict) -> None:
                await store.append_message(session_id, message)

            async for event in agent.achat(user_input, on_persist=on_persist):
                if event.type == "text":
                    print(render_markdown(event.data.get("content", "")), end="", flush=True)
                elif event.type == "tool_start":
                    name = event.data.get("name")
                    args = event.data.get("args") or {}
                    preview = str(list(args.values())[0])[:50] if args else ""
                    print(f"\n{DIM}▸{RESET} {CYAN}{name}{RESET} {DIM}{preview}{RESET}")
                elif event.type == "tool_output":
                    line = event.data.get("content", "")
                    if line:
                        print(f"{DIM}{line}{RESET}")
                elif event.type == "tool_done":
                    result = event.data.get("result", "")
                    first = (result.splitlines() or [""])[0][:80]
                    if result.startswith("ok"):
                        print(f"  {GREEN}✓{RESET} {DIM}{first}{RESET}")
                    elif result.startswith("error"):
                        print(f"  {RED}✗{RESET} {first}")
                    else:
                        print(f"  {DIM}↳ {first}{RESET}")
                elif event.type == "error":
                    print(f"\n{RED}✗ Error:{RESET} {event.data.get('message', '')}")

            print()

        except KeyboardInterrupt:
            agent.cancel()
            print(f"\n{DIM}Cancelled{RESET}")
        except EOFError:
            print(f"\n{DIM}Goodbye!{RESET}")
            return


def main() -> None:
    settings = get_settings()
    model = os.environ.get("MODEL") or settings.default_model or "anthropic:claude-sonnet-4-5"
    api_base = os.environ.get("BASE_URL") or settings.api_base

    cwd = os.getcwd()
    store = SessionStore()
    import hashlib

    # One default session per working directory (pi-style)
    session_id = hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:12]

    data = asyncio.run(store.get_or_create(session_id, model=model, cwd=cwd, api_base=api_base))
    messages = data.get("messages") or []

    agent = Agent(model=model, cwd=cwd, session_dir=store.session_dir(session_id), api_base=api_base, messages=messages)

    print(f"\n{BOLD}mycode{RESET} | {CYAN}{model}{RESET} | {DIM}{cwd}{RESET}")
    asyncio.run(chat_loop(agent, store=store, session_id=session_id))


if __name__ == "__main__":
    main()
