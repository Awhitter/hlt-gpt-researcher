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


def test_public_health_names_no_repositories(tmp_path, monkeypatch):
    """Index truth belongs on /readiness, behind the token.

    Asserts on the actual response body rather than the source text — the
    first version of this test read the function source and tripped over the
    word "repositories" in its own docstring.
    """
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    (tmp_path / ".hlt-reindex-status").write_text(
        f"ok {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    )
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
    assert json.loads(asyncio.run(server.health_check(None)).body)["status"] == "degraded"


def _run_reindex(repos: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    # Never invoke an installed graph CLI or load a model during shell proofs.
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    _env_spy(tools, "gitnexus", 'printf "%s\\n" "$*" >> "$GITNEXUS_TEST_CALLS"')
    return subprocess.run(
        ["bash", str(Path(server.__file__).with_name("reindex.sh"))],
        env={
            **os.environ,
            "REPOS_DIR": str(repos),
            "GITNEXUS_HOME": str(tmp_path / "gitnexus"),
            "CODEGRAPH_FORCE_REINDEX": "1",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            # Never reach the real Katailyst from a test.
            "KATAILYST2_MCP_TOKEN": "",
            "KATAILYST_MCP_TOKEN": "",
            "KATAILYST2_MCP_URL": "http://127.0.0.1:9/mcp",
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


def test_frozen_failure_is_reported_as_paused_not_ok(tmp_path):
    repos = tmp_path / "repos"
    checkout = repos / "demo"
    checkout.mkdir(parents=True)
    error = checkout / ".hlt-index-error"
    error.write_text("previous failure")
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SKIP_REPOS="demo")
    assert result.returncode == 0, result.stderr
    assert error.read_text() == "previous failure"
    assert (repos / ".hlt-reindex-status").read_text().split()[0] == "paused"


def test_paused_run_records_count_and_oldest_index(tmp_path):
    repos = tmp_path / "repos"
    for slug, stamp in (("old", "2026-08-06T06:18:27Z"), ("newer", "2026-09-22T20:22:39Z")):
        (repos / slug).mkdir(parents=True)
        (repos / slug / ".hlt-indexed-at").write_text(stamp + "\n")
    (repos / "newer" / ".hlt-index-error").write_text("previous failure")
    result = _run_reindex(
        repos,
        tmp_path,
        CODEGRAPH_REPOS="newer|example/newer,old|example/old",
        CODEGRAPH_SKIP_REPOS="newer,old",
    )
    assert result.returncode == 0, result.stderr
    state, _, count, since = (repos / ".hlt-reindex-status").read_text().split()
    assert (state, count, since) == ("paused", "2", "2026-08-06T06:18:27Z")

    (repos / "never").mkdir()
    _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="old|example/old,never|example/never", CODEGRAPH_SKIP_REPOS="old,never")
    assert (repos / ".hlt-reindex-status").read_text().split()[2:] == ["2", "never"]


def test_public_health_names_paused_count_and_date_but_no_repositories(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    ran = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    status = tmp_path / ".hlt-reindex-status"
    status.write_text(f"paused {ran.strftime('%Y-%m-%dT%H:%M:%SZ')} 5 2026-08-06T06:18:27Z\n")
    payload = json.loads(asyncio.run(server.health_check(None)).body)
    assert payload["status"] == "degraded"
    assert payload["reason"] == (
        f"Daily refresh ran {ran.day} {ran:%b %H:%M} UTC. "
        "5 repositories paused since 6 Aug; answers about them are stale."
    )
    for repo in server.REPO_GITHUB:
        assert repo not in json.dumps(payload)

    status.write_text("paused 2026-01-01T00:00:00Z 1 never\n")
    reason = json.loads(asyncio.run(server.health_check(None)).body)["reason"]
    assert reason == "The last re-index is stale; answers may be stale."


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
    # Only genuinely old unreachable objects may be reclaimed. A Git writer
    # can have just written a blob without having attached it to a ref yet.
    old_object = checkout / ".git" / "objects" / garbage[:2] / garbage[2:]
    old_time = time.time() - 31 * 24 * 3600
    os.utime(old_object, (old_time, old_time))
    in_flight = subprocess.check_output(["git", "-C", str(checkout), "hash-object", "-w", "--stdin"], input=b"another writer's new object").decode().strip()
    (upstream / "new-source.txt").write_text("new source")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "new source"], check=True)
    expected = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"])

    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo", CODEGRAPH_SOURCE_ONLY_REPOS="demo", CODEGRAPH_MIN_FREE_MB="0")

    assert result.returncode == 0, result.stderr
    assert subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == expected
    assert subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", garbage], capture_output=True).returncode != 0
    assert subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", in_flight], capture_output=True).returncode == 0
    assert (index / "graph.db").read_text() == "saved graph"
    assert (checkout / ".hlt-source-ready-at").exists()
    assert (repos / ".hlt-reindex-status").read_text().startswith("ok ")
    assert (tmp_path / "gitnexus-calls").read_text() == "list\n"


def test_refresh_drops_a_key_baked_into_origin_and_authenticates_from_the_environment(tmp_path):
    """Replacing GITHUB_TOKEN must reach old checkouts, and no key may stay on the disk."""
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    repos = tmp_path / "repos"
    repos.mkdir()
    checkout = repos / "demo"
    subprocess.run(["git", "clone", "-q", str(upstream), str(checkout)], check=True)
    # How clones were made before 8 Nov 2026: the key inside origin's URL.
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "set-url", "origin",
         "https://x-access-token:stolen-old-key@github.com/example/demo.git"],
        check=True,
    )
    (upstream / "new-source.txt").write_text("new source")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "new source"], check=True)
    expected = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"])

    result = _run_reindex(
        repos,
        tmp_path,
        CODEGRAPH_REPOS="demo|example/demo",
        CODEGRAPH_SOURCE_ONLY_REPOS="demo",
        CODEGRAPH_MIN_FREE_MB="0",
        GITHUB_TOKEN="fresh-key",
        # Serve github.com from the local upstream so the proof needs no network.
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0=f"url.{upstream.as_uri()}.insteadOf",
        GIT_CONFIG_VALUE_0="https://github.com/example/demo.git",
    )

    assert result.returncode == 0, result.stderr
    assert subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]) == expected
    origin = subprocess.check_output(["git", "-C", str(checkout), "remote", "get-url", "origin"], text=True).strip()
    assert origin == "https://github.com/example/demo.git"
    config = (checkout / ".git" / "config").read_text()
    assert "stolen-old-key" not in config and "fresh-key" not in config


def test_skipped_and_retired_checkouts_drop_legacy_credentials_before_refresh(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    for slug in ("frozen", "retired"):
        checkout = repos / slug
        _create_source_repo(checkout)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                        f"https://x-access-token:fixture-only@github.com/example/{slug}.git"], check=True)
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="frozen|example/frozen", CODEGRAPH_SKIP_REPOS="frozen")
    assert result.returncode == 0, result.stderr
    for checkout in repos.iterdir():
        if checkout.is_dir():
            assert subprocess.check_output(["git", "-C", str(checkout), "config", "--local",
                                            "remote.origin.url"], text=True).strip() == f"https://github.com/example/{checkout.name}.git"
    assert "fixture-only" not in result.stdout + result.stderr


def test_selected_symlink_is_refused_without_touching_target(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    target = tmp_path / "unmanaged"
    _create_source_repo(target)
    (repos / "demo").symlink_to(target, target_is_directory=True)
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="demo|example/demo")
    assert result.returncode == 1
    assert "unsafe checkout path" in result.stderr
    assert not (target / ".hlt-index-error").exists()


def test_origin_cleanup_failure_stops_refresh_without_printing_credentials(tmp_path):
    repos = tmp_path / "repos"
    checkout = repos / "retired"
    repos.mkdir()
    _create_source_repo(checkout)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                    "https://fixture-only@github.com/example/retired.git"], check=True)
    (checkout / ".git" / "config.lock").touch()
    result = _run_reindex(repos, tmp_path, CODEGRAPH_REPOS="missing|example/missing")
    assert result.returncode == 1
    assert "fixture-only" not in result.stdout + result.stderr
    assert (repos / ".hlt-reindex-status").read_text().startswith("failed ")
    assert not (repos / "missing").exists()


def test_a_frozen_graph_with_fresh_code_reports_the_code_as_ready(tmp_path, monkeypatch):
    """Five repos sat frozen for months; once their code refreshes, say so and keep the graph's date."""
    _create_source_repo(tmp_path / "katailyst2")
    repo = tmp_path / "katailyst2"
    graph_stamp = repo / ".hlt-indexed-at"
    graph_stamp.write_text("2026-08-06T06:18:27Z\n", encoding="utf-8")
    old = time.time() - 90 * 24 * 3600
    os.utime(graph_stamp, (old, old))
    (repo / ".hlt-source-ready-at").write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))

    readiness = server._repo_readiness("katailyst2")

    assert readiness["status"] == "ready"
    assert readiness["mode"] == "source"
    assert readiness["structuralIndexedAt"] == "2026-08-06T06:18:27Z"
    assert readiness["error"] is None


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


def test_public_health_is_unverified_without_refresh_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    response = asyncio.run(server.health_check(None))
    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "degraded"


def test_starting_retry_does_not_claim_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REPOS_DIR", str(tmp_path))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    status = tmp_path / ".hlt-reindex-status"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for state in ("failed", "running"):
        status.write_text(f"{state} {now}\n")
        response = asyncio.run(server.health_check(None))
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "degraded"
    status.write_text(f"ok {now}\n")
    assert json.loads(asyncio.run(server.health_check(None)).body)["status"] == "ok"


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


def test_every_codegraph_tool_says_it_only_reads():
    """Katailyst's door runs only tools whose live definition attests readOnlyHint."""
    tools = asyncio.run(server.mcp.list_tools())
    assert tools
    for tool in tools:
        hints = tool.annotations
        assert hints is not None, tool.name
        assert hints.readOnlyHint is True and hints.destructiveHint is False, tool.name


# --- GitHub credentials: Katailyst-minted tokens first, GITHUB_TOKEN as fallback ---
#
# Since the 8 Nov 2026 key rotation GitHub refused GITHUB_TOKEN and every refresh
# failed. reindex.sh now asks Katailyst's GitHub App (github.token.mint) for
# one-hour read tokens. These proofs run the real reindex.sh and the real helper
# against a local stand-in for Katailyst and a git shim that records which key
# each network step carried, then runs the real git.

MINTED = "minted-fixture-key"
PAT = "pat-fixture-key"
KATAILYST_KEY = "kata-fixture-key"
VERB = "github.token.mint"
# lib/mcp/transport-keepalive.ts sends this comment frame every 15 s while a call runs.
KEEPALIVE = b": katailyst keepalive\n\n"
# The MCP door's answer to a key without the verb's scope (lib/mcp/handler.ts).
SCOPE_DENIED = {
    "error": "scope_denied", "verb": VERB, "neededScope": "github.token", "have": ["registry.read"],
    "toolsets": [], "hint": "Ask an owner to grant github.token.", "forPerson": "This key cannot do that yet.",
}
# github-app.ts once a caller has minted twenty tokens in an hour.
RATE_LIMITED = {
    "error": "This caller has minted 20 GitHub tokens in the last hour (limit 20). Reuse the token you have; it lasts an hour.",
    "code": "scope_denied", "meta": {"rateLimited": True}, "verb": VERB,
}


def _minted_for(names: list[str]) -> str:
    return f"{MINTED}-for-{'-'.join(names)}"


class _FakeKatailyst:
    """A streamable-HTTP MCP stand-in that answers github_token_mint the way Katailyst does.

    A refusal has Katailyst's own shape, a tool result with isError and one JSON text part
    (lib/mcp/handler.ts), in the words of lib/providers/github-app-policy.ts and github-app.ts.
    Like Katailyst it refuses a whole request for one repository off its list (`off_list`), one
    not ticked on the installation (`not_installed`), or more than eight names.
    """

    def __init__(self, mint_ok: bool = True, *, off_list=(), not_installed=(), refusal=None,
                 token_per_request: bool = False, status: int | None = None, redirect_to: str | None = None,
                 stall: bool = False, keepalives: int = 0, reply=None):
        import http.server
        import threading

        self.calls: list[dict] = []
        self.authorization: list[str] = []
        self.requests = 0
        self._closed = threading.Event()
        fake = self

        def fail(body: dict) -> dict:
            return {"isError": True, "content": [{"type": "text", "text": json.dumps(body)}]}

        def mint(asked: list[str]) -> dict:
            names = [name.split("/")[-1] for name in asked]  # Katailyst answers in GitHub's own spelling
            if len(names) > 8:
                return fail({"error": "At most 8 repositories per token.", "code": "scope_denied", "verb": VERB})
            for name in names:
                if name in off_list:
                    return fail({"error": f"{name} is not on the Katailyst GitHub App's repository list.",
                                 "code": "scope_denied", "verb": VERB})
            missing = [name for name in names if name in not_installed]
            if missing:
                return fail({
                    "error": f"The Katailyst GitHub App is not installed on {', '.join(missing)}. The owner chooses "
                             "its repositories on GitHub (Settings, Applications, HLT Katailyst, Configure).",
                    "code": "tool_failed", "verb": VERB,
                    "meta": {"providerCalled": True, "notInstalled": len(missing) == len(names), "notInstalledOn": missing},
                })
            if refusal is not None:
                return fail(refusal)
            if not mint_ok:
                return fail({"error": "This key cannot get GitHub tokens.", "code": "scope_denied", "verb": VERB})
            data = {"token": _minted_for(names) if token_per_request else MINTED, "expiresAt": "2026-11-14T06:00:00Z",
                    "repositories": sorted(names),
                    "permissions": {"contents": "read", "metadata": "read"}, "reused": False}
            return {"structuredContent": data, "content": [{"type": "text", "text": json.dumps(data)}]}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ANN002
                pass

            def _seen(self):
                fake.requests += 1
                fake.authorization.append(self.headers.get("Authorization", ""))

            def do_GET(self):  # noqa: N802
                # urllib turns a followed 302 into a GET: it is recorded like any other request.
                self._seen()
                self.send_response(405)
                self.end_headers()

            def do_POST(self):  # noqa: N802
                self._seen()
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.end_headers()
                    return
                if status:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                calling = body["method"] != "initialize"
                if not calling:
                    answer = {"jsonrpc": "2.0", "id": body["id"], "result": {
                        "protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake"}}}
                else:
                    fake.calls.append(body["params"])
                    answer = reply(body["id"]) if reply else {
                        "jsonrpc": "2.0", "id": body["id"], "result": mint(body["params"]["arguments"]["repositories"])}
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if isinstance(answer, str) else "text/event-stream")
                self.send_header("Mcp-Session-Id", "fixture-session")
                self.end_headers()
                try:
                    if isinstance(answer, str):
                        self.wfile.write(answer.encode())
                        return
                    sent = 0
                    while calling and (stall or sent < keepalives) and not fake._closed.is_set():
                        self.wfile.write(KEEPALIVE)
                        self.wfile.flush()
                        sent += 1
                        time.sleep(0.1)
                    if not (calling and stall):
                        self.wfile.write(f"event: message\ndata: {json.dumps(answer)}\n\n".encode())
                except OSError:
                    pass  # the caller stopped waiting

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def asked(self) -> list[list[str]]:
        """The repositories each mint request named, in order."""
        return [call["arguments"]["repositories"] for call in self.calls]

    def close(self):
        self._closed.set()
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):  # noqa: ANN002
        self.close()


def _git_shim(tmp_path: Path) -> Path:
    """A git that logs the key on each fetch/clone, refuses listed keys like GitHub, then runs real git."""
    import sys

    real_git = shutil.which("git")
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    shim = tools / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import base64, os, sys\n"
        "key = ''\n"
        "for i in range(int(os.environ.get('GIT_CONFIG_COUNT') or 0)):\n"
        "    if os.environ.get(f'GIT_CONFIG_KEY_{i}') == 'http.https://github.com/.extraheader':\n"
        "        key = base64.b64decode(os.environ[f'GIT_CONFIG_VALUE_{i}'].split()[-1]).decode().split(':', 1)[1]\n"
        "if {'fetch', 'clone'} & set(sys.argv[1:]):\n"
        "    with open(os.environ['GIT_AUTH_LOG'], 'a') as log:\n"
        "        log.write((key or '-') + '\\n')\n"
        "    if key and key in os.environ.get('GIT_REFUSE_KEYS', '').split(','):\n"
        "        sys.stderr.write(\"fatal: could not read Username for 'https://github.com': terminal prompts disabled\\n\")\n"
        "        sys.exit(128)\n"
        f"os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])\n"
    )
    shim.chmod(0o755)
    return tmp_path / "git-auth-log"


def _serve_from(*pairs: tuple[Path, str]) -> dict[str, str]:
    """GIT_CONFIG entries that serve each github.com URL from a local upstream."""
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (upstream, url) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = f"url.{upstream.as_uri()}.insteadOf"
        env[f"GIT_CONFIG_VALUE_{i}"] = url
    return env


def _keys_used(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def _github_fixture(tmp_path: Path, *fulls: str) -> tuple[Path, Path, dict[str, str]]:
    """One local upstream served as every github.com repository named: (checkouts, key log, reindex env)."""
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    repos = tmp_path / "repos"
    repos.mkdir()
    log = _git_shim(tmp_path)
    slugs = [full.split("/")[1] for full in fulls]
    return repos, log, {
        "CODEGRAPH_REPOS": ",".join(f"{slug}|{full}" for slug, full in zip(slugs, fulls)),
        "CODEGRAPH_SOURCE_ONLY_REPOS": ",".join(slugs),
        "CODEGRAPH_MIN_FREE_MB": "0",
        "GITHUB_TOKEN": PAT,
        "KATAILYST2_MCP_TOKEN": KATAILYST_KEY,
        "GIT_AUTH_LOG": str(log),
        **_serve_from(*((upstream, f"https://github.com/{full}.git") for full in fulls)),
    }


def _no_key_anywhere(result: subprocess.CompletedProcess, repos: Path | None = None) -> None:
    """No key value in what the run printed, in its status line or in any checkout's saved Git settings."""
    kept = [result.stdout + result.stderr]
    if repos is not None:
        kept += [path.read_text() for path in repos.glob("*/.git/config")]
        kept += [path.read_text() for path in repos.glob(".hlt-reindex-status")]
    for text in kept:
        for secret in (MINTED, PAT, KATAILYST_KEY):
            assert secret not in text
    assert "Traceback" not in result.stderr


def _run_mint(*repositories: str, **env: str) -> subprocess.CompletedProcess:
    """The real helper on the system Python, with the fixture key and never the real Katailyst."""
    return subprocess.run(
        ["python3", str(SERVICE_DIR / "mint_github_token.py"), *repositories],
        env={**os.environ, "KATAILYST2_MCP_TOKEN": KATAILYST_KEY, "KATAILYST_MCP_TOKEN": "",
             "KATAILYST2_MCP_URL": "http://127.0.0.1:9/mcp", "CODEGRAPH_MINT_TIMEOUT_S": "20", **env},
        capture_output=True, text=True, timeout=60,
    )


def _one_line(result: subprocess.CompletedProcess) -> str:
    """The helper's whole stderr, which must be a single plain line: no traceback, no reply, no key."""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("[codegraph] ") and len(lines[0]) < 400
    _no_key_anywhere(result)
    return lines[0]


def _health_reason(repos: Path, monkeypatch) -> str:
    """Public /health's reason for the status line a run left, which must name no repository or key."""
    monkeypatch.setattr(server, "REPOS_DIR", str(repos))
    monkeypatch.setenv("CODEGRAPH_MIN_FREE_MB", "0")
    payload = json.loads(asyncio.run(server.health_check(None)).body)
    assert payload["status"] == "degraded"
    flat = json.dumps(payload)
    for private in (PAT, MINTED, KATAILYST_KEY, *(path.name for path in repos.iterdir() if path.is_dir())):
        assert private not in flat
    return payload["reason"]


def test_refresh_uses_the_katailyst_minted_token_and_never_prints_a_key(tmp_path):
    covered, outside = tmp_path / "covered", tmp_path / "outside"
    _create_source_repo(covered)
    _create_source_repo(outside)
    repos = tmp_path / "repos"
    repos.mkdir()
    log = _git_shim(tmp_path)
    katailyst = _FakeKatailyst()
    try:
        result = _run_reindex(
            repos, tmp_path,
            CODEGRAPH_REPOS="demo|Awhitter/demo,other|HLT-Master/other",
            CODEGRAPH_SOURCE_ONLY_REPOS="demo,other",
            CODEGRAPH_MIN_FREE_MB="0",
            GITHUB_TOKEN=PAT,
            KATAILYST2_MCP_TOKEN=KATAILYST_KEY,
            KATAILYST2_MCP_URL=katailyst.url,
            GIT_AUTH_LOG=str(log),
            **_serve_from((covered, "https://github.com/Awhitter/demo.git"),
                          (outside, "https://github.com/HLT-Master/other.git")),
        )
    finally:
        katailyst.close()

    assert result.returncode == 0, result.stderr
    # The App covers Awhitter repositories; anything else keeps GITHUB_TOKEN.
    assert _keys_used(log) == [MINTED, PAT]
    assert len(katailyst.calls) == 1
    call = katailyst.calls[0]
    assert call["name"] == "github_token_mint"
    assert call["arguments"]["repositories"] == ["demo"]
    assert call["arguments"]["permissions"] == {"contents": "read", "metadata": "read"}
    assert set(katailyst.authorization) == {f"Bearer {KATAILYST_KEY}"}
    _no_key_anywhere(result, repos)
    assert "Katailyst minted a read-only GitHub token for demo" in result.stderr
    # Each repository's key and the reason for it are in the log and on the status line.
    assert "[codegraph] demo: GitHub key is the token Katailyst minted" in result.stdout
    assert "[codegraph] other: GitHub key is GITHUB_TOKEN, the fallback (its owner is outside Katailyst's GitHub App)" in result.stderr
    state, _, *detail = (repos / ".hlt-reindex-status").read_text().split()
    assert state == "ok"
    assert detail == ["github-auth", "mint=ok", "repos=demo:katailyst:minted:ok,other:github-token:outside-app:ok"]


@pytest.mark.parametrize("gap", ["off the App's list", "not ticked on the installation"])
def test_one_uncovered_repository_falls_back_alone_and_the_rest_keep_minted_tokens(tmp_path, monkeypatch, gap):
    """Katailyst refuses a whole request for one uncovered name; that used to turn the minted token off for all."""
    repos, log, env = _github_fixture(tmp_path, "Awhitter/demo", "Awhitter/stray", "Awhitter/the-vault")
    uncovered = {"off_list": ["stray"]} if gap == "off the App's list" else {"not_installed": ["stray"]}
    with _FakeKatailyst(token_per_request=True, **uncovered) as katailyst:
        result = _run_reindex(repos, tmp_path, KATAILYST2_MCP_URL=katailyst.url, GIT_REFUSE_KEYS=PAT, **env)

    # One request for everything first, then one per repository once that was refused.
    assert katailyst.asked == [["demo", "stray", "the-vault"], ["demo"], ["stray"], ["the-vault"]]
    assert _keys_used(log) == [_minted_for(["demo"]), PAT, _minted_for(["the-vault"])]
    assert result.returncode == 1  # GitHub refuses the fallback key, so only the uncovered repository fails
    assert "FAILED to index stray" in result.stderr and "FAILED to index demo" not in result.stderr
    assert (repos / "demo" / ".hlt-source-ready-at").exists() and (repos / "the-vault" / ".hlt-source-ready-at").exists()
    assert "[codegraph] demo: GitHub key is the token Katailyst minted" in result.stdout
    assert ("[codegraph] stray: GitHub key is GITHUB_TOKEN, the fallback "
            "(Katailyst refused this repository: not covered by the GitHub App)") in result.stderr
    assert "Katailyst did not mint a token for stray (not covered by the GitHub App)" in result.stderr
    assert "Katailyst minted a GitHub token for 2 of 3 repositories; for the rest: using GITHUB_TOKEN" in result.stderr
    # The status line names the uncovered repository; public /health gives the cause and a count, no name.
    state, _, *detail = (repos / ".hlt-reindex-status").read_text().split()
    assert state == "failed"
    assert detail == ["github-auth", "mint=partial", "refused=github-token",
                      "repos=demo:katailyst:minted:ok,stray:github-token:not-covered:refused,the-vault:katailyst:minted:ok"]
    reason = _health_reason(repos, monkeypatch)
    assert reason == (
        "The last re-index did not finish: GitHub refused GITHUB_TOKEN; "
        "Katailyst refused 1 repository (not covered by the GitHub App). Answers may be stale."
    )
    assert "stray" not in reason
    _no_key_anywhere(result, repos)


def test_more_than_eight_repositories_are_asked_for_at_most_eight_at_a_time(tmp_path):
    """Katailyst refuses a request naming more than eight repositories."""
    names = [f"repo{n}" for n in range(9)]
    repos, log, env = _github_fixture(tmp_path, *(f"Awhitter/{name}" for name in names))
    with _FakeKatailyst(token_per_request=True) as katailyst:
        result = _run_reindex(repos, tmp_path, KATAILYST2_MCP_URL=katailyst.url, GIT_REFUSE_KEYS=PAT, **env)

    assert result.returncode == 0, result.stderr
    assert katailyst.asked == [names[:8], names[8:]]
    assert _keys_used(log) == [_minted_for(names[:8])] * 8 + [_minted_for(names[8:])]
    assert (repos / ".hlt-reindex-status").read_text().split()[3] == "mint=ok"
    _no_key_anywhere(result, repos)


def _closed_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("trouble", ["unreachable", "keepalives past the deadline", "HTTP 401"])
def test_katailyst_trouble_ends_in_a_clean_fallback_to_github_token(tmp_path, monkeypatch, trouble):
    repos, log, env = _github_fixture(tmp_path, "Awhitter/demo", "Awhitter/the-vault")
    katailyst = {
        "unreachable": None,
        # The reply never comes, but a keepalive does every 0.1 s: a per-read timeout alone would wait for ever.
        "keepalives past the deadline": _FakeKatailyst(stall=True),
        "HTTP 401": _FakeKatailyst(status=401),
    }[trouble]
    said, why = {
        "unreachable": ("Katailyst could not be reached", "no-answer"),
        "keepalives past the deadline": ("Katailyst did not answer within 2 s", "no-answer"),
        "HTTP 401": ("Katailyst refused the key in KATAILYST2_MCP_TOKEN (HTTP 401)", "key-refused"),
    }[trouble]
    url = katailyst.url if katailyst else f"http://127.0.0.1:{_closed_port()}/mcp"
    try:
        # _run_reindex gives the whole run 60 s: a mint that outlived its deadline would fail the test there.
        result = _run_reindex(repos, tmp_path, KATAILYST2_MCP_URL=url,
                              CODEGRAPH_MINT_TIMEOUT_S="2" if trouble == "keepalives past the deadline" else "20", **env)
    finally:
        if katailyst:
            katailyst.close()

    assert result.returncode == 0, result.stderr
    assert _keys_used(log) == [PAT, PAT]
    assert said in result.stderr
    assert "Katailyst did not mint a GitHub token: using GITHUB_TOKEN" in result.stderr
    if katailyst:
        # No answer and a refused key are true of every repository: nothing is asked again per repository.
        assert len(katailyst.calls) <= (1 if trouble == "keepalives past the deadline" else 0)
        assert set(katailyst.authorization) == {f"Bearer {KATAILYST_KEY}"}
    state, _, *detail = (repos / ".hlt-reindex-status").read_text().split()
    assert state == "ok"
    assert detail == ["github-auth", "mint=failed", f"repos=demo:github-token:{why}:ok,the-vault:github-token:{why}:ok"]
    _no_key_anywhere(result, repos)


def test_a_key_without_the_github_token_scope_is_named_as_that_on_public_health(tmp_path, monkeypatch):
    """/health used to blame the scope for every failed mint; now it does only when Katailyst says so."""
    repos, log, env = _github_fixture(tmp_path, "Awhitter/demo", "Awhitter/the-vault")
    with _FakeKatailyst(refusal=SCOPE_DENIED) as katailyst:
        result = _run_reindex(repos, tmp_path, KATAILYST2_MCP_URL=katailyst.url, GIT_REFUSE_KEYS=PAT, **env)

    assert result.returncode == 1
    assert katailyst.asked == [["demo", "the-vault"]]  # the key is the problem, so no request per repository
    assert _keys_used(log) == [PAT, PAT]
    assert "the key in KATAILYST2_MCP_TOKEN lacks the github.token scope" in result.stderr
    assert "registry.read" not in result.stdout + result.stderr  # the key's other scopes stay out of the log
    assert _health_reason(repos, monkeypatch) == (
        "The last re-index did not finish: GitHub refused GITHUB_TOKEN; "
        "the Katailyst key lacks the github.token scope (KATAILYST2_MCP_TOKEN). Answers may be stale."
    )
    _no_key_anywhere(result, repos)


def test_a_failed_mint_falls_back_to_github_token_and_says_so(tmp_path):
    repos, log, env = _github_fixture(tmp_path, "Awhitter/demo")
    with _FakeKatailyst(mint_ok=False) as katailyst:
        result = _run_reindex(repos, tmp_path, KATAILYST2_MCP_URL=katailyst.url, **env)

    assert result.returncode == 0, result.stderr
    assert _keys_used(log) == [PAT]
    assert "Katailyst did not mint a GitHub token: using GITHUB_TOKEN" in result.stderr
    assert "This key cannot get GitHub tokens." in result.stderr
    assert "repos=demo:github-token:key-refused:ok" in (repos / ".hlt-reindex-status").read_text()
    _no_key_anywhere(result, repos)


def test_without_a_katailyst_credential_the_refresh_uses_github_token(tmp_path):
    repos, log, env = _github_fixture(tmp_path, "Awhitter/demo")
    result = _run_reindex(repos, tmp_path, **{**env, "KATAILYST2_MCP_TOKEN": ""})
    assert result.returncode == 0, result.stderr
    assert _keys_used(log) == [PAT]
    assert "no Katailyst credential set: using GITHUB_TOKEN" in result.stderr
    assert "[codegraph] demo: GitHub key is GITHUB_TOKEN, the fallback (no Katailyst credential is set)" in result.stderr
    _no_key_anywhere(result, repos)


def test_a_refused_key_is_named_on_public_health_without_its_value(tmp_path, monkeypatch):
    """8 Nov 2026: /health said only "did not finish" while GitHub refused the key for days."""
    repos, _, env = _github_fixture(tmp_path, "Awhitter/demo")
    result = _run_reindex(repos, tmp_path, GIT_REFUSE_KEYS=PAT, **{**env, "KATAILYST2_MCP_TOKEN": ""})
    assert result.returncode == 1
    assert "GitHub refused GITHUB_TOKEN" in result.stderr
    status = (repos / ".hlt-reindex-status").read_text()
    assert status.startswith("failed ") and "github-auth mint=unset refused=github-token" in status
    assert "repos=demo:github-token:unset:refused" in status
    assert _health_reason(repos, monkeypatch) == (
        "The last re-index did not finish: GitHub refused GITHUB_TOKEN; "
        "no Katailyst credential is set (KATAILYST2_MCP_TOKEN). Answers may be stale."
    )


@pytest.mark.parametrize(("detail", "causes"), [
    ("mint=ok refused=katailyst repos=demo:katailyst:minted:refused", "GitHub refused the token Katailyst minted"),
    ("mint=failed refused=none repos=demo:none:scope:refused",
     "no GitHub key is set; the Katailyst key lacks the github.token scope (KATAILYST2_MCP_TOKEN)"),
    ("mint=failed refused=github-token repos=demo:github-token:key-refused:refused",
     "GitHub refused GITHUB_TOKEN; Katailyst refused the key (KATAILYST2_MCP_TOKEN)"),
    ("mint=failed refused=github-token repos=demo:github-token:no-answer:refused",
     "GitHub refused GITHUB_TOKEN; Katailyst did not answer the token request"),
    ("mint=failed refused=github-token repos=demo:github-token:refused:refused",
     "GitHub refused GITHUB_TOKEN; Katailyst did not mint a GitHub token"),
    # Only the repositories GitHub turned away count: a fallback that worked is not a cause.
    ("mint=partial refused=github-token repos=a:katailyst:minted:ok,b:github-token:not-covered:refused,"
     "c:github-token:not-covered:refused,d:github-token:outside-app:ok,e:github-token:scope:failed",
     "GitHub refused GITHUB_TOKEN; Katailyst refused 2 repositories (not covered by the GitHub App)"),
    ("mint=unused refused=github-token repos=web:github-token:outside-app:refused",
     "GitHub refused GITHUB_TOKEN; 1 repository outside Katailyst's GitHub App (another owner)"),
    ("mint=unset refused=github-token repos=demo:github-token:unset:refused",
     "GitHub refused GITHUB_TOKEN; no Katailyst credential is set (KATAILYST2_MCP_TOKEN)"),
    # Without a record for each repository nothing is guessed: the scope used to be blamed for every failed mint.
    ("mint=failed refused=github-token", "GitHub refused GITHUB_TOKEN"),
])
def test_public_health_tells_the_causes_apart_without_naming_a_repository(tmp_path, monkeypatch, detail, causes):
    (tmp_path / ".hlt-reindex-status").write_text(f"failed 2026-11-14T04:48:00Z github-auth {detail}\n")
    reason = _health_reason(tmp_path, monkeypatch)
    assert reason == f"The last re-index did not finish: {causes}. Answers may be stale."
    assert "demo" not in reason and "web" not in reason


def _env_spy(tools: Path, name: str, then: str) -> None:
    """A stand-in for a command that logs which Katailyst variables it was started with, then runs `then`."""
    spy = tools / name
    spy.write_text(
        "#!/bin/sh\n"
        'if [ -n "${CHILD_ENV_LOG:-}" ]; then\n'
        f"  held=$(env | grep -E '^KATAILYST2?_MCP_TOKEN=|{KATAILYST_KEY}' | cut -d= -f1 | sort | tr '\\n' ' ')\n"
        f'  printf \'%s\\t%s\\t%s\\n\' {name} "$(basename -- "${{1:-}}")" "${{held:--}}" >> "$CHILD_ENV_LOG"\n'
        "fi\n"
        f"{then}\n"
    )
    spy.chmod(0o755)


@pytest.mark.parametrize("names", [("KATAILYST2_MCP_TOKEN",), ("KATAILYST_MCP_TOKEN",),
                                   ("KATAILYST2_MCP_TOKEN", "KATAILYST_MCP_TOKEN")])
def test_only_the_mint_helper_is_ever_started_with_the_katailyst_key(tmp_path, names):
    """The key can mint tokens: gitnexus, git and every other child of the run start without it."""
    upstream = tmp_path / "upstream"
    _create_source_repo(upstream)
    repos = tmp_path / "repos"
    repos.mkdir()
    # An existing checkout, so git also runs before the mint (the saved-origin check).
    subprocess.run(["git", "clone", "-q", str(upstream), str(repos / "old")], check=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    for command in ("git", "python3"):
        _env_spy(tools, command, f'exec {shlex.quote(shutil.which(command))} "$@"')
    children = tmp_path / "child-env"
    with _FakeKatailyst() as katailyst:
        result = _run_reindex(
            repos, tmp_path,
            CODEGRAPH_REPOS="old|Awhitter/old,new|Awhitter/new",
            CODEGRAPH_SOURCE_ONLY_REPOS="old",  # `new` goes through gitnexus analyze, after the mint
            CODEGRAPH_MIN_FREE_MB="0",
            KATAILYST2_MCP_URL=katailyst.url,
            CHILD_ENV_LOG=str(children),
            **{name: KATAILYST_KEY for name in names},
            **_serve_from((upstream, "https://github.com/Awhitter/old.git"),
                          (upstream, "https://github.com/Awhitter/new.git")),
        )

    assert result.returncode == 0, result.stderr
    # The helper still got the key: Katailyst saw it on every request and minted.
    assert katailyst.asked == [["old", "new"]]
    assert set(katailyst.authorization) == {f"Bearer {KATAILYST_KEY}"}
    started = [line.split("\t") for line in children.read_text().splitlines()]
    assert {command for command, _, _ in started} == {"git", "python3", "gitnexus"}
    assert ["gitnexus", "analyze", "-"] in started and ["gitnexus", "list", "-"] in started
    holders = {(command, argument): held.split() for command, argument, held in started if held != "-"}
    assert holders == {("python3", "mint_github_token.py"): ["KATAILYST2_MCP_TOKEN"]}
    _no_key_anywhere(result, repos)


def test_a_redirect_is_refused_and_the_key_never_reaches_the_second_origin(tmp_path):
    """urllib follows a 302 and sends `Authorization: Bearer <key>` again, to wherever it points."""
    repos, log, env = _github_fixture(tmp_path, "Awhitter/demo")
    with _FakeKatailyst() as elsewhere, _FakeKatailyst(redirect_to=elsewhere.url) as katailyst:
        mint = _run_mint("demo", KATAILYST2_MCP_URL=katailyst.url)
        result = _run_reindex(repos, tmp_path, KATAILYST2_MCP_URL=katailyst.url, **env)

    assert mint.returncode == 1
    assert mint.stdout == "fallback\tdemo\tno-answer\n"
    assert "redirect (HTTP 302)" in _one_line(mint)
    assert elsewhere.url not in mint.stderr
    # The second origin received nothing at all, so no Authorization header.
    assert elsewhere.requests == 0 and elsewhere.authorization == []
    assert katailyst.requests == 2 and set(katailyst.authorization) == {f"Bearer {KATAILYST_KEY}"}
    assert result.returncode == 0, result.stderr
    assert _keys_used(log) == [PAT]
    assert "repos=demo:github-token:no-answer:ok" in (repos / ".hlt-reindex-status").read_text()
    _no_key_anywhere(result, repos)


@pytest.mark.parametrize("address", [
    "http://0.0.0.0:{port}/mcp",  # reaches the stand-in on this machine, but is not a local name
    "http://katailyst.example/mcp",
    "http://127.0.0.1.katailyst.example:{port}/mcp",
    "http://localhost.katailyst.example:{port}/mcp",
    "ftp://127.0.0.1:{port}/mcp",
    "https:///mcp",
    "http://127.0.0.1:port/mcp",
    "katailyst.com/mcp",
])
def test_an_address_that_is_neither_https_nor_local_is_refused_before_the_key_is_sent(address):
    with _FakeKatailyst() as katailyst:
        address = address.format(port=katailyst.port)
        mint = _run_mint("demo", KATAILYST2_MCP_URL=address)
        dry = _run_mint("--dry-run", "demo", KATAILYST2_MCP_URL=address)

    assert mint.returncode == 1 and mint.stdout == ""
    line = _one_line(mint)
    assert "KATAILYST2_MCP_URL" in line and address not in line
    assert katailyst.requests == 0
    assert dry.returncode == 1 and dry.stdout == ""


@pytest.mark.parametrize("address", ["https://katailyst.com/mcp", "http://localhost:9/mcp",
                                     "http://127.0.0.1:9/mcp", "http://[::1]:9/mcp"])
def test_https_and_local_addresses_are_accepted(address):
    assert _run_mint("--dry-run", "demo", KATAILYST2_MCP_URL=address).returncode == 0


def _text_reply(text: str):
    return lambda request_id: {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}]}}


@pytest.mark.parametrize(("reply", "why", "said"), [
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id, "error": "scope denied"}, "refused", "scope denied"),
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id, "error": ["not", "words"]}, "refused", "unknown error"),
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": None}}, "refused", "unknown error"),
    (_text_reply("[1, 2]"), "no-answer", "not an object"),
    (_text_reply('"just words"'), "no-answer", "not an object"),
    (_text_reply("7"), "no-answer", "not an object"),
    (_text_reply("null"), "no-answer", "not an object"),
    (_text_reply("Service is busy"), "refused", "Service is busy"),
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id, "result": ["not", "an", "object"]}, "no-answer", "no result"),
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id, "result": {"content": ["loose", 7, None]}}, "refused", "no token"),
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {"token": 12}}}, "refused", "no token"),
    (lambda request_id: {"jsonrpc": "2.0", "id": request_id,
                         "result": {"structuredContent": {"token": "two\twords", "repositories": ["demo"]}}},
     "no-answer", "unusable token"),
    (lambda request_id: [1, 2, 3], "no-answer", "no usable reply"),
    (lambda request_id: "<html>502 Bad Gateway</html>", "no-answer", "not JSON"),
    (lambda request_id: "[1, 2, 3]", "no-answer", "no usable reply"),
], ids=["error is a string", "error is a list", "error has no message", "text is a list", "text is a string",
        "text is a number", "text is null", "text is not JSON", "result is a list", "content holds no parts",
        "token is a number", "token holds a tab", "reply is a list", "body is HTML", "body is a JSON list"])
def test_an_unexpected_reply_ends_in_one_plain_line_and_a_fallback(reply, why, said):
    with _FakeKatailyst(reply=reply) as katailyst:
        mint = _run_mint("demo", KATAILYST2_MCP_URL=katailyst.url)
    assert mint.returncode == 1
    assert mint.stdout == f"fallback\tdemo\t{why}\n"
    assert said in _one_line(mint)


def test_server_text_is_cut_and_anything_key_shaped_is_masked(tmp_path):
    words = f"denied for Bearer {KATAILYST_KEY} and ghs_abcDEF123 and kata_live_abc123 " + "x" * 2000
    with _FakeKatailyst(refusal={"error": words, "code": "tool_failed", "verb": VERB}) as katailyst:
        mint = _run_mint("demo", KATAILYST2_MCP_URL=katailyst.url)
    line = _one_line(mint)
    assert mint.returncode == 1 and "denied for [redacted] and [redacted] and [redacted]" in line
    assert "ghs_abcDEF123" not in line and "kata_live_abc123" not in line


def test_a_katailyst_key_that_cannot_be_a_header_is_refused_without_printing_any_of_it():
    """urllib's own error for such a header quotes it, and a mask keyed on `Bearer <word>` would leave the rest."""
    with _FakeKatailyst() as katailyst:
        mint = _run_mint("demo", KATAILYST2_MCP_URL=katailyst.url, KATAILYST2_MCP_TOKEN="kata-first second\tthird")
    assert mint.returncode == 1 and mint.stdout == ""
    line = _one_line(mint)
    assert "KATAILYST2_MCP_TOKEN is not a usable key" in line
    assert not {"kata-first", "second", "third"} & set(line.replace(":", " ").split())
    assert katailyst.requests == 0


@pytest.mark.parametrize("seconds", ["soon", "0", "-5", "nan", "inf", "45s"])
def test_a_deadline_setting_that_is_not_a_number_ends_in_one_plain_line(seconds):
    with _FakeKatailyst() as katailyst:
        mint = _run_mint("demo", KATAILYST2_MCP_URL=katailyst.url, CODEGRAPH_MINT_TIMEOUT_S=seconds)
    assert mint.returncode == 1 and mint.stdout == ""
    assert "CODEGRAPH_MINT_TIMEOUT_S must be a number of seconds" in _one_line(mint)
    assert katailyst.requests == 0


def test_keepalives_before_a_reply_inside_the_deadline_still_mint():
    with _FakeKatailyst(keepalives=5) as katailyst:
        mint = _run_mint("demo", KATAILYST2_MCP_URL=katailyst.url)
    assert mint.returncode == 0, mint.stderr
    assert mint.stdout == f"minted\tdemo\t{MINTED}\n"
    assert MINTED not in mint.stderr and KATAILYST_KEY not in mint.stderr


@pytest.mark.parametrize(("refusal", "why", "requests"), [
    # About the repositories, or words the helper does not know: each repository is asked for alone.
    ({"error": "the-vault is not on the Katailyst GitHub App's repository list.", "code": "scope_denied", "verb": VERB},
     "not-covered", 3),
    ({"error": "GitHub did not mint the token. GitHub answered 422", "code": "tool_failed", "verb": VERB}, "refused", 3),
    # About the key or the caller: every repository would get the same answer, so nothing is asked again.
    (SCOPE_DENIED, "scope", 1),
    (RATE_LIMITED, "refused", 1),
    ({"error": "Only an HLT member or an agent acting for one can get a GitHub token.", "code": "scope_denied", "verb": VERB},
     "key-refused", 1),
], ids=["off the list", "unknown words", "scope", "hourly limit", "caller"])
def test_only_a_refusal_that_one_repository_could_cause_is_asked_again_per_repository(refusal, why, requests):
    with _FakeKatailyst(refusal=refusal) as katailyst:
        mint = _run_mint("demo", "the-vault", KATAILYST2_MCP_URL=katailyst.url)
    assert mint.returncode == 1
    assert mint.stdout == f"fallback\tdemo\t{why}\nfallback\tthe-vault\t{why}\n"
    assert len(katailyst.calls) == requests
    _no_key_anywhere(mint)


def test_mint_dry_run_shows_the_request_and_calls_nothing(tmp_path):
    result = _run_mint("--dry-run", "katailyst2", "the-vault")
    assert result.returncode == 0, result.stderr
    request = json.loads(result.stdout)
    assert request["katailystCredential"] == "set"
    assert request["tool"] == "github_token_mint"
    assert request["arguments"]["repositories"] == ["katailyst2", "the-vault"]
    assert request["arguments"]["permissions"] == {"contents": "read", "metadata": "read"}
    assert request["deadlineSeconds"] == 20 and "furtherRequests" not in request
    assert KATAILYST_KEY not in result.stdout + result.stderr

    many = json.loads(_run_mint("--dry-run", *(f"repo{n}" for n in range(10))).stdout)
    assert len(many["arguments"]["repositories"]) == 8
    assert [request["repositories"] for request in many["furtherRequests"]] == [["repo8", "repo9"]]
