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

# Per-entry body cap. 1200 cut the third bullet off the 2026-08-05 entry — the
# one saying the next publish would have failed EVERY save on /onboarding.
BODY_CHARS = 4000
AUTH_TOKEN = os.getenv("CODEGRAPH_MCP_TOKEN") or os.getenv("GITNEXUS_AUTH_TOKEN")
REPO_GITHUB = {
    "hlt-gpt-researcher": "Awhitter/hlt-gpt-researcher",
    "nursing-mastery": "Awhitter/nursing-mastery",
    "scrapervault": "Awhitter/ScraperVault",
    "hlt-web-service": "HLT-Master/hlt-web-service",
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
        "mode": "structural",
        "status": "unavailable",
        "error": "Repository clone is missing.",
    }
    if not os.path.isdir(os.path.join(path, ".git")):
        return result
    result["branch"] = _git(path, "branch", "--show-current") or "detached"
    result["commitSha"] = _git(path, "rev-parse", "HEAD")
    indexed_file = Path(path) / ".hlt-indexed-at"
    source_ready_file = Path(path) / ".hlt-source-ready-at"
    error_file = Path(path) / ".hlt-index-error"
    if error_file.exists():
        result["error"] = error_file.read_text(encoding="utf-8").strip()[:500]
        result["status"] = "unavailable"
        return result
    if source_ready_file.exists() and not indexed_file.exists():
        indexed_file = source_ready_file
        result["mode"] = "source"
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
        "Source and structural code intelligence for HLT estate repos (Mastery "
        "Research, mmm2, katailyst2, ebb, scrapervault, nursing-mastery, "
        "hlt-web-service). The HLT web service and Mastery Research are "
        "source-search only to avoid unsafe index rebuilds. "
        "For implementation questions, "
        "use search_source to find real files, read_source to inspect the exact "
        "current lines, and cite the returned immutable URLs. Use query/context/"
        "impact/trace for structural analysis. Never guess a path or symbol."
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
    """Structural graph search; use search_source for exact source evidence."""
    args = ["query", q]
    if repo:
        args.extend(["--repo", repo])
    return _run_gitnexus(args)


_SOURCE_STOPWORDS = {
    "about", "actually", "and", "answer", "are", "available", "can", "current",
    "does", "exactly", "for", "from", "has", "have", "how", "into", "live",
    "not", "nursing", "mastery", "our", "research", "signed", "source", "stored",
    "system", "that", "the", "their", "there", "these", "this", "today", "users",
    "what", "when", "where", "which", "with", "would",
}
_SOURCE_EXCLUDED_PREFIXES = (
    ".",
    "dist/",
    "fixtures/",
    "node_modules/",
    "output/",
    "test/fixtures/",
    "tests/fixtures/",
)


def _repo_key(value: str) -> str:
    return value.strip().split("/")[-1].lower()


def _source_repo_keys(repo: str | None) -> list[str]:
    if repo:
        key = _repo_key(repo)
        return [key] if key in REPO_GITHUB else []
    return sorted(REPO_GITHUB)


def _source_terms(query_text: str) -> list[str]:
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", query_text or ""):
        term = raw.lower()
        if term in _SOURCE_STOPWORDS or term in terms:
            continue
        terms.append(term)
    return terms[:10]


def _source_kind(path: str) -> tuple[str, int]:
    lowered = path.lower()
    if (
        lowered.startswith("docs/plans/")
        or lowered.startswith("docs/superpowers/plans/")
        or "/archive/" in lowered
        or lowered.startswith("archive/")
    ):
        return "planning", 0
    if lowered.startswith("docs/campaigns/") or lowered in {
        "changelog.md",
        "docs/decisions.md",
    }:
        return "operational_receipt", 2
    if lowered.startswith(
        (
            "app/",
            "backend/",
            "client/",
            "lib/",
            "scripts/proof/",
            "server/",
            "services/",
            "shared/",
        )
    ):
        return "implementation", 3
    if lowered.startswith("docs/") or lowered.endswith((".md", ".mdx")):
        return "documentation", 1
    return "implementation", 3


def _source_excerpt(text: str, terms: list[str], limit: int = 600) -> str:
    """Return the matching part of long generated/source lines, not just the prefix."""
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    if not positions or len(text) <= limit:
        return text[:limit]
    start = max(min(positions) - 120, 0)
    excerpt = text[start : start + limit]
    if start:
        excerpt = f"…{excerpt[1:]}"
    if start + limit < len(text):
        excerpt = f"{excerpt[:-1]}…"
    return excerpt


@mcp.tool()
def search_source(query_text: str, repo: str | None = None, limit: int = 20) -> str:
    """Find real tracked source lines and return current immutable permalinks.

    Use this before making implementation claims. Follow promising matches with
    read_source; do not infer a whole behavior from a single matching line.
    """
    repo_keys = _source_repo_keys(repo)
    if repo and not repo_keys:
        return json.dumps({"error": "Repository is not in the active HLT index."})
    terms = _source_terms(query_text)
    if not terms:
        return json.dumps({"error": "Search needs at least one specific source term."})
    requested_limit = max(1, min(int(limit), 30))
    matches: list[dict[str, Any]] = []
    repositories: list[dict[str, Any]] = []
    for repo_key in repo_keys:
        repo_path = os.path.join(REPOS_DIR, repo_key)
        sha = _git(repo_path, "rev-parse", "HEAD")
        if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        args = ["grep", "-n", "-I", "-i", "-F"]
        for term in terms:
            args.extend(["-e", term])
        args.extend(["HEAD", "--"])
        output = _git(repo_path, *args) or ""
        readiness = _repo_readiness(repo_key)
        repositories.append(
            {
                "repo": REPO_GITHUB[repo_key],
                "commitSha": sha,
                "status": readiness.get("status"),
                "indexedAt": readiness.get("indexedAt"),
            }
        )
        prefix = "HEAD:"
        for raw_line in output.splitlines():
            line = raw_line[len(prefix):] if raw_line.startswith(prefix) else raw_line
            parts = line.split(":", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            path, line_number, text = parts
            if path.startswith(_SOURCE_EXCLUDED_PREFIXES):
                continue
            haystack = f"{path} {text}".lower()
            score = sum(1 for term in terms if term in haystack)
            source_kind, authority = _source_kind(path)
            number = int(line_number)
            matches.append(
                {
                    "repo": REPO_GITHUB[repo_key],
                    "commitSha": sha,
                    "path": path,
                    "line": number,
                    "text": _source_excerpt(text, terms),
                    "score": score,
                    "sourceKind": source_kind,
                    "authority": authority,
                    "url": f"https://github.com/{REPO_GITHUB[repo_key]}/blob/{sha}/{path}#L{number}",
                }
            )
    matches.sort(
        key=lambda item: (
            -item["authority"],
            -item["score"],
            item["path"],
            item["line"],
        )
    )
    selected = matches[:requested_limit]
    payload: dict[str, Any] = {
        "query": query_text,
        "terms": terms,
        "repositories": repositories,
        "matches": selected,
        "truncated": len(matches) > len(selected),
        "guidance": (
            "Read the relevant file context with read_source before drawing a conclusion. "
            "A planning result describes intent and is not current-state proof; prefer "
            "implementation and operational receipts, and name any conflict."
        ),
    }
    if len(repositories) == 1:
        payload["commitSha"] = repositories[0]["commitSha"]
    return json.dumps(payload)


@mcp.tool()
def read_source(
    repo: str,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> str:
    """Read bounded, numbered lines from a real file at the indexed HEAD commit."""
    repo_key = _repo_key(repo)
    if repo_key not in REPO_GITHUB:
        return json.dumps({"error": "Repository is not in the active HLT index."})
    relative = path.strip().lstrip("/")
    if not relative or ".." in Path(relative).parts:
        return json.dumps({"error": "Invalid repository path."})
    repo_path = os.path.join(REPOS_DIR, repo_key)
    sha = _git(repo_path, "rev-parse", "HEAD") or ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return json.dumps({"error": "Repository HEAD is unavailable."})
    content = _git(repo_path, "show", f"{sha}:{relative}")
    if content is None:
        return json.dumps({"error": "Source file does not exist at the indexed commit."})
    first = max(1, int(start_line))
    requested_end = int(end_line) if end_line is not None else first + 199
    last = max(first, min(requested_end, first + 239))
    all_lines = content.splitlines()
    last = min(last, len(all_lines))
    numbered = [
        {"line": number, "text": all_lines[number - 1][:1000]}
        for number in range(first, last + 1)
        if number <= len(all_lines)
    ]
    readiness = _repo_readiness(repo_key)
    line_fragment = f"#L{first}" + (f"-L{last}" if last > first else "")
    return json.dumps(
        {
            "repo": REPO_GITHUB[repo_key],
            "commitSha": sha,
            "path": relative,
            "startLine": first,
            "endLine": last,
            "lines": numbered,
            "truncated": requested_end > last,
            "status": readiness.get("status"),
            "indexedAt": readiness.get("indexedAt"),
            "url": f"https://github.com/{REPO_GITHUB[repo_key]}/blob/{sha}/{relative}{line_fragment}",
        }
    )


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
    repo_key = _repo_key(repo)
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
def recent_changes(
    repo: str, days: int = 14, limit: int = 12, dates: str = ""
) -> str:
    """What actually shipped in a repo recently, in plain language.

    Reads the repo's CHANGELOG.md, which both nursing-mastery and ScraperVault
    keep as dated, thematic prose ("a day that shipped forty PRs gets grouped by
    what actually changed for a nurse") and hold complete with a coverage gate
    that asserts every merged PR appears in it.

    This is a far better answer than Linear for shipped work: these repos merge
    hundreds of PRs a fortnight, while Linear's completed feed is capped and
    ordered by last-touched. Use Linear for what is OPEN, and this for what is
    DONE.

    Returns TWO things, and the difference matters:

    * ``index`` — every entry in the window as ``{date, title}``. Cheap, and
      always complete for the period asked for. Read this first; it is how you
      see the whole fortnight rather than the newest slice of it.
    * ``entries`` — full bodies, for the newest ``limit`` entries or exactly the
      ``dates`` you ask for (comma-separated ``YYYY-MM-DD``).

    Why: this repo's fortnight is ~340KB of prose. Handed all of it, a reader
    takes the top and stops — which is how an agent explained the product from
    three days of a window it had asked fourteen days for, and never mentioned
    that sign-in had moved eight days earlier. The index makes the whole period
    visible at a glance; a second call fetches only the bodies that matter.

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
    wanted = {d.strip() for d in dates.split(",") if d.strip()}
    index = []
    entries = []
    bodies_truncated = []
    for match in re.finditer(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[-–—]?\s*(.*)$", text, re.M):
        date, title = match.group(1), match.group(2).strip()
        if date < cutoff:
            break  # newest-first, so the first old entry ends the window
        # The index is ALWAYS complete for the window — titles are cheap, and
        # seeing every headline is what stops a reader mistaking the newest
        # slice for the period.
        index.append({"date": date, "title": title})
        take = (date in wanted) if wanted else (len(entries) < limit)
        if not take:
            continue
        body = text[match.end() : text.find("\n## ", match.end())].strip()
        if len(body) > BODY_CHARS:
            bodies_truncated.append(date)
            body = body[:BODY_CHARS]
        entries.append({"date": date, "title": title, "body": body})
    in_window = len(index)

    return json.dumps(
        {
            "repo": repo_key,
            "github": REPO_GITHUB[repo_key],
            # `since` is what was ASKED for. `covers` is what is actually in
            # this payload — they differ the moment `limit` bites, and the gap
            # is the whole bug this reports. Cleo was asked to explain the
            # product, requested 21 days, received the newest 25 entries
            # (three days), and correctly said "8/3-8/5" — while the auth
            # change she needed sat 8 days outside what she was handed. A tool
            # that names a window it did not deliver makes the reader wrong.
            "since": cutoff,
            "covers": {
                "from": entries[-1]["date"] if entries else None,
                "to": entries[0]["date"] if entries else None,
            },
            "index": index,
            "index_complete": True,
            "complete": len(entries) == in_window and not bodies_truncated,
            "entries_in_window": in_window,
            "entries_returned": len(entries),
            "entries_omitted": max(in_window - len(entries), 0),
            "bodies_truncated": bodies_truncated,
            "truncation_note": (
                None
                if len(entries) == in_window and not bodies_truncated
                else (
                    f"`index` lists ALL {in_window} entries since {cutoff} and is "
                    f"complete — read it to see the whole period. Full bodies are "
                    f"included for only {len(entries)} of them"
                    + (f" ({len(bodies_truncated)} cut at {BODY_CHARS} chars)" if bodies_truncated else "")
                    + ". Scan the index, then call again with "
                    f"`dates=\"YYYY-MM-DD,YYYY-MM-DD\"` for the entries that matter. "
                    f"Do NOT describe the bodies you happen to have as the full period."
                )
            ),
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


@mcp.custom_route("/verify-source", methods=["POST"])
async def verify_source_route(request: Request) -> JSONResponse:
    """Validate a private repository path through the authenticated checkout."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "A JSON body is required."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "A JSON object is required."}, status_code=400)
    result = json.loads(
        verify_source_ref(
            str(payload.get("repo") or ""),
            str(payload.get("path") or ""),
            str(payload.get("commitSha") or "") or None,
        )
    )
    return JSONResponse(result)


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
