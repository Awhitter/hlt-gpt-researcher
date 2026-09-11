"""Exercise Astra's real pinned catalog/effort/context methods with offline IO.

These gates run inside the Linux image after applying the HLT overlay. Only
network and unrelated provider loading are replaced; the modified methods and
the existing account-scoped cache/provenance implementation execute unchanged.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


def _module(path: Path, name: str, names: set[str] | None = None) -> ModuleType:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if names is not None:
        selected = []
        found = set()
        for node in tree.body:
            assigned = set()
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                assigned.add(node.name)
            elif isinstance(node, ast.Assign):
                assigned.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                assigned.add(node.target.id)
            if assigned & names:
                selected.append(node)
                found.update(assigned & names)
        assert found == names, f"upstream contract moved: {sorted(names - found)}"
        tree = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
            type_ignores=[],
        )
        ast.fix_missing_locations(tree)
    module = ModuleType(name)
    module.__dict__.update(
        base64=base64, hashlib=hashlib, json=json, re=re,
        logger=logging.getLogger(name),
        time=SimpleNamespace(time=lambda: 10_000.0),
    )
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


def _method(path: Path, class_name: str, method_name: str, namespace: dict):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    method = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method,
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[method_name]


def _assert_responses_boundary(hermes_root: Path, reasoning, metadata, check) -> None:
    """Execute the actual full request builder; no production test hook.

    Only unrelated transcript conversion, cache-key generation, and endpoint
    loading are stubbed. ProviderProfile's real default vocabulary method,
    ResponsesApiTransport.build_kwargs, and all Astra normalization run intact.
    """
    transport_path = hermes_root / "agent/transports/codex.py"
    helpers = _module(transport_path, "proof_transport_helpers", {
        "_profile_declared_efforts", "_native_compaction_active", "_bounded_prompt_cache_key",
    })
    profile_efforts = _method(hermes_root / "providers/base.py", "ProviderProfile",
                              "supported_reasoning_efforts", {})
    provider = SimpleNamespace(supported_reasoning_efforts=lambda model: profile_efforts(None, model))
    plugin_path = hermes_root / "plugins/model-providers/openai-codex/__init__.py"
    plugin = ast.parse(plugin_path.read_text(encoding="utf-8"))
    registrations = [n for n in ast.walk(plugin) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "ProviderProfile"]
    check("Codex uses the inherited profile vocabulary hook",
          len(registrations) == 1 and not any(isinstance(n, ast.ClassDef) for n in plugin.body)
          and not any(k.arg == "supported_reasoning_efforts" for k in registrations[0].keywords)
          and provider.supported_reasoning_efforts("gpt-6-astra") is None)
    providers = ModuleType("providers")
    providers.get_provider_profile = lambda name: provider if name == "openai-codex" else None
    adapter = ModuleType("agent.codex_responses_adapter")
    adapter._responses_tools = lambda tools: []
    adapter._chat_messages_to_responses_input = lambda messages, **kwargs: messages
    run_agent = ModuleType("run_agent")
    run_agent.DEFAULT_AGENT_IDENTITY = "offline fixture"
    metadata._infer_provider_from_url = lambda url: "openai-codex"
    namespace = {
        "_is_azure_foundry_responses": lambda params: False,
        "_native_compaction_active": helpers._native_compaction_active,
        "_profile_declared_efforts": helpers._profile_declared_efforts,
        "codex_supported_efforts": reasoning.codex_supported_efforts,
        "clamp_effort": reasoning.clamp_effort,
        "_cache_scope_from_session_id": lambda session: "",
        "_content_cache_key": lambda *args: None,
        "_default_prompt_cache_retention_for_request": lambda *args: None,
        "_bounded_prompt_cache_key": helpers._bounded_prompt_cache_key,
    }
    build = _method(transport_path, "ResponsesApiTransport", "build_kwargs", namespace)
    transport = SimpleNamespace(_resolve_issuer_kind=lambda params: "codex")
    with patch.dict(sys.modules, {"providers": providers,
                                 "agent.codex_responses_adapter": adapter, "run_agent": run_agent}):
        for requested, expected in [("none", "low"), ("minimal", "low"), ("high", "high"), ("max", "max")]:
            wire = build(transport, "gpt-6-astra-900k", [], provider="openai-codex",
                         base_url="https://chatgpt.com/backend-api/codex",
                         is_codex_backend=True, instructions="offline fixture",
                         reasoning_config={"effort": requested}, max_tokens=32768, timeout=15)
            check(f"actual Responses wire normalizes {requested} and strips picker alias",
                  wire["model"] == "gpt-6-astra" and wire["reasoning"]["effort"] == expected)
            check(f"{requested} request preserves Codex protocol and timeout",
                  "max_output_tokens" not in wire and wire["timeout"] == 15.0 and wire["store"] is False)


def assert_codex_astra(hermes_root: Path) -> None:
    reasoning = _module(hermes_root / "agent/reasoning_effort.py", "agent.reasoning_effort")
    context_names = {
        "DEFAULT_CONTEXT_LENGTHS", "_CODEX_OAUTH_CONTEXT_FALLBACK",
        "_CODEX_OAUTH_VERIFIED_ABOVE_ADVERTISED_PREFIXES",
        "_CODEX_OAUTH_VERIFIED_ABOVE_ADVERTISED_EXACT",
        "_CODEX_OAUTH_STALE_ADVERTISED_CTX", "CODEX_CONTEXT_VARIANT_SUFFIX",
        "_CODEX_900K_ELIGIBLE_BASES", "_CODEX_900K_SNAPSHOT_BASES",
        "_CODEX_900K_SNAPSHOT_RE", "_codex_oauth_context_cache",
        "_CODEX_OAUTH_CONTEXT_CACHE_TTL", "_bare_codex_slug",
        "is_codex_900k_base", "is_codex_context_variant",
        "strip_codex_context_variant_suffix", "has_codex_context_variant",
        "_verified_codex_ctx_for_slug", "_codex_oauth_token_fingerprint",
        "_extract_chatgpt_account_id", "_fetch_codex_oauth_context_lengths_with_source",
        "_resolve_codex_oauth_context_length_with_source", "_strip_provider_prefix",
    }
    metadata = _module(hermes_root / "agent/model_metadata.py", "agent.model_metadata", context_names)
    catalog = _module(hermes_root / "hermes_cli/codex_models.py", "proof_codex_models")
    package = ModuleType("agent")
    package.__path__ = []
    package.reasoning_effort = reasoning
    package.model_metadata = metadata
    cases = []

    def check(name: str, condition: bool) -> None:
        assert condition, name
        cases.append(name)

    # Synthetic account fixtures only. No stored login or provider request is used.
    def token(account: str) -> str:
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account}}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"fixture.{body}.unsigned"

    account_a, account_b = token("fixture-account-a"), token("fixture-account-b")
    state = {"status": 200, "models": [
        {"slug": "gpt-6-astra", "priority": 0, "context_window": 272_000, "supported_in_api": False},
        {"slug": "gpt-5.6-sol", "priority": 1, "context_window": 272_000},
    ]}
    calls = []

    def get(url, *, headers, **kwargs):
        assert url == "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
        assert headers["ChatGPT-Account-Id"] in {"fixture-account-a", "fixture-account-b"}
        calls.append({"account": headers["ChatGPT-Account-Id"], "timeout": kwargs.get("timeout")})
        return SimpleNamespace(
            status_code=state["status"], json=lambda: {"models": list(state["models"])}
        )

    fake_httpx = ModuleType("httpx")
    fake_httpx.get = get
    metadata.requests = SimpleNamespace(get=get)
    metadata._ensure_requests = lambda: None
    metadata._resolve_requests_verify = lambda: True
    with patch.dict(sys.modules, {
        "agent": package, "agent.reasoning_effort": reasoning,
        "agent.model_metadata": metadata, "httpx": fake_httpx,
    }), TemporaryDirectory(prefix="hermes-astra-offline-") as temporary:
        with patch.dict(os.environ, {"CODEX_HOME": temporary}):
            home = Path(temporary)
            (home / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
            (home / "models_cache.json").write_text(json.dumps({"models": [
                {"slug": "openai/gpt-6-astra-900k"}, {"slug": "gpt-5.6-sol"}
            ]}), encoding="utf-8")

            live = catalog.get_codex_model_ids(access_token=account_a)
            check("live account can advertise Astra despite public-API flag false", "gpt-6-astra" in live)
            check("large context is a separate explicit picker option", "gpt-6-astra-900k" in live)
            check("base precedes large-context variant", live.index("gpt-6-astra") < live.index("gpt-6-astra-900k"))
            offline = catalog.get_codex_model_ids()
            check("stale config and cache are not Astra entitlement", not any(reasoning.is_astra_model(m) for m in offline))
            check("offline Sol recovery remains available", "gpt-5.6-sol" in offline)
            state["models"] = []
            empty = catalog.get_codex_model_ids(access_token=account_a)
            check("empty live catalog cannot promote cached Astra", not any(reasoning.is_astra_model(m) for m in empty))
            state["status"] = 403
            denied = catalog.get_codex_model_ids(access_token=account_a)
            check("denied catalog cannot promote cached Astra", not any(reasoning.is_astra_model(m) for m in denied))
            state["status"] = 200
            state["models"] = [{"slug": "gpt-6-astra", "visibility": "hidden"}]
            hidden = catalog.get_codex_model_ids(access_token=account_a)
            check("hidden Astra is not selectable", not any(reasoning.is_astra_model(m) for m in hidden))

        for model in ["gpt-6-astra", "openai/gpt-6-astra", "gpt-6-astra-900k"]:
            supported = reasoning.codex_supported_efforts(model)
            check(f"{model} has stable Astra effort vocabulary",
                  supported == ("low", "medium", "high", "xhigh", "max"))
            check(f"{model} cannot disable reasoning",
                  reasoning.clamp_effort("none", supported) == "low")
            check(f"{model} minimal uses provider floor",
                  reasoning.clamp_effort("minimal", supported) == "low")
        supported = reasoning.codex_supported_efforts("gpt-6-astra")
        check("high and max preserve explicit caller choice",
              all(reasoning.clamp_effort(e, supported) == e for e in ("high", "max")))
        check("unset effort stays unset", reasoning.clamp_effort(None, supported) is None)
        check("Sol reasoning vocabulary preserved",
              reasoning.codex_supported_efforts("gpt-5.6-sol") == reasoning.CODEX_GPT56_EFFORTS)
        check("unknown future Astra suffix is not assumed supported",
              not reasoning.is_astra_model("gpt-6-astra-unannounced"))

        resolve = metadata._resolve_codex_oauth_context_length_with_source
        check("Codex default stays 272k with conservative provenance",
              resolve("gpt-6-astra") == (272_000, "fallback"))
        check("direct API context remains a distinct source",
              metadata.DEFAULT_CONTEXT_LENGTHS["gpt-6-astra"] == 1_050_000)
        check("900k requires explicit opt-in",
              resolve("openai/gpt-6-astra-900k") == (900_000, "fallback"))
        check("picker alias is stripped before transport",
              metadata.strip_codex_context_variant_suffix("gpt-6-astra-900k") == "gpt-6-astra")

        state["models"] = [{"slug": "gpt-6-astra", "context_window": 272_000}]
        call_count = len(calls)
        check("first account catalog is fresh evidence",
              resolve("gpt-6-astra", account_a) == (272_000, "live"))
        check("same-account cache does not masquerade as fresh evidence",
              resolve("gpt-6-astra", account_a) == (272_000, "memory") and len(calls) == call_count + 1)
        state["models"] = [{"slug": "gpt-6-astra", "context_window": 400_000}]
        check("other account cannot inherit prior context entitlement",
              resolve("gpt-6-astra", account_b) == (400_000, "live"))
        check("actual changed catalog limit wins over 900k shortcut",
              resolve("gpt-6-astra-900k", account_b) == (400_000, "memory"))
        fingerprint = metadata._codex_oauth_token_fingerprint(account_a)
        metadata._codex_oauth_context_cache[fingerprint] = ({"gpt-6-astra": 272_000}, 0.0)
        check("expired account cache requires fresh catalog evidence",
              resolve("gpt-6-astra", account_a) == (400_000, "live"))
        check("raw credentials never become cache keys",
              all(key not in {account_a, account_b} and len(key) == 16
                  for key in metadata._codex_oauth_context_cache))
        _assert_responses_boundary(hermes_root, reasoning, metadata, check)

    print(json.dumps({"status": "PASS", "cases": len(cases),
                      "proof": "offline actual patched catalog/effort/context methods",
                      "providerCalls": 0, "caseNames": cases}, sort_keys=True))


if __name__ == "__main__":
    assert_codex_astra(Path(sys.argv[1]))
