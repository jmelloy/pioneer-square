"""Common interface every LLM provider adapter implements.

This formalizes the dispatch-by-provider-string logic that historically lived
inline in ``foreman.llm.make_anthropic_client`` (client construction) and the
parallel ``call_anthropic``/``call_openai_compatible`` free functions (request
execution) into a small set of classes with one shared shape: create a
client, run a chat completion (streaming or not), and raise a normalized
error on failure. See ``backend/foreman/llm_providers/__init__.py`` for the
package overview and the concrete adapters
(``anthropic_provider.py``/``bedrock_provider.py``/``openai_provider.py``).

Every response -- whether it came back from the Anthropic Messages API,
Bedrock's Converse API, or an OpenAI-compatible chat-completions endpoint --
is normalized to the Anthropic Messages API shape before it reaches a caller
(each adapter delegates to the translation already implemented in
``foreman.llm``/``foreman.providers.bedrock``), exposed via ``.model_dump()``
on whatever ``chat_completion`` returns. Callers never need a
provider-specific branch to read a response.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any


class LLMProvider(ABC):
    """Adapter over one LLM backend (Anthropic, Amazon Bedrock, an
    OpenAI-compatible endpoint, ...)."""

    name: str

    @abstractmethod
    def create_client(
        self,
        *,
        env: Mapping[str, str],
        model: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Construct and return a client for this provider.

        ``env`` is the effective credential/config source (process
        environment overlaid with any per-guild overrides), not necessarily
        ``os.environ`` itself, so callers can scope credentials per request.
        Provider-specific construction parameters (region, aws_profile,
        api_key, timeout, ...) are accepted via ``**kwargs`` -- see each
        adapter's ``create_client`` docstring for the ones it reads.
        """

    @abstractmethod
    async def chat_completion(
        self,
        client: Any,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Any, str | None]:
        """Execute one non-streaming chat completion.

        Returns ``(response, request_id)``. ``response`` supports
        ``.model_dump()``, returning a plain dict in the Anthropic Messages
        API response shape (content blocks, stop_reason, usage, ...).
        Raises a ``foreman.llm_providers.errors.ProviderError`` on failure.
        """

    @abstractmethod
    def stream_chat_completion(
        self,
        client: Any,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream one chat completion, yielding incremental text chunks.

        A plain (non-async-generator) method so a provider that can't stream
        (e.g. a native Bedrock foundation model, see ``bedrock_provider.py``)
        can raise ``NotImplementedError`` immediately, before the caller ever
        starts iterating -- an ``async def ...: yield`` body would otherwise
        defer that raise until the first ``__anext__()``.
        """
