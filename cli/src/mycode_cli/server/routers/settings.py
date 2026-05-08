"""Global config read/write API.

Reads and writes ``~/.mycode/config.json`` only. Project-level
``.mycode/config.json`` files are not modified by this endpoint, and they
continue to override the global file at runtime.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from mycode.providers import (
    is_supported_provider,
    list_env_discoverable_providers,
    list_supported_providers,
    provider_default_models,
    provider_env_api_key_names,
)
from mycode_cli.config import (
    PERMISSION_LEVEL_OPTIONS,
    PERMISSION_MODE_OPTIONS,
    REASONING_EFFORT_OPTIONS,
    normalize_permission_level,
    normalize_permission_mode,
    normalize_reasoning_effort,
    resolve_mycode_home,
)
from mycode_cli.server.schemas import SettingsRequest

router = APIRouter()

_API_KEY_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_MODEL_OVERRIDE_KEYS = (
    "context_window",
    "max_output_tokens",
    "supports_reasoning",
    "supports_image_input",
    "supports_pdf_input",
)


def is_api_key_env_ref(value: str) -> str | None:
    """Return the env var name when ``value`` is a ``${NAME}`` reference."""

    match = _API_KEY_ENV_REF_RE.fullmatch(value.strip())
    return match.group(1) if match else None


def _optional_string(raw: dict[str, Any], key: str, label: str) -> str | None:
    """Read an optional string field. Returns the trimmed value, or ``None`` when
    absent / null / empty so callers can simply skip the key."""

    if key not in raw:
        return None
    value = raw[key]
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value.strip() or None


def _validate_default(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("default must be an object")

    out: dict[str, Any] = {}
    for key in ("provider", "model"):
        value = _optional_string(raw, key, f"default.{key}")
        if value:
            out[key] = value

    effort = raw.get("reasoning_effort")
    if effort not in (None, ""):
        normalize_reasoning_effort(effort)
        out["reasoning_effort"] = effort

    ct = raw.get("compact_threshold")
    if ct is False:
        out["compact_threshold"] = False
    elif ct is not None:
        if isinstance(ct, bool) or not isinstance(ct, int | float) or not 0 <= ct <= 1:
            raise ValueError("default.compact_threshold must be a number in [0, 1] or false")
        out["compact_threshold"] = float(ct)

    return out


def _validate_permission(raw: Any) -> Any:
    if isinstance(raw, str):
        return normalize_permission_level(raw)
    if not isinstance(raw, dict):
        raise ValueError("permission must be a string or object")

    out: dict[str, Any] = {}
    if "level" in raw:
        out["level"] = normalize_permission_level(raw.get("level"))
    if "mode" in raw:
        out["mode"] = normalize_permission_mode(raw.get("mode"))
    return out


def _validate_provider(name: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"provider {name!r} must be an object")

    out: dict[str, Any] = {}

    raw_type = raw.get("type")
    if raw_type in (None, ""):
        # Built-in name → type fallback; otherwise the user must spell it out.
        if not is_supported_provider(name):
            raise ValueError(f"provider {name!r} must set 'type'")
    elif not isinstance(raw_type, str):
        raise ValueError(f"provider {name!r}: type must be a string")
    elif not is_supported_provider(raw_type):
        supported = ", ".join(list_supported_providers())
        raise ValueError(f"provider {name!r}: unsupported type {raw_type!r}; supported: {supported}")
    else:
        out["type"] = raw_type

    for key in ("api_key", "base_url"):
        value = _optional_string(raw, key, f"provider {name!r}: {key}")
        if value:
            out[key] = value

    effort = raw.get("reasoning_effort")
    if effort not in (None, ""):
        normalize_reasoning_effort(effort)
        out["reasoning_effort"] = effort

    raw_models = raw.get("models")
    if raw_models is not None:
        models = _validate_models(name, raw_models)
        if models:
            out["models"] = models

    return out


def _validate_models(name: str, raw: Any) -> dict[str, dict[str, Any]]:
    # Both list (ids only) and dict (id → metadata overrides) are accepted; we
    # always normalise to the dict form for storage.
    if isinstance(raw, list):
        items: list[tuple[Any, Any]] = [(m, None) for m in raw]
    elif isinstance(raw, dict):
        items = list(raw.items())
    else:
        raise ValueError(f"provider {name!r}: models must be a list or object")

    out: dict[str, dict[str, Any]] = {}
    for model_id, overrides in items:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError(f"provider {name!r}: model id must be a non-empty string")
        key = model_id.strip()
        if overrides is None:
            out[key] = {}
        elif isinstance(overrides, dict):
            out[key] = {k: v for k, v in overrides.items() if k in _MODEL_OVERRIDE_KEYS and v is not None}
        else:
            raise ValueError(f"provider {name!r}: model {key!r} config must be an object")
    return out


def validate_global_config(data: Any) -> dict[str, Any]:
    """Validate a raw global config payload. Returns a cleaned dict ready to persist.

    Empty / null fields are dropped. Raises ``ValueError`` on invalid input.
    """

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config must be an object")

    out: dict[str, Any] = {}

    if data.get("default") is not None:
        default = _validate_default(data["default"])
        if default:
            out["default"] = default

    if data.get("permission") is not None:
        out["permission"] = _validate_permission(data["permission"])

    raw_providers = data.get("providers")
    if raw_providers is not None:
        if not isinstance(raw_providers, dict):
            raise ValueError("providers must be an object")
        providers: dict[str, dict[str, Any]] = {}
        for name, raw in raw_providers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("provider name must be a non-empty string")
            cleaned = name.strip()
            providers[cleaned] = _validate_provider(cleaned, raw)
        if providers:
            out["providers"] = providers

    return out


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
async def get_settings_endpoint() -> dict[str, Any]:
    return _build_response(resolve_mycode_home() / "config.json")


@router.put("/settings")
async def put_settings_endpoint(payload: SettingsRequest) -> dict[str, Any]:
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
