"""CLI interface - async streaming"""

import asyncio
import os
import re

from app.agent.core import Agent, get_model_config

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
BLUE, CYAN, GREEN, RED = "\033[34m", "\033[36m", "\033[32m", "\033[31m"


def separator() -> str:
    return f"{DIM}{'─' * min(os.get_terminal_size().columns, 120)}{RESET}"


def render_markdown(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)


async def chat_loop(agent: Agent) -> None:
    """Main async chat loop."""
    while True:
        try:
            print(separator())
            user_input = await asyncio.to_thread(input, f"{BOLD}{BLUE}❯{RESET} ")
            user_input = user_input.strip()

            if not user_input:
                continue
            if user_input in ("/q", "exit", "quit"):
                print(f"{DIM}Goodbye!{RESET}")
                break
            if user_input == "/c":
                agent.clear()
                print(f"{GREEN}✓{RESET} Conversation cleared")
                continue

            print(separator())

            async for event in agent.achat(user_input):
                if event.type == "text":
                    print(render_markdown(event.data["content"]), end="", flush=True)

                elif event.type == "tool_start":
                    name = event.data["name"]
                    args = event.data["args"]
                    preview = str(list(args.values())[0])[:50] if args else ""
                    print(f"\n{DIM}▸{RESET} {CYAN}{name}{RESET} {DIM}{preview}{RESET}")

                elif event.type == "tool_done":
                    result = event.data["result"]
                    lines = result.split("\n")
                    preview = lines[0][:60]
                    if len(lines) > 1:
                        preview += f" {DIM}[+{len(lines) - 1} lines]{RESET}"
                    elif len(lines[0]) > 60:
                        preview += "..."

                    if result.startswith("ok"):
                        print(f"  {GREEN}✓{RESET} {DIM}{preview}{RESET}")
                    elif result.startswith("error"):
                        print(f"  {RED}✗{RESET} {preview}")
                    else:
                        print(f"  {DIM}↳ {preview}{RESET}")

                elif event.type == "error":
                    print(f"\n{RED}✗ Error:{RESET} {event.data['message']}")

            print()

        except KeyboardInterrupt:
            agent.cancel()
            print(f"\n{DIM}Cancelled{RESET}")
        except EOFError:
            print(f"\n{DIM}Goodbye!{RESET}")
            break


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    model, api_base = get_model_config()
    if not model:
        print(f"{RED}Error: No LLM API key found.{RESET}")
        return

    agent = Agent(model=model, cwd=os.getcwd(), api_base=api_base)
    print(f"\n{BOLD}mycode{RESET} | {CYAN}{model}{RESET} | {DIM}{agent.cwd}{RESET}")

    asyncio.run(chat_loop(agent))


if __name__ == "__main__":
    main()
