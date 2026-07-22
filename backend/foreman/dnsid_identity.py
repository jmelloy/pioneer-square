"""Configured DNSid identity and RFC 9421 helpers for Pioneer A2A traffic."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

DNSID_A2A_EXTENSION_URI = (
    "https://example-provider.example/a2a/extensions/dnsid-http-message-signatures/v1"
)
DNSID_A2A_SIGNATURE_TAG = "a2a-dnsid-http-sig-v1"
A2A_VERSION = "1.0"

REQUIRED_SIGNATURE_COMPONENTS = [
    "@method",
    "@authority",
    "@target-uri",
    "content-type",
    "content-digest",
    "a2a-version",
    "a2a-extensions",
]


@dataclass(frozen=True)
class DNSidRuntime:
    """One CLI-provisioned DNSid principal mapped to one Pioneer guild."""

    guild_slug: str
    public_origin: str
    allowlist: frozenset[str]
    manager: object
    http_signatures: object

    @property
    def domain(self) -> str:
        return self.manager.protocol_config.domain  # type: ignore[attr-defined]

    def public_url(self, path: str, query: str = "") -> str:
        suffix = f"?{query}" if query else ""
        return f"{self.public_origin}{path}{suffix}"

    def verify_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> object:
        from dnsid.models import HttpRequest, HttpVerificationOptions

        canonical_headers = {key.lower(): value for key, value in headers.items()}
        # RFC 9421 derives @authority from Host when present. Never let a reverse
        # proxy's internal Host override the configured public A2A authority.
        canonical_headers["host"] = urlsplit(url).netloc
        return self.http_signatures.verify_signed_http_request(
            HttpRequest(method=method, url=url, headers=canonical_headers, body=body),
            HttpVerificationOptions(
                required_components=REQUIRED_SIGNATURE_COMPONENTS,
                required_tag=DNSID_A2A_SIGNATURE_TAG,
            ),
        )


def is_dnsid_a2a_enabled() -> bool:
    return bool(os.environ.get("DNSID_CONFIG_DIR", "").strip())


def _load_allowlist() -> frozenset[str]:
    path = os.environ.get("PIONEER_DNSID_ALLOWLIST_FILE", "").strip()
    raw: object
    if path:
        raw = json.loads(Path(path).read_text())
    else:
        value = os.environ.get("PIONEER_DNSID_ALLOWLIST", "[]").strip()
        raw = json.loads(value or "[]")
    if isinstance(raw, dict):
        raw = raw.get("allow", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("DNSid allowlist must be a JSON string array or an object with 'allow'")
    return frozenset(item.rstrip(".").lower() for item in raw if item.strip())


def _c2sp_log_registry(protocol_config: object, transport_config: object) -> object | None:
    log_ref = protocol_config.log_ref  # type: ignore[attr-defined]
    if not log_ref.startswith("c2sp-tlog:"):
        return None

    import httpx
    from dnsid.c2sp_tlog import (
        C2spTlogReaderOptions,
        parse_c2sp_policy_file,
        parse_c2sp_tlog_lr,
        register_c2sp_tlog,
    )
    from dnsid.manager import _make_sdk_transport
    from dnsid.registry import LogRegistry

    parsed = parse_c2sp_tlog_lr(log_ref)
    client = httpx.Client(transport=_make_sdk_transport(transport_config), timeout=10.0)

    def transport(url: str) -> bytes:
        response = client.get(url)
        response.raise_for_status()
        return response.content

    response = client.get(f"{parsed.log_prefix}/dnsid-policy")
    response.raise_for_status()
    policy = parse_c2sp_policy_file(response.text)
    policy.scope = parsed.scope
    if parsed.origin not in policy.origins:
        raise RuntimeError(f"C2SP policy does not cover {parsed.origin}")
    registry = LogRegistry()
    register_c2sp_tlog(
        registry,
        C2spTlogReaderOptions(
            policy=policy,
            transport=transport,
            max_clock_skew_ms=30_000,
            checkpoint_freshness_ms=5 * 60_000,
        ),
    )
    return registry


@lru_cache(maxsize=1)
def get_dnsid_runtime() -> DNSidRuntime | None:
    """Load the identity provisioned by the main DNSid CLI, if configured."""
    config_dir = os.environ.get("DNSID_CONFIG_DIR", "").strip()
    if not config_dir:
        return None

    from dnsid import IdentityManager, IdentityManagerDependencies, LocalKeyProvider
    from dnsid.cli_config import config_from_cli_directory
    from dnsid.http_signatures import HttpSignatureProfile

    cli_result = config_from_cli_directory(config_dir)
    protocol_config = cli_result.protocol_config
    transport_config = cli_result.transport_config
    if os.environ.get("DNSID_DOMAIN", "").strip():
        from dnsid.environment import config_from_environment

        env_result = config_from_environment(dict(os.environ))
        if env_result.protocol_config.domain != cli_result.protocol_config.domain:
            raise ValueError("DNSID_DOMAIN does not match the DNSID_CONFIG_DIR identity")
        protocol_config = env_result.protocol_config
        transport_config = env_result.transport_config
    if os.environ.get("DNSID_TESTNET_ZONE", "").strip():
        transport_config.allow_private_addresses = True

    registry = _c2sp_log_registry(protocol_config, transport_config)
    manager = IdentityManager(
        protocol_config,
        LocalKeyProvider.from_cli_directory(cli_result.key_directory),
        deps=IdentityManagerDependencies(
            transport_config=transport_config,
            log_registry=registry,
        ),
    )
    guild_slug = os.environ.get("PIONEER_DNSID_GUILD", "").strip()
    if not guild_slug:
        guild_slug = protocol_config.domain.split(".", 1)[0]
    public_origin = os.environ.get("PIONEER_DNSID_PUBLIC_URL", "").strip().rstrip("/")
    if not public_origin:
        public_origin = f"https://{protocol_config.domain}"
    parsed_origin = urlsplit(public_origin)
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc:
        raise ValueError("PIONEER_DNSID_PUBLIC_URL must be an absolute HTTP(S) origin")
    if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
        raise ValueError("PIONEER_DNSID_PUBLIC_URL must not include a path, query, or fragment")

    return DNSidRuntime(
        guild_slug=guild_slug,
        public_origin=public_origin,
        allowlist=_load_allowlist(),
        manager=manager,
        http_signatures=HttpSignatureProfile.from_identity_manager(manager),
    )


def clear_dnsid_runtime_cache() -> None:
    """Test helper for environment-driven configuration."""
    get_dnsid_runtime.cache_clear()
