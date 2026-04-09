"""Update the bundled model metadata catalog from models.dev."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

MODELS_DEV_URL = "https://models.dev/api.json"
TARGET_PATH = (
    Path(__file__).resolve().parents[1]
    / "mycode-go"
    / "internal"
    / "models"
    / "models_catalog.json"
)
PROVIDERS = (
    "aihubmix",
    "anthropic",
    "deepseek",
    "google",
    "minimax",
    "moonshotai",
    "openai",
    "openai_chat",
    "openrouter",
    "zai",
)


def main() -> None:
    request = Request(MODELS_DEV_URL, headers={"User-Agent": "mycode-go/1.0"})
    with urlopen(request, timeout=30) as response:
        raw_source = json.loads(response.read().decode("utf-8"))

    if not isinstance(raw_source, dict):
        raise SystemExit("models.dev returned an invalid catalog")
    source: dict[str, Any] = raw_source

    catalog: dict[str, dict[str, dict[str, int | bool | None]]] = {}
    for provider_name in PROVIDERS:
        provider = source.get(provider_name)
        if not isinstance(provider, dict):
            continue

        raw_models = provider.get("models")
        if not isinstance(raw_models, dict):
            continue

        models: dict[str, dict[str, int | bool | None]] = {}
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

            models[model_id] = {
                "context_window": context_window
                if isinstance(context_window, int) and not isinstance(context_window, bool)
                else None,
                "max_output_tokens": max_output_tokens
                if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool)
                else None,
                "supports_reasoning": supports_reasoning if isinstance(supports_reasoning, bool) else None,
                "supports_image_input": supports_image_input,
                "supports_pdf_input": supports_pdf_input,
            }

        if models:
            catalog[provider_name] = dict(sorted(models.items()))

    TARGET_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {TARGET_PATH}")


if __name__ == "__main__":
    main()
