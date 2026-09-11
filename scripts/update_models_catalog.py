"""Update the bundled model metadata catalog from basellm/llm-metadata.

basellm repackages models.dev data as native-provider-only model lists:
https://github.com/basellm/llm-metadata
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

BASELLM_URL = "https://basellm.github.io/llm-metadata/api/all.json"
TARGET_PATH = Path(__file__).resolve().parents[1] / "mycode" / "src" / "mycode" / "models_catalog.json"

# basellm provider ids matching the official providers mycode ships adapters for.
OFFICIAL_PROVIDERS = (
    "alibaba",
    "anthropic",
    "cohere",
    "deepseek",
    "google",
    "meta",
    "minimax",
    "mistral",
    "moonshotai",
    "openai",
    "stepfun",
    "tencent-tokenhub",
    "xai",
    "xiaomi",
    "zai",
)


PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")

# Hand-curated catalog entries, merged over the fetched data so they survive
# regeneration. Add model names basellm's official lists do not carry (e.g.
# release names that third-party interfaces serve) or pin/correct upstream
# data. Entries are frozen at maintenance time.
MANUAL_MODELS: dict[str, dict[str, Any]] = {
    # Served as "deepseek-flash" by the official API; the release name is what
    # third-party interfaces expose.
    "deepseek-v4.1-flash": {
        "context_window": 1_000_000,
        "max_output_tokens": 384_000,
        "reasoning_efforts": ["low", "high", "max"],
        "supports_image_input": True,
        "supports_pdf_input": False,
        "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003, "reasoning": 0.6},
    },
    "qwen3.8-27b": {
        "context_window": 1_000_000,
        "max_output_tokens": 131_072,
        "reasoning_efforts": None,
        "supports_image_input": True,
        "supports_pdf_input": False,
        "cost": {"input": 0.5, "output": 3.0, "cache_read": 0.1},
    },
}


def extract_cost(raw_model: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize basellm cost data: USD per 1M tokens, context tiers only."""

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
        "reasoning_efforts": extract_reasoning_efforts(raw_model),
        "supports_image_input": "image" in input_modalities,
        "supports_pdf_input": "pdf" in input_modalities,
    }
    if cost := extract_cost(raw_model):
        entry["cost"] = cost
    return entry


def main() -> None:
    request = Request(BASELLM_URL, headers={"User-Agent": "mycode/1.0"})
    with urlopen(request, timeout=30) as response:
        source: dict[str, Any] = json.load(response)

    models: dict[str, dict[str, Any]] = {}
    for provider_id in OFFICIAL_PROVIDERS:
        for model_name, raw_model in source[provider_id]["models"].items():
            if model_name in models:
                raise ValueError(f"duplicate official model name: {model_name}")
            models[model_name] = extract_model(raw_model)

    for name in sorted(MANUAL_MODELS.keys() & models.keys()):
        print(f"manual entry shadows upstream data: {name}")
    models.update(MANUAL_MODELS)

    catalog = {"models": models}
    TARGET_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET_PATH} ({len(models)} models)")


if __name__ == "__main__":
    main()
