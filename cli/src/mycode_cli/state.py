"""Remembered CLI selections: the last provider and model, and the reasoning effort per model."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from mycode_cli.config import (
    ResolvedProvider,
    Settings,
    provider_models,
    resolve_mycode_home,
    resolve_provider,
    resolve_provider_choices,
)


@dataclass
class CliState:
    provider: str | None = None
    model: str | None = None
    # Reasoning effort per ``provider/model``; a missing entry means auto.
    efforts: dict[str, str] = field(default_factory=dict)

    def effort_for(self, resolved: ResolvedProvider) -> str | None:
        """Return the remembered effort for a resolved model, if the model still supports it."""

        effort = self.efforts.get(f"{resolved.provider_name}/{resolved.model}")
        if effort and resolved.supports_reasoning_effort and effort in resolved.reasoning_efforts:
            return effort
        return None

    def set_effort(self, provider_name: str, model: str, effort: str | None) -> None:
        key = f"{provider_name}/{model}"
        if effort is None:
            self.efforts.pop(key, None)
        else:
            self.efforts[key] = effort


def _state_path() -> Path:
    return resolve_mycode_home() / "cli.json"


def load_state() -> CliState:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return CliState()
    if not isinstance(data, dict):
        return CliState()

    provider = data.get("provider")
    model = data.get("model")
    efforts = data.get("effort")
    return CliState(
        provider=provider if isinstance(provider, str) else None,
        model=model if isinstance(model, str) else None,
        efforts={key: value for key, value in efforts.items() if isinstance(key, str) and isinstance(value, str)}
        if isinstance(efforts, dict)
        else {},
    )


def save_state(state: CliState) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="cli.json.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"provider": state.provider, "model": state.model, "effort": state.efforts}, file, indent=2)
            file.write("\n")
        os.replace(temp_name, path)
    except Exception:
        with suppress(OSError):
            os.unlink(temp_name)
        raise


def resolve_remembered_provider(
    settings: Settings,
    state: CliState,
    *,
    provider_name: str | None = None,
    model: str | None = None,
) -> ResolvedProvider:
    """Resolve the provider to start with: explicit choices first, then the remembered one.

    A remembered provider that is no longer available is skipped; a remembered
    model the provider no longer lists falls back to the provider's first model.
    """

    if not provider_name:
        remembered = next(
            (choice for choice in resolve_provider_choices(settings) if choice.provider_name == state.provider), None
        )
        if remembered is not None:
            provider_name = state.provider
            if not model and state.model in provider_models(settings, remembered):
                model = state.model
    return resolve_provider(settings, provider_name=provider_name, model=model)
