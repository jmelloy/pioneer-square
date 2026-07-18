"""Normalized error hierarchy for LLM provider adapters.

Each adapter's ``chat_completion``/``stream_chat_completion`` wraps the
underlying SDK/HTTP call and re-raises failures as one of the types below, so
callers written against the new provider interface can handle "auth failed" /
"rate limited" / "misconfigured" the same way regardless of which provider
actually served (or failed to serve) the request.

This is additive: the legacy free functions in ``foreman.llm``
(``call_anthropic``, ``call_openai_compatible``, ``make_anthropic_client``)
keep raising their native SDK/httpx exceptions unchanged, since existing
callers and tests depend on that. ``BedrockModelNotConfiguredError`` in
``foreman.llm`` also keeps its historical ``ValueError`` ancestry for the same
reason -- it is not part of this hierarchy.
"""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """Base class for normalized provider failures."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retryable: bool = False,
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.original = original


class ProviderConfigError(ProviderError):
    """The provider is missing required configuration (model, base_url, credentials)."""


class ProviderAuthError(ProviderError):
    """Authentication/authorization failure (401/403-equivalent)."""


class ProviderRateLimitError(ProviderError):
    """Rate limited (429-equivalent). Always retryable."""


class ProviderAPIError(ProviderError):
    """Any other non-2xx / SDK-level failure."""


def normalize_error(exc: Exception, *, provider: str) -> ProviderError:
    """Map a provider-native exception to the common ``ProviderError`` hierarchy.

    Recognizes the Anthropic SDK's exception types (if installed), httpx
    status/transport errors, and botocore ``ClientError`` (if installed) --
    covering all three adapters in this package -- and falls back to a bare
    ``ProviderAPIError`` for anything else so callers always get a
    ``ProviderError`` regardless of which transport actually failed.
    """
    if isinstance(exc, ProviderError):
        return exc

    try:
        import anthropic as _anthropic_mod
    except ImportError:
        _anthropic_mod = None  # type: ignore[assignment]

    if _anthropic_mod is not None:
        if isinstance(exc, _anthropic_mod.AuthenticationError):
            return ProviderAuthError(str(exc), provider=provider, status_code=401, original=exc)
        if isinstance(exc, _anthropic_mod.RateLimitError):
            return ProviderRateLimitError(
                str(exc), provider=provider, status_code=429, retryable=True, original=exc
            )
        if isinstance(exc, _anthropic_mod.APIStatusError):
            status = getattr(exc, "status_code", None)
            return ProviderAPIError(
                str(exc),
                provider=provider,
                status_code=status,
                retryable=bool(status and status >= 500),
                original=exc,
            )
        if isinstance(exc, _anthropic_mod.APIConnectionError):
            return ProviderAPIError(str(exc), provider=provider, retryable=True, original=exc)

    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return ProviderAuthError(str(exc), provider=provider, status_code=status, original=exc)
        if status == 429:
            return ProviderRateLimitError(
                str(exc), provider=provider, status_code=status, retryable=True, original=exc
            )
        return ProviderAPIError(
            str(exc), provider=provider, status_code=status, retryable=status >= 500, original=exc
        )
    if isinstance(exc, httpx.HTTPError):
        return ProviderAPIError(str(exc), provider=provider, retryable=True, original=exc)

    try:
        from botocore.exceptions import ClientError as _BotoClientError
    except ImportError:
        _BotoClientError = None  # type: ignore[assignment]

    if _BotoClientError is not None and isinstance(exc, _BotoClientError):
        response: dict[str, Any] = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if code in ("ThrottlingException", "TooManyRequestsException"):
            return ProviderRateLimitError(
                str(exc), provider=provider, status_code=status, retryable=True, original=exc
            )
        if code in (
            "AccessDeniedException",
            "UnrecognizedClientException",
            "UnauthorizedException",
        ):
            return ProviderAuthError(str(exc), provider=provider, status_code=status, original=exc)
        return ProviderAPIError(
            str(exc),
            provider=provider,
            status_code=status,
            retryable=bool(status and status >= 500),
            original=exc,
        )

    return ProviderAPIError(str(exc), provider=provider, original=exc)
