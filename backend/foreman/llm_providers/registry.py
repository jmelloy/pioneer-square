"""Provider registry: config-driven selection of an ``LLMProvider`` implementation.

The provider is selected purely by name (``FOREMAN_PROVIDER`` env var, or a
guild's configured ``provider`` field) -- switching providers never requires a
code change, only a different config value.
"""

from __future__ import annotations

import os

from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .bedrock_provider import BedrockProvider
from .errors import ProviderConfigError
from .openai_provider import OpenAIProvider

# Providers reachable without a connected foreman API proxy -- i.e. ones the
# embedded foreman can call directly. Matches (and is the source of truth
# for) foreman.llm.ANTHROPIC_SDK_PROVIDERS, the historical name kept for
# backward compatibility.
DIRECT_SDK_PROVIDERS = frozenset({"anthropic", "bedrock"})

_REGISTRY: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "bedrock": BedrockProvider,
    "openai": OpenAIProvider,
}


def get_provider(provider: str | None = None) -> LLMProvider:
    """Return an ``LLMProvider`` instance for *provider*.

    Falls back to the ``FOREMAN_PROVIDER`` environment variable, then
    ``"anthropic"``, reading ``os.environ`` dynamically (like
    ``foreman.llm.get_foreman_model``) so a ``FOREMAN_PROVIDER`` set after
    import -- or by a test -- is respected. Raises ``ProviderConfigError`` for
    an unrecognized provider name.
    """
    resolved = (provider or os.environ.get("FOREMAN_PROVIDER", "anthropic")).lower()
    try:
        provider_cls = _REGISTRY[resolved]
    except KeyError:
        raise ProviderConfigError(
            f"Unknown provider {resolved!r}; supported providers are {sorted(_REGISTRY)}",
            provider=resolved,
        ) from None
    return provider_cls()


def available_providers() -> list[str]:
    """Return the list of registered provider names, for diagnostics/UI."""
    return sorted(_REGISTRY)
