"""Explicit Vercel Gateway transport shared by chat and embeddings."""

import os
from typing import Any


GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"


def validate_gateway_options(options: dict[str, Any]) -> None:
    """Reject transport overrides at construction and per-request boundaries."""
    reserved = {
        "api_key", "openai_api_key", "base_url", "openai_api_base",
        "client", "async_client", "root_client", "root_async_client",
        "http_client", "http_async_client", "openai_proxy",
        "default_headers", "default_query", "extra_headers", "extra_query",
        "organization", "openai_organization", "byok",
    }
    if reserved.intersection(options):
        raise ValueError("Vercel Gateway authentication and endpoint cannot be overridden")
    # SDK defaults and extra_body merge into request options. Inspect only
    # transport envelopes, not arbitrary user content or output JSON schemas.
    for envelope in ("model_kwargs", "extra_body", "providerOptions", "gateway"):
        if envelope in options and options[envelope] is not None:
            if not isinstance(options[envelope], dict):
                raise ValueError(f"Vercel Gateway {envelope} must be an object")
            validate_gateway_options(options[envelope])


def gateway_client_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """Use only Gateway authentication, without an SDK's direct-provider defaults."""
    validate_gateway_options(options)
    key = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
    if not key:
        raise ValueError("AI_GATEWAY_API_KEY is required for the vercel provider")
    return {
        **options,
        "openai_api_key": key,
        "openai_api_base": GATEWAY_BASE_URL,
    }
