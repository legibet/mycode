"""MiniMax adapter using the provider's Anthropic-compatible messages API."""

from __future__ import annotations

from typing import Any

from mycode.core.providers.anthropic_like import THINKING_BUDGETS, AnthropicLikeAdapter
from mycode.core.providers.base import ProviderRequest


class MiniMaxAdapter(AnthropicLikeAdapter):
    provider_id = "minimax"
    label = "MiniMax"
    default_base_url = "https://api.minimax.io/anthropic"
    env_api_key_names = ("MINIMAX_API_KEY",)
    default_models = ("MiniMax-M2.7", "MiniMax-M2.7-highspeed")

    def thinking_config(self, request: ProviderRequest) -> dict[str, Any] | None:
        effort = (request.reasoning_effort or "").strip().lower()
        if not effort or effort == "auto":
            # MiniMax already emits thinking blocks by default on its messages
            # endpoint, so we only send explicit config when the caller asked
            # for a non-default reasoning mode.
            return None
        if effort in {"none", "off", "disabled"}:
            return {"type": "disabled"}
        budget = THINKING_BUDGETS.get(effort)
        if budget is None:
            return None
        return {"type": "enabled", "budget_tokens": budget}
