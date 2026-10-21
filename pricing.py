"""Paid API models the user can pick, with their list prices.

Plain data so config.py can offer the choices without importing the SDKs.
Prices are US dollars per million tokens at the providers' standard tier,
as published in PRICES_AS_OF. ai.py turns token counts into dollars with
them; nothing here is fetched at run time.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_AS_OF = "2026-10"


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    input: float            # $ per 1M uncached input tokens
    cache_read: float       # $ per 1M input tokens served from the prompt cache
    cache_write: float      # $ per 1M input tokens written to the cache
    output: float           # $ per 1M output tokens (reasoning included)

    def cost(self, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write: int = 0) -> float:
        return (input_tokens * self.input + cache_read * self.cache_read
                + cache_write * self.cache_write + output_tokens * self.output) / 1_000_000


# The first model of each provider is its default.
MODELS: dict[str, tuple[Model, ...]] = {
    "openai": (
        Model("gpt-6.1-sol", "GPT-6.1 Sol", 2.00, 0.10, 2.00, 10.00),
        Model("gpt-6-astra", "GPT-6 Astra (most capable)", 10.00, 1.00, 10.00, 50.00),
        Model("gpt-6-luna", "GPT-6 Luna (cheapest)", 0.10, 0.01, 0.10, 0.50),
    ),
    "anthropic": (
        Model("claude-opus-5-5", "Claude Opus 5.5", 4.00, 0.20, 5.00, 20.00),
        Model("claude-fable-5-1", "Claude Fable 5.1 (most capable)", 10.00, 0.25, 12.50, 50.00),
        Model("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 0.20, 2.50, 10.00),
        Model("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)", 0.10, 0.01, 0.125, 0.50),
    ),
}

PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic"}

# Tailoring and drafting backends that bill an API key, by provider.
API_BACKENDS = {"openai-api": "openai", "claude-api": "anthropic"}


def default_model(provider: str) -> str:
    return MODELS[provider][0].id


def model_ids(provider: str) -> tuple[str, ...]:
    return tuple(m.id for m in MODELS[provider])


def find(provider: str, model_id: str) -> Model | None:
    """The price entry for a model; a dated or fallback variant the API
    reports ("claude-opus-5-5-...") is priced as its base model."""
    for m in MODELS.get(provider, ()):
        if model_id == m.id or model_id.startswith(m.id + "-"):
            return m
    return None
