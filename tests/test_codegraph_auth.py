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


# --- recent_changes must not name a window it did not deliver ---------------


def _load_codegraph_server():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "services" / "codegraph" / "server.py"
    spec = importlib.util.spec_from_file_location("hlt_codegraph_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recent_changes_reports_what_it_actually_covered(tmp_path, monkeypatch):
    """It asked for 21 days, got the newest 25 entries, and said "since 7-17".

    Cleo requested three weeks to explain the product, was handed three days,
    and the auth change she needed sat outside what she received. A tool that
    names a window it did not deliver makes its caller confidently wrong.
    """
    server = _load_codegraph_server()

    repo = tmp_path / "nursing-mastery"
    repo.mkdir()
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc)
    # 40 entries over 8 days — more than any sane `limit`
    lines = []
    for i in range(40):
        day = (today - timedelta(days=i // 5)).strftime("%Y-%m-%d")
        lines.append(f"## {day} - entry {i}\n\n- body {i}\n")
    (repo / "CHANGELOG.md").write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(server, "_repo_readiness", lambda _repo: {})

    import json

    payload = json.loads(server.recent_changes("nursing-mastery", days=21, limit=10))

    assert payload["entries_returned"] == 10
    assert payload["entries_in_window"] == 40
    assert payload["entries_omitted"] == 30
    assert payload["complete"] is False
    assert "Do NOT describe the bodies you happen to have as the full period" in (
        payload["truncation_note"]
    )
    # and it must say what it really covered, not just what was asked for
    assert payload["covers"]["to"] >= payload["covers"]["from"]


def test_a_complete_window_says_so(tmp_path, monkeypatch):
    server = _load_codegraph_server()
    repo = tmp_path / "nursing-mastery"
    repo.mkdir()
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (repo / "CHANGELOG.md").write_text(f"## {day} - only entry\n\n- short body\n", encoding="utf-8")

    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(server, "_repo_readiness", lambda _repo: {})

    import json

    payload = json.loads(server.recent_changes("nursing-mastery", days=21))
    assert payload["complete"] is True
    assert payload["truncation_note"] is None


def test_the_index_is_always_complete_even_when_bodies_are_not(tmp_path, monkeypatch):
    """A 340KB fortnight cannot be read in one pass, so raising caps only moves
    the wall. The index — dates and titles for the WHOLE window — is what lets a
    reader see the period and choose, instead of taking the newest slice.

    This is the miss it exists to prevent: an agent asked for 14 days, read 3,
    and never mentioned that sign-in had moved eight days earlier.
    """
    server = _load_codegraph_server()
    repo = tmp_path / "nursing-mastery"
    repo.mkdir()
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc)
    lines = []
    for i in range(40):
        day = (today - timedelta(days=i // 4)).strftime("%Y-%m-%d")
        lines.append(f"## {day} - entry {i}\n\n- body {i}\n")
    (repo / "CHANGELOG.md").write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(server, "_repo_readiness", lambda _repo: {})

    import json

    payload = json.loads(server.recent_changes("nursing-mastery", days=21, limit=5))

    assert len(payload["index"]) == 40, "every headline in the window must be visible"
    assert payload["index_complete"] is True
    assert payload["entries_returned"] == 5
    assert "Scan the index" in payload["truncation_note"]


def test_specific_dates_can_be_fetched_after_scanning_the_index(tmp_path, monkeypatch):
    """The second call: having seen the headlines, pull only what matters."""
    server = _load_codegraph_server()
    repo = tmp_path / "nursing-mastery"
    repo.mkdir()
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc)
    older = (today - timedelta(days=8)).strftime("%Y-%m-%d")
    newer = today.strftime("%Y-%m-%d")
    (repo / "CHANGELOG.md").write_text(
        f"## {newer} - a mobile pass\n\n- tap targets\n\n"
        f"## {older} - sign in gets its front door\n\n- the auth change\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(server, "_repo_readiness", lambda _repo: {})

    import json

    payload = json.loads(server.recent_changes("nursing-mastery", days=21, dates=older))

    assert [e["date"] for e in payload["entries"]] == [older]
    assert "the auth change" in payload["entries"][0]["body"]
    # and the index still shows both, so nothing is hidden by asking narrowly
    assert len(payload["index"]) == 2
