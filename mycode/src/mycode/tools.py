"""Tool execution runtime.

Tools are :class:`ToolSpec` values, usually produced by wrapping a typed
Python function with :func:`tool`. A tool runner receives a
:class:`ToolContext` and can dispatch to other registered tools through the
generic ``call`` / ``acall``::

    @tool
    def shout(ctx: ToolContext, name: str) -> str:
        return ctx.call("greet", {"name": name}).output.upper()
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import typing
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType
from typing import Any, cast, overload

from griffe import Docstring, DocstringSectionKind, Parser
from pydantic import ConfigDict, Field, ValidationError, create_model

# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

ToolOutputCallback = Callable[[str], None]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Result of one tool run."""

    # Replayed to providers on later turns.
    output: str
    # Structured replay content, e.g. an image returned by a tool.
    content: list[dict[str, Any]] | None = None
    # UI-only structured data such as edit patches and line stats.
    metadata: dict[str, Any] | None = None
    is_error: bool = False


SyncToolRunner = Callable[["ToolContext", dict[str, Any]], ToolExecutionResult]
AsyncToolRunner = Callable[["ToolContext", dict[str, Any]], Coroutine[Any, Any, ToolExecutionResult]]
ToolRunner = SyncToolRunner | AsyncToolRunner


def _is_async_callable(runner: ToolRunner) -> bool:
    current: Any = runner
    while True:
        current = inspect.unwrap(current)
        if isinstance(current, functools.partial):
            current = current.func
            continue
        return inspect.iscoroutinefunction(current) or inspect.iscoroutinefunction(type(current).__call__)


@dataclass(frozen=True)
class ToolSpec:
    """One tool the agent can call."""

    name: str
    description: str
    input_schema: dict[str, Any]
    runner: ToolRunner
    # Streaming tools push incremental output through ToolContext.emit.
    streams_output: bool = False

    @property
    def is_async(self) -> bool:
        return _is_async_callable(self.runner)


# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """Per-call runtime context handed to every tool runner."""

    executor: ToolExecutor
    cwd: str
    tool_output_dir: Path
    supports_image_input: bool = False
    # Only agent-loop calls have a provider tool-call id and live output sink.
    tool_call_id: str | None = None
    emit: ToolOutputCallback | None = None

    def call(self, name: str, args: dict[str, Any]) -> ToolExecutionResult:
        """Dispatch another registered tool by name."""

        return self.executor.execute(name, args, self)

    async def acall(self, name: str, args: dict[str, Any]) -> ToolExecutionResult:
        """Asynchronously dispatch another registered tool by name."""

        return await self.executor.aexecute(name, args, self)


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Tool registry and dispatch for one agent session."""

    def __init__(self, tools: Sequence[ToolSpec]):
        names = [spec.name for spec in tools]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tool name in specs: {names}")
        self._tools: dict[str, ToolSpec] = {spec.name: spec for spec in tools}

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """Return the registered tool specs in definition order."""

        return tuple(self._tools.values())

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
            for spec in self._tools.values()
        ]

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolExecutionResult(output=f"error: unknown tool: {name}", is_error=True)
        if spec.is_async:
            runner = cast(AsyncToolRunner, spec.runner)
            return asyncio.run(runner(ctx, args))
        runner = cast(SyncToolRunner, spec.runner)
        return runner(ctx, args)

    async def aexecute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolExecutionResult(output=f"error: unknown tool: {name}", is_error=True)
        if spec.is_async:
            runner = cast(AsyncToolRunner, spec.runner)
            return await runner(ctx, args)
        runner = cast(SyncToolRunner, spec.runner)
        return await asyncio.to_thread(runner, ctx, args)


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


@overload
def tool(
    function: FunctionType,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, str] | None = None,
    streams_output: bool = False,
) -> ToolSpec: ...


@overload
def tool(
    function: None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, str] | None = None,
    streams_output: bool = False,
) -> Callable[[FunctionType], ToolSpec]: ...


def tool(
    function: FunctionType | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, str] | None = None,
    streams_output: bool = False,
) -> ToolSpec | Callable[[FunctionType], ToolSpec]:
    """Wrap a sync or async Python function as a :class:`ToolSpec`."""

    current_frame = inspect.currentframe()
    caller_frame = current_frame.f_back if current_frame is not None else None
    caller_locals = dict(caller_frame.f_locals) if caller_frame is not None else {}
    del current_frame
    del caller_frame

    def wrap(fn: FunctionType) -> ToolSpec:
        tool_name = name or fn.__name__
        signature = inspect.signature(fn)
        signature_parameters = list(signature.parameters.values())
        try:
            closure = inspect.getclosurevars(fn)
            localns = {**caller_locals, **closure.nonlocals, **closure.globals}
            resolved_hints = typing.get_type_hints(
                fn,
                globalns=fn.__globals__,
                localns=localns,
                include_extras=True,
            )
        except Exception:
            resolved_hints = {}

        wants_context = bool(signature_parameters) and resolved_hints.get(signature_parameters[0].name) is ToolContext
        tool_params = signature_parameters[1:] if wants_context else signature_parameters
        tool_param_names = [parameter.name for parameter in tool_params]
        description_from_docstring, param_descriptions = _parse_tool_docstring(fn)
        if parameters is not None:
            unknown_params = sorted(set(parameters) - set(tool_param_names))
            if unknown_params:
                raise ValueError(f"unknown parameter descriptions for tool {tool_name!r}: {unknown_params}")
            param_descriptions.update(parameters)

        fields: dict[str, Any] = {}
        defaulted_non_nullable_params: set[str] = set()
        for parameter in tool_params:
            if parameter.kind not in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}:
                raise ValueError(f"unsupported tool parameter kind: {parameter.name}")

            annotation = resolved_hints.get(parameter.name, parameter.annotation)
            if annotation is inspect.Signature.empty:
                raise TypeError(f"tool parameter {parameter.name!r} requires a type annotation")

            default = ... if parameter.default is inspect.Signature.empty else parameter.default
            if default is not ... and not _allows_none(annotation):
                defaulted_non_nullable_params.add(parameter.name)
            fields[parameter.name] = (annotation, Field(default, description=param_descriptions.get(parameter.name)))

        model_name = "".join(part.capitalize() for part in tool_name.split("_")) + "Args"
        args_model = create_model(
            model_name,
            __config__=ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True),
            **fields,
        )
        input_schema = _clean_input_schema(args_model.model_json_schema(by_alias=True), tool_name=tool_name)

        resolved_description = description or description_from_docstring
        if not resolved_description:
            raise ValueError(f"tool {tool_name!r} requires a docstring or explicit description")

        is_async = inspect.iscoroutinefunction(fn)

        def prepare_args(args: dict[str, Any]) -> dict[str, Any] | ToolExecutionResult:
            # Strict providers send explicit null for an omitted optional field;
            # drop it so the parameter default applies instead of failing validation.
            validation_args = {
                key: value
                for key, value in args.items()
                if not (value is None and key in defaulted_non_nullable_params)
            }
            try:
                parsed_args = args_model.model_validate(validation_args)
            except ValidationError as exc:
                return ToolExecutionResult(output=f"error: invalid tool input: {exc}", is_error=True)

            return {name: getattr(parsed_args, name) for name in tool_param_names}

        if is_async:

            async def async_runner(context: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
                call_args = prepare_args(args)
                if isinstance(call_args, ToolExecutionResult):
                    return call_args
                value = await (fn(context, **call_args) if wants_context else fn(**call_args))
                return _coerce_tool_result(value)

            runner: ToolRunner = async_runner

        else:

            def sync_runner(context: ToolContext, args: dict[str, Any]) -> ToolExecutionResult:
                call_args = prepare_args(args)
                if isinstance(call_args, ToolExecutionResult):
                    return call_args
                value = fn(context, **call_args) if wants_context else fn(**call_args)
                return _coerce_tool_result(value)

            runner = sync_runner

        return ToolSpec(
            name=tool_name,
            description=resolved_description,
            input_schema=input_schema,
            runner=runner,
            streams_output=streams_output,
        )

    if function is None:
        return wrap
    return wrap(function)


def _allows_none(annotation: Any) -> bool:
    if annotation is Any or annotation is type(None):
        return True
    if typing.get_origin(annotation) is typing.Annotated:
        return _allows_none(typing.get_args(annotation)[0])
    return type(None) in typing.get_args(annotation)


def _parse_tool_docstring(fn: Callable[..., Any]) -> tuple[str | None, dict[str, str]]:
    docstring = inspect.getdoc(fn)
    if not docstring:
        return None, {}

    description_parts: list[str] = []
    param_descriptions: dict[str, str] = {}
    parsed = Docstring(
        docstring,
        parser=Parser.google,
        parser_options={"warn_missing_types": False, "warn_unknown_params": False, "warnings": False},
    ).parse()
    for section in parsed:
        if section.kind is DocstringSectionKind.text:
            description_parts.append(str(section.value).strip())
        elif section.kind is DocstringSectionKind.parameters:
            for parameter in section.value:
                param_descriptions[parameter.name] = parameter.description

    description = "\n\n".join(part for part in description_parts if part)
    return description or docstring, param_descriptions


def _clean_input_schema(value: Any, *, tool_name: str) -> Any:
    """Strip noisy Pydantic metadata and reject unsupported dynamic-key objects.

    User-defined property names are preserved; only schema-level ``title`` and
    ``format`` keys are dropped. ``dict``/map inputs (``additionalProperties``
    other than ``False``) are rejected here so the error surfaces at decoration.
    """

    if isinstance(value, dict):
        if value.get("additionalProperties") not in (None, False):
            raise TypeError(f"tool {tool_name!r} uses unsupported dict/map input; use a Pydantic model or list[Model]")
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"properties", "$defs", "defs"} and isinstance(item, dict):
                cleaned[key] = {name: _clean_input_schema(schema, tool_name=tool_name) for name, schema in item.items()}
            elif key not in {"title", "format"}:
                cleaned[key] = _clean_input_schema(item, tool_name=tool_name)
        return cleaned
    if isinstance(value, list):
        return [_clean_input_schema(item, tool_name=tool_name) for item in value]
    return value


def _coerce_tool_result(value: Any) -> ToolExecutionResult:
    if isinstance(value, ToolExecutionResult):
        return value
    if isinstance(value, str):
        return ToolExecutionResult(output=value)
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return ToolExecutionResult(output=text)
