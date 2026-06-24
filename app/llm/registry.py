"""LLM provider registry."""
from __future__ import annotations

from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.openai_provider import OpenAIProvider


class LLMRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._providers: dict[str, LLMProvider] = {
            "anthropic": AnthropicProvider(settings),
            "openai": OpenAIProvider(
                settings,
                name="openai",
                api_key=settings.openai_api_key,
                default_model=settings.default_openai_model,
            ),
            "local": OpenAIProvider(
                settings,
                name="local",
                api_key=(
                    settings.local_openai_api_key if settings.local_openai_base_url else None
                ),
                base_url=settings.local_openai_base_url,
                default_model=settings.local_model,
                # Local OpenAI-compatible servers (Ollama/vLLM) may not accept
                # image_url content; degrade gracefully to a text note unless
                # the configured model is known to support vision.
                supports_vision=settings.local_supports_vision,
            ),
        }

    def get(self, name: str | None = None) -> LLMProvider:
        name = name or self._settings.default_llm_provider
        provider = self._providers.get(name)
        if provider is None:
            raise ValueError(f"unknown LLM provider: {name!r}")
        if not provider.is_configured():
            raise ValueError(
                f"LLM provider {name!r} is not configured (missing API key/base_url)"
            )
        return provider

    def available(self) -> list[str]:
        return [n for n, p in self._providers.items() if p.is_configured()]
