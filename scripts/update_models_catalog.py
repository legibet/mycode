"""Update the bundled model metadata catalog from models.dev."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mycode.utils import as_bool, as_int

MODELS_DEV_URL = "https://models.dev/api.json"
TARGET_PATH = Path(__file__).resolve().parents[1] / "mycode" / "src" / "mycode" / "models_catalog.json"
PROVIDERS = (
    "anthropic",
    "deepseek",
    "google",
    "minimax",
    "moonshotai",
    "openai",
    "openai_chat",
    "openrouter",
    "xai",
    "zai",
)


PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")


def as_price(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def extract_cost(raw_model: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize models.dev cost data: USD per 1M tokens, context tiers only."""

    raw_cost = raw_model.get("cost")
    if not isinstance(raw_cost, dict):
        return None

    cost: dict[str, Any] = {key: price for key in PRICE_KEYS if (price := as_price(raw_cost.get(key))) is not None}

    tiers: list[dict[str, Any]] = []
    raw_tiers = raw_cost.get("tiers")
    for raw_tier in raw_tiers if isinstance(raw_tiers, list) else []:
        if not isinstance(raw_tier, dict):
            continue
        tier_info = raw_tier.get("tier")
        if not isinstance(tier_info, dict) or tier_info.get("type") != "context":
            continue
        size = as_int(tier_info.get("size"))
        if size is None:
            continue
        tier: dict[str, Any] = {key: price for key in PRICE_KEYS if (price := as_price(raw_tier.get(key))) is not None}
        tier["size"] = size
        tiers.append(tier)
    if tiers:
        cost["tiers"] = sorted(tiers, key=lambda tier: tier["size"])

    return cost or None


def extract_reasoning_efforts(raw_model: dict[str, Any]) -> list[str] | None:
    """Extract the string values advertised by effort options."""

    raw_options = raw_model.get("reasoning_options")
    if not isinstance(raw_options, list):
        return None

    efforts: list[str] = []
    for option in raw_options:
        if not isinstance(option, dict) or option.get("type") != "effort":
            continue
        values = option.get("values")
        if isinstance(values, list):
            efforts.extend(value for value in values if isinstance(value, str))
    return list(dict.fromkeys(efforts))


def main() -> None:
    request = Request(MODELS_DEV_URL, headers={"User-Agent": "mycode/1.0"})
    with urlopen(request, timeout=30) as response:
        raw_source = json.loads(response.read().decode("utf-8"))

    if not isinstance(raw_source, dict):
        raise SystemExit("models.dev returned an invalid catalog")
    source: dict[str, Any] = raw_source

    catalog: dict[str, dict[str, dict[str, Any]]] = {}
    for provider_name in PROVIDERS:
        provider = source.get(provider_name)
        if not isinstance(provider, dict):
            continue
        raw_models = provider.get("models")
        if not isinstance(raw_models, dict):
            continue

        models: dict[str, dict[str, Any]] = {}
        for model_id, raw_model in raw_models.items():
            if not isinstance(model_id, str) or not isinstance(raw_model, dict):
                continue

            limits = raw_model.get("limit")
            limit_data = limits if isinstance(limits, dict) else {}
            context_window = limit_data.get("context")
            max_output_tokens = limit_data.get("output")
            supports_reasoning = raw_model.get("reasoning")
            modalities = raw_model.get("modalities")
            input_modalities = modalities.get("input") if isinstance(modalities, dict) else None
            supports_image_input = isinstance(input_modalities, list) and "image" in input_modalities
            supports_pdf_input = isinstance(input_modalities, list) and "pdf" in input_modalities

            entry: dict[str, Any] = {
                "context_window": as_int(context_window),
                "max_output_tokens": as_int(max_output_tokens),
                "supports_reasoning": as_bool(supports_reasoning),
                "reasoning_efforts": extract_reasoning_efforts(raw_model),
                "supports_image_input": supports_image_input,
                "supports_pdf_input": supports_pdf_input,
            }
            if cost := extract_cost(raw_model):
                entry["cost"] = cost
            models[model_id] = entry

        if models:
            catalog[provider_name] = dict(sorted(models.items()))

    TARGET_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET_PATH}")


if __name__ == "__main__":
    main()
