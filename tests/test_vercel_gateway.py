"""No-network contracts for explicit Gateway routing, including SDK defaults."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gpt_researcher.config.config import Config
from gpt_researcher.llm_provider.generic.base import GenericLLMProvider
from gpt_researcher.llm_provider.vercel_gateway import gateway_client_kwargs
from gpt_researcher.memory.embeddings import Memory
from gpt_researcher.utils.llm import create_chat_completion


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "gateway-fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "direct-provider-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://direct.invalid/v1")


def test_gateway_missing_key_never_falls_back_to_openai(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "direct-provider-must-not-be-used")
    with pytest.raises(ValueError, match="AI_GATEWAY_API_KEY is required"):
        gateway_client_kwargs({})


@pytest.mark.parametrize("override", [
    "api_key", "openai_api_key", "base_url", "openai_api_base",
    "client", "async_client", "root_client", "root_async_client",
    "http_client", "http_async_client", "openai_proxy",
    "default_headers", "default_query", "organization", "openai_organization",
])
def test_gateway_rejects_credential_and_endpoint_overrides(gateway_env, override):
    with pytest.raises(ValueError, match="cannot be overridden"):
        gateway_client_kwargs({override: "override-must-not-be-used"})


def test_gateway_config_keeps_complete_provider_model_id():
    assert Config.parse_llm("vercel:openai/example-model") == ("vercel", "openai/example-model")
    assert Config.parse_embedding("vercel:openai/example-embedding") == ("vercel", "openai/example-embedding")


def test_chat_constructor_uses_gateway_key_and_fixed_endpoint(gateway_env):
    with patch("langchain_openai.ChatOpenAI") as constructor:
        GenericLLMProvider.from_provider("vercel", model="openai/example-model", temperature=0)
    assert constructor.call_args.kwargs == {
        "model": "openai/example-model",
        "temperature": 0,
        "openai_api_key": "gateway-fixture",
        "openai_api_base": "https://ai-gateway.vercel.sh/v1",
    }


def test_embeddings_send_text_through_gateway(gateway_env):
    with patch("langchain_openai.OpenAIEmbeddings") as constructor:
        Memory("vercel", "openai/example-embedding", check_embedding_ctx_length=True)
    assert constructor.call_args.kwargs == {
        "model": "openai/example-embedding",
        "check_embedding_ctx_length": False,
        "openai_api_key": "gateway-fixture",
        "openai_api_base": "https://ai-gateway.vercel.sh/v1",
    }


def test_real_sdk_clients_ignore_direct_provider_environment(gateway_env):
    chat = GenericLLMProvider.from_provider("vercel", model="openai/example-model")
    embeddings = Memory("vercel", "openai/example-embedding").get_embeddings()
    assert str(chat.llm.root_client.base_url) == "https://ai-gateway.vercel.sh/v1/"
    assert chat.llm.root_client.api_key == "gateway-fixture"
    assert str(embeddings.client._client.base_url) == "https://ai-gateway.vercel.sh/v1/"
    assert embeddings.client._client.api_key == "gateway-fixture"


@pytest.mark.parametrize("model,reasoning", [("openai/gpt-5", False), ("openai/o3-mini", True)])
def test_gateway_preserves_native_model_capabilities(model, reasoning):
    provider = SimpleNamespace(get_chat_response=AsyncMock(return_value="fixture response"))
    with (
        patch("gpt_researcher.utils.llm.get_llm", return_value=provider) as factory,
        patch("gpt_researcher.utils.llm.observe_langfuse", return_value=nullcontext(None)),
    ):
        result = asyncio.run(create_chat_completion(
            messages=[{"role": "user", "content": "fixture"}],
            model=model, llm_provider="vercel", reasoning_effort="high",
        ))
    assert result == "fixture response"
    assert factory.call_args.args == ("vercel",)
    assert factory.call_args.kwargs["model"] == model
    assert factory.call_args.kwargs["temperature"] is None
    assert factory.call_args.kwargs["max_tokens"] is None
    assert (factory.call_args.kwargs.get("reasoning_effort") == "high") is reasoning
