"""Explicit Vercel Gateway transport shared by chat and embeddings."""

import os
from typing import Any


GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"


def gateway_client_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """Use only Gateway authentication, without an SDK's direct-provider defaults."""
    reserved = {
        "api_key", "openai_api_key", "base_url", "openai_api_base",
        "client", "async_client", "root_client", "root_async_client",
        "http_client", "http_async_client", "openai_proxy",
        "default_headers", "default_query",
        "organization", "openai_organization",
    }
    if reserved.intersection(options):
        raise ValueError("Vercel Gateway authentication and endpoint cannot be overridden")
    key = os.environ.get("AI_GATEWAY_API_KEY", "").strip()
    if not key:
        raise ValueError("AI_GATEWAY_API_KEY is required for the vercel provider")
    return {
        **options,
        "openai_api_key": key,
        "openai_api_base": GATEWAY_BASE_URL,
    }
