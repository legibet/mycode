"""Anthropic Messages adapter built on the official Anthropic SDK."""

from __future__ import annotations

from typing import Any

from mycode.core.providers.anthropic_like import THINKING_BUDGETS, AnthropicLikeAdapter
from mycode.core.providers.base import ProviderRequest


class AnthropicAdapter(AnthropicLikeAdapter):
    provider_id = "anthropic"
    label = "Anthropic"
    default_base_url = "https://api.anthropic.com"
    env_api_key_names = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    default_models = ("claude-sonnet-4-6", "claude-opus-4-1")

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = (request.reasoning_effort or "").strip().lower()
        if not effort or effort == "auto":
            return None
        if effort in {"none", "off", "disabled"}:
            return {"type": "disabled"}
        budget = THINKING_BUDGETS.get(effort)
        if budget is None:
            return None
        return {"type": "enabled", "budget_tokens": budget}
