"""Streamable-HTTP MCP facade over GitNexus CLI for HLT estate repos.

GPT Researcher connects via CODEGRAPH_MCP_URL / CODEGRAPH_MCP_TOKEN.
Tools shell out to `gitnexus query|context|impact|trace|list` so the
structural graph stays the source of truth without embedding GitNexus.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="[%(asctime)s][%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("hlt-codegraph")

GITNEXUS_HOME = os.getenv("GITNEXUS_HOME", "/data/gitnexus")
REPOS_DIR = os.getenv("REPOS_DIR", "/data/repos")
AUTH_TOKEN = os.getenv("CODEGRAPH_MCP_TOKEN") or os.getenv("GITNEXUS_AUTH_TOKEN")
REPO_GITHUB = {
    "nursing-mastery": "Awhitter/nursing-mastery",
    "scrapervault": "Awhitter/ScraperVault",
    "katailyst2": "Awhitter/katailyst2",
    "mmm2": "Awhitter/MMM2",
    "ebb": "Awhitter/evidence-based-business",
}
INDEX_STALE_HOURS = int(os.getenv("CODEGRAPH_STALE_HOURS", "36"))


def _allowed_hosts() -> list[str]:
    configured = os.getenv("MCP_ALLOWED_HOSTS")
    if configured:
        return [host.strip() for host in configured.split(",") if host.strip()]
    port = os.getenv("PORT", "8080")
    hosts = [
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        "127.0.0.1",
        "localhost",
        "0.0.0.0",
    ]
    # Render terminates TLS at the edge and forwards with the public Host
    # header, so the external hostname must be allowed for MCP clients.
    external = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if external:
        hosts.append(external)
    return hosts


def _run_gitnexus(args: list[str], timeout: int = 120) -> str:
    env = os.environ.copy()
    env["GITNEXUS_HOME"] = GITNEXUS_HOME
    try:
        completed = subprocess.run(
            ["gitnexus", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return json.dumps({"error": "gitnexus CLI not installed"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "gitnexus command timed out", "args": args})

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return json.dumps(
            {
                "error": "gitnexus command failed",
                "args": args,
                "stdout": stdout[:4000],
                "stderr": stderr[:4000],
                "code": completed.returncode,
            }
        )
    return stdout or stderr or "{}"


def _git(repo_path: str, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _repo_readiness(repo: str) -> dict[str, Any]:
    path = os.path.join(REPOS_DIR, repo)
    result: dict[str, Any] = {
        "repo": repo,
        "branch": None,
        "commitSha": None,
        "indexedAt": None,
        "status": "unavailable",
        "error": "Repository clone is missing.",
    }
    if not os.path.isdir(os.path.join(path, ".git")):
        return result
    result["branch"] = _git(path, "branch", "--show-current") or "detached"
    result["commitSha"] = _git(path, "rev-parse", "HEAD")
    indexed_file = Path(path) / ".hlt-indexed-at"
    error_file = Path(path) / ".hlt-index-error"
    if error_file.exists():
        result["error"] = error_file.read_text(encoding="utf-8").strip()[:500]
        result["status"] = "unavailable"
        return result
    if not indexed_file.exists() or not result["commitSha"]:
        result["error"] = "This repository has not completed an index run."
        return result
    indexed_at = indexed_file.read_text(encoding="utf-8").strip()
    result["indexedAt"] = indexed_at
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(indexed_at.replace("Z", "+00:00"))
        if age.total_seconds() > INDEX_STALE_HOURS * 3600:
            result["status"] = "partial"
            result["error"] = f"Index is older than {INDEX_STALE_HOURS} hours."
        else:
            result["status"] = "ready"
            result["error"] = None
    except ValueError:
        result["status"] = "partial"
        result["error"] = "Index timestamp is invalid."
    return result


mcp = FastMCP(
    name="HLT Codegraph",
    instructions=(
        "Structural code intelligence for HLT estate repos (mmm2, katailyst2, "
        "ebb, scrapervault, nursing-mastery). Prefer list_repos then query/"
        "context/impact/trace. Answers should stay architecture-aware."
    ),
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(allowed_hosts=_allowed_hosts()),
)


@mcp.tool()
def list_repos() -> str:
    """List indexed estate repositories available for structural queries."""
    return _run_gitnexus(["list"])


@mcp.tool()
def query(q: str, repo: str | None = None) -> str:
    """Hybrid search across the code knowledge graph."""
    args = ["query", q]
    if repo:
        args.extend(["--repo", repo])
    return _run_gitnexus(args)


@mcp.tool()
def context(symbol: str, repo: str | None = None) -> str:
    """360-degree view of a symbol: callers, callees, cluster participation."""
    args = ["context", symbol]
    if repo:
        args.extend(["--repo", repo])
    return _run_gitnexus(args)


@mcp.tool()
def impact(symbol: str, repo: str | None = None) -> str:
    """Blast-radius analysis for a symbol or file."""
    args = ["impact", symbol]
    if repo:
        args.extend(["--repo", repo])
    return _run_gitnexus(args)


@mcp.tool()
def trace(from_symbol: str, to_symbol: str, repo: str | None = None) -> str:
    """Shortest path between two symbols in the call graph."""
    args = ["trace", from_symbol, to_symbol]
    if repo:
        args.extend(["--repo", repo])
    return _run_gitnexus(args)


@mcp.tool()
def repo_overview(repo: str) -> str:
    """Plain-English overview: path on disk plus gitnexus status for one repo."""
    path = os.path.join(REPOS_DIR, repo)
    status = _run_gitnexus(["status"], timeout=60)
    listing = _run_gitnexus(["list"], timeout=60)
    return json.dumps(
        {
            "repo": repo,
            "path": path,
            "exists": os.path.isdir(path),
            "status": status,
            "registry": listing,
            "readiness": _repo_readiness(repo),
        }
    )


@mcp.tool()
def verify_source_ref(repo: str, path: str, commit_sha: str | None = None) -> str:
    """Verify that a source path exists at an exact commit and return its permalink."""
    repo_key = repo.lower().strip()
    if repo_key not in REPO_GITHUB:
        return json.dumps({"exists": False, "error": "Repository is not in the active HLT index."})
    relative = path.strip().lstrip("/")
    if not relative or ".." in Path(relative).parts:
        return json.dumps({"exists": False, "error": "Invalid repository path."})
    repo_path = os.path.join(REPOS_DIR, repo_key)
    current_sha = _git(repo_path, "rev-parse", "HEAD")
    requested_sha = (commit_sha or current_sha or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", requested_sha):
        return json.dumps({"exists": False, "error": "An exact 40-character commit SHA is required."})
    exists = _git(repo_path, "cat-file", "-e", f"{requested_sha}:{relative}") is not None
    readiness = _repo_readiness(repo_key)
    return json.dumps(
        {
            "repo": REPO_GITHUB[repo_key],
            "commitSha": requested_sha,
            "path": relative,
            "exists": exists,
            "indexedAt": readiness.get("indexedAt"),
            "status": readiness.get("status"),
            "url": f"https://github.com/{REPO_GITHUB[repo_key]}/blob/{requested_sha}/{relative}",
        }
    )


@mcp.tool()
def recent_changes(repo: str, days: int = 14, limit: int = 25) -> str:
    """What actually shipped in a repo recently, in plain language.

    Reads the repo's CHANGELOG.md, which both nursing-mastery and ScraperVault
    keep as dated, thematic prose ("a day that shipped forty PRs gets grouped by
    what actually changed for a nurse") and hold complete with a coverage gate
    that asserts every merged PR appears in it.

    This is a far better answer than Linear for shipped work: these repos merge
    hundreds of PRs a fortnight, while Linear's completed feed is capped and
    ordered by last-touched. Use Linear for what is OPEN, and this for what is
    DONE.

    Note the clone here is `--depth=1`, so `git log` has no history — the
    CHANGELOG file is the record, and it is the better one anyway.
    """
    repo_key = repo.strip().lower()
    if repo_key not in REPO_GITHUB:
        return json.dumps({"error": f"Unknown repo {repo!r}.", "known": sorted(REPO_GITHUB)})

    path = os.path.join(REPOS_DIR, repo_key, "CHANGELOG.md")
    if not os.path.isfile(path):
        return json.dumps({"error": f"{repo_key} has no CHANGELOG.md in the indexed clone."})

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        return json.dumps({"error": f"Could not read CHANGELOG.md: {error}"})

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
    entries = []
    for match in re.finditer(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[-–—]?\s*(.*)$", text, re.M):
        date, title = match.group(1), match.group(2).strip()
        if date < cutoff:
            break  # newest-first, so the first old entry ends the window
        body = text[match.end() : text.find("\n## ", match.end())]
        entries.append({"date": date, "title": title, "body": body.strip()[:1200]})
        if len(entries) >= limit:
            break

    return json.dumps(
        {
            "repo": repo_key,
            "github": REPO_GITHUB[repo_key],
            "since": cutoff,
            "readiness": _repo_readiness(repo_key),
            "entry_count": len(entries),
            "entries": entries,
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):  # noqa: ANN001
    """Liveness only. Deliberately says nothing about what we index.

    Render's ipAllowList is 0.0.0.0/0, so this endpoint is world-readable. It
    used to return repo slugs, branches and 40-char commit SHAs for five
    private repositories — a map of the estate, free to anyone who guessed the
    hostname. Index truth moved to /readiness, behind the bearer token.
    """
    return JSONResponse({"status": "ok", "service": "hlt-codegraph"})


@mcp.custom_route("/readiness", methods=["GET"])
async def readiness_check(request):  # noqa: ANN001
    """Per-repository index truth. Authenticated (see BearerAuthMiddleware)."""
    repos = []
    if os.path.isdir(REPOS_DIR):
        repos = sorted(
            name
            for name in os.listdir(REPOS_DIR)
            if os.path.isdir(os.path.join(REPOS_DIR, name))
        )
    return JSONResponse(
        {
            "status": "ok",
            "service": "hlt-codegraph",
            "repos": repos,
            "repositories": [_repo_readiness(repo) for repo in REPO_GITHUB],
            "gitnexus_home": GITNEXUS_HOME,
        }
    )


# Only liveness is public. "/" used to be exempt too, which on a
# streamable-HTTP MCP app is a live transport path, not a landing page.
PUBLIC_PATHS = frozenset({"/health"})


def auth_failure(path: str, authorization: str, token: str | None) -> tuple[int, str] | None:
    """Return ``(status, detail)`` when a request must be rejected, else ``None``.

    Split out from the middleware so the decision itself can be tested — this
    is the whole perimeter of the sidecar, and it previously failed open.
    """
    if path in PUBLIC_PATHS:
        return None

    # Fail CLOSED. CODEGRAPH_MCP_TOKEN is `sync: false` in render.yaml, so a
    # blank value is one dashboard slip away — and an unauthenticated tool
    # surface here serves every private repo's source via `gitnexus query`.
    # 503 rather than exiting: a hard exit deploy-loops, where this leaves the
    # instance up and legible.
    if not token:
        return 503, "Service is not configured with an auth token"

    provided = ""
    if authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    # Encode both sides: compare_digest raises TypeError on non-ASCII str,
    # which would turn a 401 into a 500.
    if not hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8")):
        return 401, "Unauthorized"
    return None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        failure = auth_failure(
            request.url.path, request.headers.get("authorization", ""), AUTH_TOKEN
        )
        if failure is None:
            return await call_next(request)
        status, detail = failure
        if status == 503:
            logger.error("CODEGRAPH_MCP_TOKEN is unset — refusing all authenticated routes")
        return JSONResponse({"detail": detail}, status_code=status)


app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)


def run() -> None:
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    run()
