"""Global config read/write API.

Reads and writes ``~/.mycode/config.json`` only. Project-level
``.mycode/config.json`` files are not modified by this endpoint, and they
continue to override the global file at runtime.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from mycode.providers import (
    list_env_discoverable_providers,
    list_supported_providers,
    provider_default_models,
    provider_env_api_key_names,
)
from mycode_cli.config import (
    PERMISSION_LEVEL_OPTIONS,
    PERMISSION_MODE_OPTIONS,
    REASONING_EFFORT_OPTIONS,
    is_api_key_env_ref,
    resolve_mycode_home,
    validate_global_config,
)
from mycode_cli.server.schemas import SettingsRequest

router = APIRouter()


def _read_raw_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"failed to parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"{path} must contain a JSON object")
    return data


def _present_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Build the UI-facing view: deepcopy, mask literal secrets, normalise
    ``models`` to an ordered list of ids (keeping any per-model overrides
    alongside)."""

    out = copy.deepcopy(raw)
    providers = out.get("providers")
    if not isinstance(providers, dict):
        return out

    for entry in providers.values():
        if not isinstance(entry, dict):
            continue

        api_key = entry.get("api_key")
        if isinstance(api_key, str) and api_key.strip():
            if is_api_key_env_ref(api_key):
                # ${VAR} stays visible — it's not the secret itself.
                entry["api_key_saved"] = False
            else:
                entry["api_key"] = None
                entry["api_key_saved"] = True
        else:
            entry["api_key"] = None
            entry["api_key_saved"] = False

        models = entry.get("models")
        if isinstance(models, dict):
            entry["models"] = list(models.keys())
            overrides = {k: v for k, v in models.items() if isinstance(v, dict) and v}
            if overrides:
                entry["model_overrides"] = overrides

    return out


def _env_presence(raw: dict[str, Any]) -> dict[str, bool]:
    """Booleans for the env vars the UI cares about: every built-in API-key var
    plus any ``${VAR}`` referenced by the saved config."""

    names: set[str] = set()
    for provider_id in list_env_discoverable_providers():
        names.update(provider_env_api_key_names(provider_id))

    for entry in (raw.get("providers") or {}).values():
        if isinstance(entry, dict):
            api_key = entry.get("api_key")
            if isinstance(api_key, str):
                ref = is_api_key_env_ref(api_key)
                if ref:
                    names.add(ref)

    return {name: bool((os.environ.get(name) or "").strip()) for name in sorted(names)}


def _build_response(path: Path) -> dict[str, Any]:
    raw = _read_raw_config(path)
    return {
        "path": str(path),
        "exists": path.is_file(),
        "config": _present_config(raw),
        "options": {
            "provider_types": list(list_supported_providers()),
            "permission_levels": list(PERMISSION_LEVEL_OPTIONS),
            "permission_modes": list(PERMISSION_MODE_OPTIONS),
            "reasoning_efforts": list(REASONING_EFFORT_OPTIONS),
        },
        "env": _env_presence(raw),
        "provider_type_env_vars": {
            ptype: list(provider_env_api_key_names(ptype))
            for ptype in list_supported_providers()
            if provider_env_api_key_names(ptype)
        },
        "provider_type_default_models": {
            ptype: list(provider_default_models(ptype))
            for ptype in list_supported_providers()
            if provider_default_models(ptype)
        },
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@router.get("/settings")
def get_settings_endpoint() -> dict[str, Any]:
    return _build_response(resolve_mycode_home() / "config.json")


@router.put("/settings")
def put_settings_endpoint(payload: SettingsRequest) -> dict[str, Any]:
    path = resolve_mycode_home() / "config.json"
    existing = _read_raw_config(path)
    incoming = copy.deepcopy(payload.config or {})

    # Three-state api_key merge: when the UI sends null / omits it, we copy the
    # existing literal forward so secrets survive a save without round-tripping.
    existing_providers = existing.get("providers") or {}
    for name, entry in (incoming.get("providers") or {}).items():
        if not isinstance(entry, dict) or entry.get("api_key") is not None:
            continue
        prior = existing_providers.get(name) if isinstance(existing_providers, dict) else None
        if isinstance(prior, dict) and "api_key" in prior:
            entry["api_key"] = prior["api_key"]
        else:
            entry.pop("api_key", None)

    try:
        cleaned = validate_global_config(incoming)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _atomic_write(path, cleaned)
    return _build_response(path)
