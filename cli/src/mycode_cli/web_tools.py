"""Configurable web fetch and search tools for mycode-cli."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx2
from bs4 import BeautifulSoup
from markdownify import ATX, markdownify

from mycode.tools import ToolContext, ToolExecutionResult, ToolSpec, tool
from mycode_cli import __version__
from mycode_cli.config import WebConfig, resolve_web_api_key
from mycode_cli.tools import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS = 10
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_EXCERPT_CHARS = 400

_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_EXA_CONTENTS_URL = "https://api.exa.ai/contents"
_EXA_SEARCH_URL = "https://api.exa.ai/search"
_ACCEPT = (
    "text/markdown;q=1.0, text/html;q=0.9, application/xhtml+xml;q=0.8, "
    "text/plain;q=0.7, application/json;q=0.7, */*;q=0.1"
)
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

type Recency = Literal["day", "week", "month", "year"]
type SearchDepth = Literal["fast", "balanced", "deep"]


class _ResponseTooLarge(Exception):
    pass


class _WebToolError(Exception):
    pass


def build_web_tools(web: WebConfig) -> list[ToolSpec]:
    """Build the web tools for one resolved settings snapshot."""

    @tool(
        name="webfetch",
        description="Read the content of a URL.",
        parameters={
            "url": "HTTP or HTTPS URL to read.",
            "timeout": (
                f"Whole-call timeout in seconds. Defaults to {DEFAULT_TIMEOUT_SECONDS}; maximum {MAX_TIMEOUT_SECONDS}."
            ),
        },
    )
    async def webfetch(
        ctx: ToolContext,
        url: str,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ToolExecutionResult:
        return await _run_webfetch(ctx, web, url, timeout)

    tools = [webfetch]
    if web.search == "off":
        return tools

    @tool(
        name="websearch",
        description=(
            "Search the web and return matching pages with short excerpts. Use webfetch to read a page's full content."
        ),
        parameters={
            "query": "Search query.",
            "max_results": f"Maximum results. Defaults to {DEFAULT_MAX_RESULTS}; maximum {MAX_RESULTS}.",
            "recency": "Optional publication recency filter.",
            "include_domains": "Only include results from these domains.",
            "exclude_domains": "Exclude results from these domains.",
            "search_depth": "Use deep only when a normal search is insufficient.",
        },
    )
    async def websearch(
        query: str,
        max_results: int | None = None,
        recency: Recency | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        search_depth: SearchDepth | None = None,
    ) -> ToolExecutionResult:
        return await _run_websearch(
            web,
            query,
            max_results,
            recency,
            include_domains,
            exclude_domains,
            search_depth,
        )

    tools.append(websearch)
    return tools


async def _run_webfetch(
    ctx: ToolContext,
    web: WebConfig,
    url: str,
    requested_timeout: int | None,
) -> ToolExecutionResult:
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        return _error("URL must start with http:// or https://")

    budget = max(
        1,
        min(
            requested_timeout if requested_timeout is not None else DEFAULT_TIMEOUT_SECONDS,
            MAX_TIMEOUT_SECONDS,
        ),
    )
    provider = web.fetch
    try:
        async with asyncio.timeout(budget):
            if provider == "local":
                content, final_url = await _fetch_local(url, budget)
            elif provider == "tavily":
                content, final_url = await _fetch_tavily(web, url, budget)
            else:
                content, final_url = await _fetch_exa(web, url, budget)
            return _format_fetch_result(ctx, url, final_url, content)
    except (TimeoutError, httpx2.TimeoutException):
        return _error(
            f"request timed out after {budget}s; retry with a larger timeout "
            + f"(max {MAX_TIMEOUT_SECONDS}) if the site is slow"
        )
    except _ResponseTooLarge:
        return _error("response too large (over 5MB)")
    except (_WebToolError, ValueError) as exc:
        return _error(str(exc))
    except httpx2.RequestError as exc:
        prefix = f"{provider} request failed" if provider != "local" else "request failed"
        return _error(f"{prefix}: {exc}")


async def _run_websearch(
    web: WebConfig,
    query: str,
    max_results: int | None,
    recency: Recency | None,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    search_depth: SearchDepth | None,
) -> ToolExecutionResult:
    provider = web.search
    count = max(1, min(max_results if max_results is not None else DEFAULT_MAX_RESULTS, MAX_RESULTS))
    depth = search_depth or "balanced"
    try:
        async with asyncio.timeout(DEFAULT_TIMEOUT_SECONDS):
            if provider == "tavily":
                results = await _search_tavily(web, query, count, recency, include_domains, exclude_domains, depth)
            else:
                results = await _search_exa(web, query, count, recency, include_domains, exclude_domains, depth)
    except (TimeoutError, httpx2.TimeoutException):
        return _error(f"request timed out after {DEFAULT_TIMEOUT_SECONDS}s")
    except _ResponseTooLarge:
        return _error("response too large (over 5MB)")
    except (_WebToolError, ValueError) as exc:
        return _error(str(exc))
    except httpx2.RequestError as exc:
        return _error(f"{provider} request failed: {exc}")

    if not results:
        return ToolExecutionResult(output="No results found.", metadata={"results": 0})

    blocks: list[str] = []
    for index, result in enumerate(results, start=1):
        title = result["title"] or result["url"] or "Untitled"
        result_url = result["url"]
        excerpt = result["excerpt"][:MAX_EXCERPT_CHARS]
        blocks.append("\n".join(part for part in (f"{index}. {title}", result_url, excerpt) if part))
    return ToolExecutionResult(output="\n\n".join(blocks), metadata={"results": len(results)})


async def _fetch_local(url: str, budget: int) -> tuple[str, str]:
    headers = {"Accept": _ACCEPT, "User-Agent": f"mycode/{__version__}"}
    async with httpx2.AsyncClient(follow_redirects=True, max_redirects=5, timeout=budget) as client:
        for attempt in range(2):
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 403 and attempt == 0:
                    headers = {**headers, "User-Agent": _BROWSER_USER_AGENT}
                    continue
                if response.status_code >= 400:
                    raise _WebToolError(f"HTTP {response.status_code} {response.reason_phrase} for {response.url}")
                body = await _read_body(response)
                return _convert_response(response, body), str(response.url)
    raise AssertionError("unreachable")


async def _fetch_tavily(web: WebConfig, url: str, budget: int) -> tuple[str, str]:
    api_key = resolve_web_api_key(web, "tavily")
    data = await _post_json(
        "tavily",
        _TAVILY_EXTRACT_URL,
        {
            "urls": [url],
            "format": "markdown",
            "extract_depth": "basic",
            "timeout": min(budget, 60),
        },
        _tavily_headers(api_key),
        budget,
        keyless=api_key is None,
    )
    results = data.get("results")
    if isinstance(results, list) and results:
        result = results[0]
        if isinstance(result, dict) and isinstance(result.get("raw_content"), str):
            return result["raw_content"], str(result.get("url") or url)

    failed = data.get("failed_results")
    if isinstance(failed, list) and failed and isinstance(failed[0], dict):
        raise _WebToolError(f"tavily request failed: {_short_message(failed[0])}")
    raise _WebToolError("tavily request failed: no content returned")


async def _fetch_exa(web: WebConfig, url: str, budget: int) -> tuple[str, str]:
    api_key = resolve_web_api_key(web, "exa")
    if api_key is None:
        raise _WebToolError("web.exa.api_key is required")
    data = await _post_json(
        "exa",
        _EXA_CONTENTS_URL,
        {"urls": [url], "text": True},
        _exa_headers(api_key),
        budget,
    )
    statuses = data.get("statuses")
    if isinstance(statuses, list) and statuses:
        status = statuses[0]
        if isinstance(status, dict) and status.get("status") != "success":
            raise _WebToolError(f"exa request failed: {_short_message(status)}")
    results = data.get("results")
    if isinstance(results, list) and results:
        result = results[0]
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            return result["text"], str(result.get("url") or url)
    raise _WebToolError("exa request failed: no content returned")


async def _search_tavily(
    web: WebConfig,
    query: str,
    max_results: int,
    recency: Recency | None,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    depth: SearchDepth,
) -> list[dict[str, str]]:
    api_key = resolve_web_api_key(web, "tavily")
    payload: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "search_depth": {"fast": "fast", "balanced": "basic", "deep": "advanced"}[depth],
        "chunks_per_source": 1,
        "include_answer": False,
        "include_raw_content": False,
    }
    if recency:
        payload["time_range"] = recency
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    data = await _post_json(
        "tavily",
        _TAVILY_SEARCH_URL,
        payload,
        _tavily_headers(api_key),
        DEFAULT_TIMEOUT_SECONDS,
        keyless=api_key is None,
    )
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        raise _WebToolError("tavily request failed: invalid results")
    return [
        {
            "title": str(result.get("title") or ""),
            "url": str(result.get("url") or ""),
            "excerpt": str(result.get("content") or ""),
        }
        for result in raw_results
        if isinstance(result, dict)
    ]


async def _search_exa(
    web: WebConfig,
    query: str,
    max_results: int,
    recency: Recency | None,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    depth: SearchDepth,
) -> list[dict[str, str]]:
    api_key = resolve_web_api_key(web, "exa")
    if api_key is None:
        raise _WebToolError("web.exa.api_key is required")
    payload: dict[str, Any] = {
        "query": query,
        "numResults": max_results,
        "type": {"fast": "fast", "balanced": "auto", "deep": "deep-lite"}[depth],
        "contents": {"highlights": {"maxCharacters": MAX_EXCERPT_CHARS}},
    }
    if recency:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
        payload["startPublishedDate"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains

    data = await _post_json(
        "exa",
        _EXA_SEARCH_URL,
        payload,
        _exa_headers(api_key),
        DEFAULT_TIMEOUT_SECONDS,
    )
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        raise _WebToolError("exa request failed: invalid results")
    results: list[dict[str, str]] = []
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        highlights = result.get("highlights")
        excerpt = " ".join(str(item) for item in highlights) if isinstance(highlights, list) else ""
        results.append(
            {
                "title": str(result.get("title") or ""),
                "url": str(result.get("url") or ""),
                "excerpt": excerpt,
            }
        )
    return results


async def _post_json(
    provider: Literal["tavily", "exa"],
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    budget: int,
    *,
    keyless: bool = False,
) -> dict[str, Any]:
    async with (
        httpx2.AsyncClient(timeout=budget) as client,
        client.stream("POST", url, json=payload, headers=headers) as response,
    ):
        body = await _read_body(response)
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _WebToolError(f"{provider} request failed: invalid JSON response") from exc
        if response.status_code >= 400:
            if provider == "tavily" and keyless and response.status_code == 429:
                raise _WebToolError("tavily rate limit exceeded; set web.tavily.api_key to raise limits")
            raise _WebToolError(f"{provider} request failed: {_short_message(data)}")
    if not isinstance(data, dict):
        raise _WebToolError(f"{provider} request failed: invalid JSON response")
    return data


async def _read_body(response: httpx2.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
        raise _ResponseTooLarge

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge
    return bytes(body)


def _convert_response(response: httpx2.Response, body: bytes) -> str:
    content_type = response.headers.get("content-type", "")
    mime = content_type.split(";", 1)[0].strip().lower()
    text_mimes = {"application/json", "application/xml"}
    is_text = (
        not mime or mime.startswith("text/") or mime in text_mimes or mime.endswith("+json") or mime.endswith("+xml")
    )
    if mime not in {"text/html", "application/xhtml+xml"} and not is_text:
        raise _WebToolError(f"unsupported content type: {mime}")

    text = body.decode(response.encoding or "utf-8", errors="replace")
    if mime not in {"text/html", "application/xhtml+xml"}:
        return text

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "meta", "link", "iframe", "svg"]):
        tag.decompose()
    for image in soup.find_all("img"):
        if str(image.get("src") or "").startswith("data:"):
            image.decompose()
    converted = markdownify(str(soup.body or soup), heading_style=ATX)
    return re.sub(r"\n{4,}", "\n\n\n", converted).strip()


def _format_fetch_result(ctx: ToolContext, requested_url: str, final_url: str, content: str) -> ToolExecutionResult:
    visible, truncated_by = _truncate_head(content)
    notes: list[str] = []
    if final_url != requested_url:
        notes.append(f"[Redirected to {final_url}]")
    if truncated_by:
        path = ctx.tool_output_dir / f"webfetch-{ctx.tool_call_id or 'call'}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        limit = f"{DEFAULT_MAX_LINES} lines" if truncated_by == "lines" else f"{DEFAULT_MAX_BYTES // 1024}KB"
        notes.append(
            f"[Output truncated: Showing the first {limit}. Full output: {path}. " + "Use read to inspect the rest.]"
        )
    output = "\n\n".join(part for part in (visible, *notes) if part)
    return ToolExecutionResult(output=output)


def _truncate_head(text: str) -> tuple[str, str | None]:
    raw_lines = text.splitlines(keepends=True)
    if len(raw_lines) <= DEFAULT_MAX_LINES and len(text.encode("utf-8")) <= DEFAULT_MAX_BYTES:
        return text, None

    out: list[str] = []
    used = 0
    sliced = False

    for raw_line in raw_lines:
        if len(out) >= DEFAULT_MAX_LINES:
            break
        line = raw_line.rstrip("\r\n")
        line_bytes = len(raw_line.encode("utf-8"))
        if used + line_bytes > DEFAULT_MAX_BYTES:
            budget = DEFAULT_MAX_BYTES - used
            if budget > 0:
                out.append(line.encode("utf-8")[:budget].decode("utf-8", errors="ignore"))
                sliced = True
            break
        out.append(line)
        used += line_bytes

    truncated_by = "lines" if len(out) == DEFAULT_MAX_LINES and not sliced else "bytes"
    return "\n".join(out), truncated_by


def _tavily_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    return headers


def _exa_headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "x-api-key": api_key}


def _short_message(value: object) -> str:
    if isinstance(value, dict):
        detail = value.get("detail")
        if isinstance(detail, dict) and detail.get("error"):
            return str(detail["error"])
        for key in ("error", "message", "status"):
            if value.get(key):
                return str(value[key])
    return "request failed"


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(output=f"error: {message}", is_error=True)
