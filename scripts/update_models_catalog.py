"""Update the bundled model metadata catalog from models.dev."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

MODELS_DEV_URL = "https://models.dev/catalog.json"
TARGET_PATH = Path(__file__).resolve().parents[1] / "mycode" / "src" / "mycode" / "models_catalog.json"
PROVIDERS = (
    "alibaba",
    "anthropic",
    "deepseek",
    "google",
    "minimax",
    "moonshotai",
    "openai",
    "openrouter",
    "xai",
    "zai",
)

OFFICIAL_MODEL_PROVIDERS = {
    "alibaba": "alibaba",
    "anthropic": "anthropic",
    "cohere": "cohere",
    "deepseek": "deepseek",
    "google": "google",
    "meta": "meta",
    "minimax": "minimax",
    "mistral": "mistral",
    "moonshotai": "moonshotai",
    "openai": "openai",
    "stepfun": "stepfun",
    "tencent": "tencent-tokenhub",
    "xai": "xai",
    "xiaomi": "xiaomi",
    "zhipuai": "zai",
}


PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")


def extract_cost(raw_model: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize models.dev cost data: USD per 1M tokens, context tiers only."""

    raw_cost = raw_model.get("cost")
    if raw_cost is None:
        return None

    cost = {key: raw_cost[key] for key in PRICE_KEYS if raw_cost.get(key) is not None}

    tiers: list[dict[str, Any]] = []
    for raw_tier in raw_cost.get("tiers", []):
        tier_info = raw_tier.get("tier")
        if not tier_info or tier_info.get("type") != "context" or tier_info.get("size") is None:
            continue
        tier = {key: raw_tier[key] for key in PRICE_KEYS if raw_tier.get(key) is not None}
        tier["size"] = tier_info["size"]
        tiers.append(tier)
    if tiers:
        cost["tiers"] = sorted(tiers, key=lambda tier: tier["size"])

    return cost or None


def extract_reasoning_efforts(raw_model: dict[str, Any]) -> list[str] | None:
    """Extract the string values advertised by effort options."""

    if "reasoning_options" not in raw_model:
        return None

    efforts: list[str] = []
    for option in raw_model["reasoning_options"]:
        if option.get("type") != "effort":
            continue
        values = option.get("values")
        if isinstance(values, list):
            efforts.extend(value for value in values if isinstance(value, str))
    return list(dict.fromkeys(efforts))


def extract_model(raw_model: dict[str, Any]) -> dict[str, Any]:
    limits = raw_model.get("limit", {})
    input_modalities = raw_model.get("modalities", {}).get("input", [])
    entry: dict[str, Any] = {
        "context_window": limits.get("context"),
        "max_output_tokens": limits.get("output"),
        "supports_reasoning": raw_model.get("reasoning"),
        "reasoning_efforts": extract_reasoning_efforts(raw_model),
        "supports_image_input": "image" in input_modalities,
        "supports_pdf_input": "pdf" in input_modalities,
    }
    if cost := extract_cost(raw_model):
        entry["cost"] = cost
    return entry


def main() -> None:
    request = Request(MODELS_DEV_URL, headers={"User-Agent": "mycode/1.0"})
    with urlopen(request, timeout=30) as response:
        source: dict[str, Any] = json.load(response)

    source_providers = source["providers"]

    providers: dict[str, dict[str, dict[str, Any]]] = {}
    for provider_name in PROVIDERS:
        raw_models = source_providers[provider_name]["models"]
        models = {model_id: extract_model(raw_model) for model_id, raw_model in raw_models.items()}
        providers[provider_name] = models

    fallback: dict[str, dict[str, Any]] = {}
    for model_id in source["models"]:
        owner, model_name = model_id.split("/", 1)
        provider_name = OFFICIAL_MODEL_PROVIDERS.get(owner)
        if provider_name is None:
            continue
        raw_model = source_providers[provider_name]["models"].get(model_name)
        if raw_model is None:
            continue
        if model_name in fallback:
            raise ValueError(f"duplicate official model name: {model_name}")
        fallback[model_name] = extract_model(raw_model)

    catalog = {"fallback": fallback, "providers": providers}
    TARGET_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET_PATH}")


if __name__ == "__main__":
    main()
