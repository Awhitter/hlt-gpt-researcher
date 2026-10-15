"""Perimeter tests for the codegraph sidecar.

`hlt-codegraph` exposes `gitnexus query` over five private repositories on a
service whose Render ipAllowList is 0.0.0.0/0. Its auth middleware used to
return `await call_next(request)` when the token env var was empty — failing
*open* — and its public /health returned repo slugs, branches and commit SHAs
to anyone who knew the hostname. Both are pinned here.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
    assert server.auth_failure("/verify-source", "", TOKEN) == (401, "Unauthorized")


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


def test_public_health_says_degraded_after_a_failed_reindex_without_naming_repos(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    status = tmp_path / ".hlt-reindex-status"

    status.write_text("failed 2026-10-14T00:00:00Z 2 of the checkouts did not refresh\n")
    payload = json.loads(asyncio.run(server.health_check(None)).body)
    assert payload["status"] == "degraded"
    flat = json.dumps(payload)
    for repo in server.REPO_GITHUB:
        assert repo not in flat

    status.write_text(f"ok {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
    payload = json.loads(asyncio.run(server.health_check(None)).body)
    assert payload == {"status": "ok", "service": "hlt-codegraph"}


def test_public_health_says_degraded_on_a_full_disk_or_a_stuck_run(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    status = tmp_path / ".hlt-reindex-status"
    status.write_text("ok 2026-10-15T00:00:00Z\n")

    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", str(10**12))
    response = asyncio.run(server.health_check(None))
    payload = json.loads(response.body)
    assert response.status_code == 200  # Render's health check; degraded must not fail deploys
    assert payload["status"] == "degraded"
    assert "disk" in payload["reason"]

    for unparseable in ("", "1G"):
        monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", unparseable)
        assert asyncio.run(server.health_check(None)).status_code == 200

    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    status.write_text("running 2026-01-01T00:00:00Z\n")
    assert json.loads(asyncio.run(server.health_check(None)).body)["status"] == "degraded"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status.write_text(f"running {now}\n")
    assert json.loads(asyncio.run(server.health_check(None)).body)["status"] == "ok"


def _run_reindex(repos: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    # Never invoke an installed graph CLI or load a model during shell proofs.
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    gitnexus = tools / "gitnexus"
    gitnexus.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$GITNEXUS_TEST_CALLS"\n')
    gitnexus.chmod(0o755)
    return subprocess.run(
        ["bash", str(Path(server.__file__).with_name("reindex.sh"))],
        env={
            **os.environ,
            "REPOS_DIR": str(repos),
            "GITNEXUS_HOME": str(tmp_path / "gitnexus"),
            "CODEGRAPH_FORCE_REINDEX": "1",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "GITNEXUS_TEST_CALLS": str(tmp_path / "gitnexus-calls"),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_full_disk_keeps_checkout_and_index_and_leaves_frozen_repos_alone(tmp_path):
    """If cleanup cannot recover headroom, keep the last usable source and graph."""
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    repos = tmp_path / "repos"
    repos.mkdir()
    for slug in ("demo", "frozen"):
        subprocess.run(["git", "clone", "-q", str(upstream), str(repos / slug)], check=True)
        (repos / slug / ".gitnexus").mkdir()
        (repos / slug / ".gitnexus" / "graph.db").write_text("index")
    frozen_ref = repos / "frozen" / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    before_ref = frozen_ref.read_text()
    frozen_lock = repos / "frozen" / ".git" / "index.lock"
    frozen_lock.write_text("operator-owned")
    before_sha = subprocess.check_output(["git", "-C", str(repos / "demo"), "rev-parse", "HEAD"])

    result = _run_reindex(
        repos,
        tmp_path,
        CODEGRAPH_REPOS="demo|example/demo,frozen|example/frozen",
        CODEGRAPH_SOURCE_ONLY_REPOS="demo",
        CODEGRAPH_SKIP_REPOS="frozen",
        CODEGRAPH_MIN_FREE_MB=str(10**12),
    )

    assert result.returncode == 1, result.stderr
    assert "retaining demo's checkout and index" in result.stderr, result.stderr
    assert "source checkout ready" not in result.stdout
    assert subprocess.check_output(["git", "-C", str(repos / "demo"), "rev-parse", "HEAD"]) == before_sha
    assert (repos / "demo" / ".gitnexus" / "graph.db").read_text() == "index"
    assert (repos / "demo" / ".git").is_dir()
    assert (repos / "frozen" / ".git").is_dir()
    assert (repos / "frozen" / ".gitnexus" / "graph.db").read_text() == "index"
    assert frozen_ref.read_text() == before_ref
    assert frozen_lock.read_text() == "operator-owned"
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")


def test_reindex_counts_a_failed_fetch_as_a_failure(tmp_path):
    """On 2026-10-14 a fetch that hit a full disk still logged "checkout ready"."""
    repos = tmp_path / "repos"
    checkout = repos / "demo"
    repos.mkdir()
    _create_source_repo(checkout)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
    )
    result = _run_reindex(
        repos,
        tmp_path,
        CODEGRAPH_REPOS="demo|example/demo",
        CODEGRAPH_SOURCE_ONLY_REPOS="demo",
        CODEGRAPH_MIN_FREE_MB="0",
    )
    assert "FAILED to index demo" in result.stderr
    assert "source checkout ready for demo" not in result.stdout
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")
    assert result.returncode == 1


def test_interrupted_checkout_is_never_deleted_on_retry(tmp_path):
    repos = tmp_path / "repos"
    index = repos / "demo" / ".gitnexus"
    index.mkdir(parents=True)
    (index / "graph.db").write_text("last good index")
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_MIN_FREE_MB="0")
    assert result.returncode == 1
    assert (index / "graph.db").read_text() == "last good index"
    assert "incomplete checkout" in result.stderr


def test_reindex_lock_preserves_the_active_runs_status(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    status = repos / ".hlt-reindex-status"
    status.write_text("running 2026-10-15T00:00:00Z\n")
    with (repos / ".hlt-reindex.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo")
    assert result.returncode == 0
    assert "another reindex is running" in result.stdout
    assert status.read_text() == "running 2026-10-15T00:00:00Z\n"
    assert not (repos / "demo").exists()


def test_reindex_holds_its_lock_after_the_python_helper_exits(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    subprocess.run(["git", "clone", "-q", str(upstream), str(repos / "demo")], check=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    started, release = tmp_path / "fetch-started", tmp_path / "release-fetch"
    real_git = shlex.quote(shutil.which("git"))
    wrapper = tools / "git"
    wrapper.write_text(f'''#!/bin/sh
if [ "$3" = fetch ]; then
  touch {shlex.quote(str(started))}
  while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.05; done
fi
exec {real_git} "$@"
''')
    wrapper.chmod(0o755)
    env = dict(CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SOURCE_ONLY_REPOS="demo", CODEGRAPH_MIN_FREE_MB="0")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_run_reindex, repos, tmp_path, **env)
        try:
            deadline = time.monotonic() + 10
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert started.exists(), "first run never reached fetch"
            status = (repos / ".hlt-reindex-status").read_text()
            second = _run_reindex(repos, tmp_path, **env)
            assert second.returncode == 0
            assert "another reindex is running" in second.stdout
            assert (repos / ".hlt-reindex-status").read_text() == status
        finally:
            release.touch()
        assert first.result(timeout=15).returncode == 0


def test_frozen_failure_is_not_relabelled_as_ok(tmp_path):
    repos = tmp_path / "repos"
    checkout = repos / "demo"
    checkout.mkdir(parents=True)
    error = checkout / ".hlt-index-error"
    error.write_text("previous failure")
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SKIP_REPOS="demo")
    assert result.returncode == 1
    assert error.read_text() == "previous failure"
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")


def test_recent_stamp_does_not_hide_a_failed_refresh(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    checkout = repos / "demo"
    _create_source_repo(checkout)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(tmp_path / "missing.git")], check=True)
    (checkout / ".hlt-source-ready-at").write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    (checkout / ".hlt-index-error").write_text("previous refresh failed")
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SOURCE_ONLY_REPOS="demo", CODEGRAPH_MIN_FREE_MB="0", CODEGRAPH_FORCE_REINDEX="0")
    assert result.returncode == 1
    assert "still fresh, skipping" not in result.stdout
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")


@pytest.mark.parametrize("stamp", ["", "2026-10-15T11:"])
def test_truncated_recent_stamp_without_error_marker_does_not_skip_refresh(tmp_path, stamp):
    repos = tmp_path / "repos"
    repos.mkdir()
    checkout = repos / "demo"
    _create_source_repo(checkout)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(tmp_path / "missing.git")], check=True)
    # ENOSPC can truncate the stamp and prevent even the failure marker write.
    (checkout / ".hlt-source-ready-at").write_text(stamp)
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SOURCE_ONLY_REPOS="demo", CODEGRAPH_MIN_FREE_MB="0", CODEGRAPH_FORCE_REINDEX="0")
    assert result.returncode == 1
    assert "updating demo" in result.stdout
    assert "still fresh, skipping" not in result.stdout
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")


def test_cleanup_refreshes_source_and_preserves_the_saved_graph(tmp_path):
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    repos = tmp_path / "repos"
    repos.mkdir()
    checkout = repos / "demo"
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
    index = checkout / ".gitnexus"
    index.mkdir()
    (index / "graph.db").write_text("saved graph")
    garbage = subprocess.check_output(["git", "-C", str(checkout), "hash-object", "-w", "--stdin"], input=b"unreferenced cached object").decode().strip()
    (upstream / "new-source.txt").write_text("new source")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "new source"], check=True)
    expected = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"])

    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SOURCE_ONLY_REPOS="demo", CODEGRAPH_MIN_FREE_MB="0")

    assert result.returncode == 0, result.stderr
    assert subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == expected
    assert subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", garbage], capture_output=True).returncode != 0
    assert (index / "graph.db").read_text() == "saved graph"
    assert (checkout / ".hlt-source-ready-at").exists()
    assert (repos / ".hlt-reindex-status").read_text().startswith("ok ")
    assert (tmp_path / "gitnexus-calls").read_text() == "list\n"


def test_failed_freshness_write_is_reported_as_failure(tmp_path):
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    repos = tmp_path / "repos"
    repos.mkdir()
    checkout = repos / "demo"
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
    (checkout / ".hlt-source-ready-at").mkdir()  # Force a write failure without filling the host disk.
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SOURCE_ONLY_REPOS="demo", CODEGRAPH_MIN_FREE_MB="0")
    assert result.returncode == 1
    assert (checkout / ".hlt-index-error").exists()
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")


@pytest.mark.parametrize("record", ["", "ok invalid", "unknown 2026-01-01T00:00:00Z", "ok 2026-01-01T00:00:00Z"])
def test_public_health_does_not_claim_ok_for_corrupt_or_stale_status(tmp_path, monkeypatch, record):
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    (tmp_path / ".hlt-reindex-status").write_text(record)
    response = asyncio.run(server.health_check(None))
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "degraded"


def test_public_health_stays_available_when_its_check_fails(monkeypatch):
    def broken_check():
        raise OSError("private repository path must not leak")
    monkeypatch.setattr(server, "_index_problem", broken_check)
    response = asyncio.run(server.health_check(None))
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "degraded"
    assert b"private repository path" not in response.body


def _create_source_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    source = path / "app" / "api" / "identity" / "route.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "export const consentStatus = 'interested';\n"
        "export function captureEmail(email: string) {\n"
        "  return { email, consentStatus };\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_search_source_returns_real_lines_and_immutable_links(tmp_path, monkeypatch):
    sha = _create_source_repo(tmp_path / "nursing-mastery")
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(
        server,
        "_repo_readiness",
        lambda _repo: {"status": "ready", "indexedAt": "2026-08-06T00:00:00Z"},
    )

    payload = json.loads(server.search_source("Where does email capture set consent status?", "nursing-mastery"))

    assert payload["commitSha"] == sha
    assert payload["matches"][0]["path"] == "app/api/identity/route.ts"
    assert any("consentStatus" in match["text"] for match in payload["matches"])
    assert all(f"/blob/{sha}/" in match["url"] for match in payload["matches"])


def test_read_source_returns_bounded_numbered_context(tmp_path, monkeypatch):
    sha = _create_source_repo(tmp_path / "nursing-mastery")
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(
        server,
        "_repo_readiness",
        lambda _repo: {"status": "ready", "indexedAt": "2026-08-06T00:00:00Z"},
    )

    payload = json.loads(server.read_source("nursing-mastery", "app/api/identity/route.ts", 2, 3))

    assert payload["commitSha"] == sha
    assert payload["lines"] == [
        {"line": 2, "text": "export function captureEmail(email: string) {"},
        {"line": 3, "text": "  return { email, consentStatus };"},
    ]
    assert payload["url"].endswith("#L2-L3")


def test_read_source_rejects_path_traversal(tmp_path, monkeypatch):
    _create_source_repo(tmp_path / "nursing-mastery")
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})

    payload = json.loads(server.read_source("nursing-mastery", "../secret.txt"))

    assert payload == {"error": "Invalid repository path."}


def test_search_source_ranks_implementation_above_stale_planning_docs(tmp_path, monkeypatch):
    repo = tmp_path / "katailyst2"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    implementation = repo / "lib" / "providers" / "marketo.ts"
    implementation.parent.mkdir(parents=True)
    implementation.write_text(
        "export const truth = '" + ("historical detail " * 60)
        + "Marketo OAuth connected; asset authoring ready; send remains human gated';\n",
        encoding="utf-8",
    )
    generic = repo / "lib" / "research" / "generic-source.ts"
    generic.parent.mkdir(parents=True)
    generic.write_text(
        "export const generic = 'available live research source';\n",
        encoding="utf-8",
    )
    plan = repo / "docs" / "plans" / "old-marketo-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "Marketo OAuth connected asset authoring ready send remains human gated\n",
        encoding="utf-8",
    )
    generated = repo / ".quality-loop" / "state.csv"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        "Marketo OAuth connected asset authoring ready send remains human gated\n" * 1_100,
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"katailyst2": "Awhitter/katailyst2"})
    monkeypatch.setattr(
        server,
        "_repo_readiness",
        lambda _repo: {"status": "ready", "indexedAt": "2026-08-06T00:00:00Z"},
    )

    payload = json.loads(
        server.search_source(
            "Is Marketo available as a live research source?",
            "katailyst2",
        )
    )

    assert payload["matches"][0]["path"] == "lib/providers/marketo.ts"
    assert "Marketo" in payload["matches"][0]["text"]
    planning = next(match for match in payload["matches"] if match["path"].startswith("docs/plans/"))
    assert planning["sourceKind"] == "planning"
    assert not any(match["path"].startswith(".quality-loop/") for match in payload["matches"])
    assert "not current-state proof" in payload["guidance"]


def test_source_only_repository_is_ready_for_exact_source_reads(tmp_path, monkeypatch):
    repo = tmp_path / "hlt-web-service"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("HLT account API\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    (repo / ".hlt-source-ready-at").write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))

    readiness = server._repo_readiness("hlt-web-service")

    assert readiness["status"] == "ready"
    assert readiness["mode"] == "source"
    assert readiness["error"] is None


def test_verify_source_ref_checks_the_exact_private_checkout(tmp_path, monkeypatch):
    sha = _create_source_repo(tmp_path / "nursing-mastery")
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(server, "REPO_GITHUB", {"nursing-mastery": "Awhitter/nursing-mastery"})
    monkeypatch.setattr(
        server,
        "_repo_readiness",
        lambda _repo: {"status": "ready", "indexedAt": "2026-08-06T00:00:00Z"},
    )

    payload = json.loads(
        server.verify_source_ref(
            "Awhitter/nursing-mastery",
            "app/api/identity/route.ts",
            sha,
        )
    )

    assert payload["exists"] is True
    assert payload["commitSha"] == sha
    assert payload["path"] == "app/api/identity/route.ts"
    assert f"/blob/{sha}/app/api/identity/route.ts" in payload["url"]


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
