"""Unified LLM provider shim for the foreman (issue #940).

A formal, class-based provider abstraction that replaces the
dispatch-by-provider-string logic that used to live entirely inline in
``foreman.llm.make_anthropic_client``. Three adapters are provided, one per
supported provider:

- ``AnthropicProvider``  -- the direct api.anthropic.com Messages API.
- ``BedrockProvider``    -- Amazon Bedrock: Claude-on-Bedrock (Messages API via
  ``AsyncAnthropicBedrock``) and native foundation models like Amazon Nova /
  Kimi K2 (Converse API, see ``backend/foreman/providers/bedrock.py``).
- ``OpenAIProvider``     -- any OpenAI-compatible ``/chat/completions``
  endpoint (Ollama, vLLM, LM Studio, ...).

Each adapter implements the common ``LLMProvider`` interface
(``create_client``, ``chat_completion``, ``stream_chat_completion``) and
raises the normalized ``ProviderError`` hierarchy from ``errors.py`` on
failure. Use ``get_provider(name)`` to select one -- by an explicit name, a
guild's configured ``provider`` field, or the ``FOREMAN_PROVIDER`` environment
variable -- so switching providers is a configuration change, not a code
change.

Design note: the adapters delegate their actual request/response translation
to the existing, exhaustively-tested functions in ``foreman.llm``
(``call_anthropic``, ``call_openai_compatible``) and ``foreman.providers.bedrock``
rather than reimplementing them. Those functions remain the source of truth
for wire-format details; this package is the formal interface layered on top,
consumed by ``foreman.llm.make_anthropic_client`` (for client construction)
and by the foreman runners (embedded and standalone proxy) for making calls.
"""

from __future__ import annotations

from .anthropic_provider import AnthropicProvider
from .base import LLMProvider
from .bedrock_provider import BedrockProvider
from .errors import (
    ProviderAPIError,
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    normalize_error,
)
from .openai_provider import OpenAIProvider
from .registry import DIRECT_SDK_PROVIDERS, available_providers, get_provider

__all__ = [
    "DIRECT_SDK_PROVIDERS",
    "AnthropicProvider",
    "BedrockProvider",
    "LLMProvider",
    "OpenAIProvider",
    "ProviderAPIError",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderRateLimitError",
    "available_providers",
    "get_provider",
    "normalize_error",
]
