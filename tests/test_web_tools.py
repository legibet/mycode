"""Behavior tests for the CLI web tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import override

import httpx2
import pytest

from mycode.tools import ToolContext, ToolExecutionResult, ToolExecutor
from mycode_cli.config import WebConfig, WebProviderConfig
from mycode_cli.web_tools import build_web_tools
from mycode_cli.workspace import CliDeps


@pytest.fixture(autouse=True)
def clear_web_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)


def _use_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    async_client = httpx2.AsyncClient
    transport = httpx2.MockTransport(handler)
    monkeypatch.setattr(
        "mycode_cli.web_tools.httpx2.AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )


async def _call(
    tmp_path: Path,
    web: WebConfig,
    name: str,
    args: dict[str, object],
) -> ToolExecutionResult:
    executor = ToolExecutor(build_web_tools(web))
    ctx = ToolContext(
        executor=executor,
        deps=CliDeps(cwd=tmp_path, tool_output_dir=tmp_path / "tool-output"),
        tool_call_id="call-1",
    )
    return await executor.aexecute(name, args, ctx)


@pytest.mark.asyncio
async def test_local_fetch_converts_html_and_rejects_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/image":
            return httpx2.Response(200, headers={"content-type": "image/png"}, content=b"png")
        if request.url.path == "/large":
            return httpx2.Response(
                200,
                headers={"content-type": "text/plain", "content-length": str(5 * 1024 * 1024 + 1)},
            )
        return httpx2.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><body><h1>Guide</h1><script>hidden()</script>"
                '<p>Read <a href="/docs">the docs</a>.</p>'
                '<img src="data:image/png;base64,AAAA"></body></html>'
            ),
        )

    _use_transport(monkeypatch, handler)

    page = await _call(tmp_path, WebConfig(), "webfetch", {"url": "https://example.test/page"})
    image = await _call(tmp_path, WebConfig(), "webfetch", {"url": "https://example.test/image"})
    large = await _call(tmp_path, WebConfig(), "webfetch", {"url": "https://example.test/large"})

    assert page.output == "# Guide\n\nRead [the docs](/docs)."
    assert image == ToolExecutionResult(
        output="error: unsupported content type: image/png",
        is_error=True,
    )
    assert large == ToolExecutionResult(output="error: response too large (over 5MB)", is_error=True)


@pytest.mark.asyncio
async def test_local_fetch_retries_403_and_preserves_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_agents: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        user_agents.append(request.headers["user-agent"])
        if len(user_agents) == 1:
            return httpx2.Response(403)
        return httpx2.Response(
            200,
            headers={"content-type": "text/markdown"},
            text="\n# Direct markdown\n\nKept as-is.\n",
        )

    _use_transport(monkeypatch, handler)
    result = await _call(tmp_path, WebConfig(), "webfetch", {"url": "https://example.test"})

    assert result.output == "\n# Direct markdown\n\nKept as-is.\n"
    assert user_agents[0].startswith("mycode/")
    assert user_agents[1].startswith("Mozilla/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "limit"),
    [
        pytest.param("\n".join(f"line {index}" for index in range(2100)), "2000 lines", id="lines"),
        pytest.param("界" * 20_000, "50KB", id="bytes"),
    ],
)
async def test_fetch_truncates_the_head_and_saves_the_full_content(
    content: str,
    limit: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_transport(
        monkeypatch,
        lambda _request: httpx2.Response(
            200,
            headers={"content-type": "text/plain"},
            text=content,
        ),
    )

    result = await _call(tmp_path, WebConfig(), "webfetch", {"url": "https://example.test"})
    spill = tmp_path / "tool-output" / "webfetch-call-1.md"

    assert result.output.startswith(content[:10])
    assert f"[Output truncated: Showing the first {limit}." in result.output
    assert spill.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_fetch_timeout_is_a_whole_call_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowStream(httpx2.AsyncByteStream):
        @override
        async def __aiter__(self):
            yield b"first"
            await asyncio.sleep(0.6)
            yield b"second"
            await asyncio.sleep(0.6)
            yield b"third"

    _use_transport(
        monkeypatch,
        lambda _request: httpx2.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=SlowStream(),
        ),
    )

    result = await _call(
        tmp_path,
        WebConfig(),
        "webfetch",
        {"url": "https://example.test", "timeout": 1},
    )

    assert result == ToolExecutionResult(
        output=("error: request timed out after 1s; retry with a larger timeout (max 120) if the site is slow"),
        is_error=True,
    )


@pytest.mark.asyncio
async def test_tavily_search_maps_request_and_returns_bounded_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content))
        assert request.headers["x-tavily-access-mode"] == "keyless"
        return httpx2.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Result",
                        "url": "https://example.test/result",
                        "content": "x" * 500,
                    }
                ]
            },
        )

    _use_transport(monkeypatch, handler)
    result = await _call(
        tmp_path,
        WebConfig(search="tavily"),
        "websearch",
        {
            "query": "python docs",
            "max_results": 20,
            "recency": "week",
            "include_domains": ["python.org"],
            "search_depth": "deep",
        },
    )

    assert seen["max_results"] == 10
    assert seen["search_depth"] == "advanced"
    assert seen["time_range"] == "week"
    assert seen["include_domains"] == ["python.org"]
    assert result.metadata == {"results": 1}
    assert result.output.endswith("x" * 400)


@pytest.mark.asyncio
async def test_exa_search_uses_highlights_and_provider_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content))
        assert request.headers["x-api-key"] == "exa-key"
        return httpx2.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Exa result",
                        "url": "https://example.test/exa",
                        "highlights": ["first", "second"],
                    }
                ]
            },
        )

    _use_transport(monkeypatch, handler)
    result = await _call(
        tmp_path,
        WebConfig(search="exa", exa=WebProviderConfig(api_key="exa-key")),
        "websearch",
        {"query": "typed python", "search_depth": "deep", "exclude_domains": ["spam.test"]},
    )

    assert seen["type"] == "deep-lite"
    assert seen["excludeDomains"] == ["spam.test"]
    assert seen["contents"] == {"highlights": {"maxCharacters": 400}}
    assert result.output == "1. Exa result\nhttps://example.test/exa\nfirst second"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["tavily", "exa"])
async def test_fetch_surfaces_provider_failures_returned_with_http_200(
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if provider == "tavily":
        response = {"results": [], "failed_results": [{"url": "https://bad.test", "error": "blocked"}]}
        web = WebConfig(fetch="tavily")
    else:
        response = {"results": [], "statuses": [{"id": "https://bad.test", "status": "error"}]}
        web = WebConfig(fetch="exa", exa=WebProviderConfig(api_key="exa-key"))
    _use_transport(monkeypatch, lambda _request: httpx2.Response(200, json=response))

    result = await _call(tmp_path, web, "webfetch", {"url": "https://bad.test"})

    assert result.is_error is True
    assert result.output.startswith(f"error: {provider} request failed:")
