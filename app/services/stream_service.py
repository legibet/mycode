import json
from collections.abc import AsyncIterator, Awaitable, Callable

from app.agent.core import Agent
from app.schemas.chat import StreamEvent


def format_sse(event: StreamEvent) -> str:
    """Format event as SSE payload."""
    data = json.dumps(event.model_dump(exclude_none=True))
    return f"data: {data}\n\n"


async def stream_events(
    agent: Agent,
    message: str,
    on_done: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[str]:
    """Generate SSE events from agent."""
    sent_any = False
    try:
        async for event in agent.achat(message):
            payload = StreamEvent(type=event.type, **event.data)
            yield format_sse(payload)
            sent_any = True
        if not sent_any:
            payload = StreamEvent(
                type="error",
                message="LLM produced no output. Check model or api_base.",
            )
            yield format_sse(payload)
    finally:
        if on_done:
            await on_done()
    yield "data: [DONE]\n\n"
