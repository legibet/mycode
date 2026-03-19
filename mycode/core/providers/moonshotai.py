"""Moonshot Kimi adapter using the provider's Anthropic-compatible messages API."""

from __future__ import annotations

from typing import Any

from mycode.core.providers.anthropic_like import THINKING_BUDGETS, AnthropicLikeAdapter
from mycode.core.providers.base import ProviderRequest


class MoonshotAIAdapter(AnthropicLikeAdapter):
    provider_id = "moonshotai"
    label = "Moonshot"
    default_base_url = "https://api.moonshot.ai/anthropic"
    env_api_key_names = ("MOONSHOT_API_KEY",)
    default_models = ("kimi-k2.5",)

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = (request.reasoning_effort or "").strip().lower()
        if effort in {"none", "off", "disabled"}:
            return {"type": "disabled"}

        if effort in THINKING_BUDGETS:
            return {"type": "enabled", "budget_tokens": THINKING_BUDGETS[effort]}

        model = request.model.lower()
        # Real-provider testing showed that Moonshot's messages endpoint only
        # returns reasoning blocks when thinking is enabled explicitly, and
        # tool loops require the prior reasoning block to be replayed.
        if model == "kimi-k2.5" or model.startswith("kimi-k2-thinking"):
            return {"type": "enabled", "budget_tokens": THINKING_BUDGETS["medium"]}

        return None
