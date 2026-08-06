"""Perimeter tests for the codegraph sidecar.

`hlt-codegraph` exposes `gitnexus query` over five private repositories on a
service whose Render ipAllowList is 0.0.0.0/0. Its auth middleware used to
return `await call_next(request)` when the token env var was empty — failing
*open* — and its public /health returned repo slugs, branches and commit SHAs
to anyone who knew the hostname. Both are pinned here.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "codegraph"

pytest.importorskip("mcp.server.fastmcp")


def _load(module_name: str, path: Path):
    """Load a service module by path, WITHOUT touching sys.path.

    `services/codegraph/server.py` would otherwise shadow the `server` package
    that `backend.server.app` imports, breaking every test module collected
    after this one in the same process.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load("hlt_codegraph_server", SERVICE_DIR / "server.py")

TOKEN = "s3cret-token"


def test_missing_token_fails_closed():
    """The regression that matters: no token must not mean no auth."""
    assert server.auth_failure("/mcp", f"Bearer {TOKEN}", None) == (
        503,
        "Service is not configured with an auth token",
    )
    assert server.auth_failure("/mcp", "", "") == (
        503,
        "Service is not configured with an auth token",
    )


def test_correct_token_is_accepted():
    assert server.auth_failure("/mcp", f"Bearer {TOKEN}", TOKEN) is None
    assert server.auth_failure("/mcp", f"bearer {TOKEN}", TOKEN) is None


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer wrong", f"Basic {TOKEN}", TOKEN, "Bearer ", "Bearer  "],
)
def test_bad_credentials_are_rejected(authorization):
    assert server.auth_failure("/mcp", authorization, TOKEN) == (401, "Unauthorized")


def test_non_ascii_token_returns_401_not_500():
    """compare_digest raises TypeError on non-ASCII str; we encode both sides."""
    assert server.auth_failure("/mcp", "Bearer pÄssword", TOKEN) == (401, "Unauthorized")


def test_only_health_is_public():
    assert server.auth_failure("/health", "", TOKEN) is None
    # "/" is a live transport path on a streamable-HTTP MCP app, not a landing page.
    assert server.auth_failure("/", "", TOKEN) == (401, "Unauthorized")
    assert server.auth_failure("/readiness", "", TOKEN) == (401, "Unauthorized")


def test_public_health_names_no_repositories():
    """Index truth belongs on /readiness, behind the token.

    Asserts on the actual response body rather than the source text — the
    first version of this test read the function source and tripped over the
    word "repositories" in its own docstring.
    """
    payload = json.loads(asyncio.run(server.health_check(None)).body)

    assert payload == {"status": "ok", "service": "hlt-codegraph"}
    flat = json.dumps(payload)
    for repo in server.REPO_GITHUB:
        assert repo not in flat, f"public /health names the {repo} index"
