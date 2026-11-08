# HLT Codegraph MCP (GitNexus)

Keeps six estate checkouts current (Mastery Research, katailyst2, the Vault,
nursing-mastery, MMM2, Evidence-Based-Business) and serves their code over
streamable HTTP to Mastery Research's code scope and to Katailyst agents. Every
repo refreshes its code daily as source only: GitNexus graph rebuilds of the large
repos ran the 2 GB plan out of memory, which had left five of them frozen at
Aug-Sep copies until 8 Nov 2026. Older graphs stay on disk and still answer the
structural tools; `/readiness` shows their date as `structuralIndexedAt`.
`HLT-Master/hlt-web-service` left the default list on 8 Nov 2026 because the
GitHub account can no longer read it; add it back through `CODEGRAPH_REPOS` with
a token that can.

The image uses Node 24 LTS (pinned by digest), npm 12.0.2, and GitNexus 1.6.11.
The Python facade uses stable Python 3.14 on a pinned Bookworm image; Node and
its native modules come from the same Debian release. Cleo separately keeps
Python 3.13 because its Hermes runtime declares a lower maximum.
GitNexus is pinned to a reviewed stable release so a routine rebuild cannot
silently change the graph format or CLI. Existing source-only repositories,
boot-index choice, and refresh interval remain controlled by the same settings.

The Python MCP dependency intentionally remains below version 2. Current MCP
1.30 supports Python 3.14 and retains `mcp.server.fastmcp`, which this facade
uses; MCP 2.2 removed that module. The other Python dependency floors allow
current stable compatible releases. The image runs `assert_runtime.py` to
check installed versions, dependency consistency, native Node imports, actual
public-health output, and authentication decisions. This gate does not start
the server, clone repositories, create indexes, or load embedding models.

## Tools

- Exact source evidence: `search_source`, `read_source`, `verify_source_ref`
- Structural analysis: `list_repos`, `query`, `context`, `impact`, `trace`,
  `repo_overview`

Implementation questions should start with `search_source`, follow promising
matches with `read_source`, and cite the returned immutable URL. The structural
tools explain relationships but are not a substitute for reading the current
file before making a behavior claim.

## Endpoints

| Path | Auth | Returns |
|------|------|---------|
| `/health` | public | HTTP 200 liveness with a generic index warning: `status` is `degraded` when disk space is low, refresh is running/failed/stalled, or its status is missing/stale/unreadable; no repository identities |
| `/readiness` | bearer | per-repo index status, branch, commit SHA, `gitnexus_home` |
| `/verify-source` | bearer | validates one repo/path/full-commit tuple in the private checkout |
| `/mcp` | bearer | the tool surface |

`/health` is deliberately bare. Render's ipAllowList is `0.0.0.0/0`, so
anything it returns is world-readable, and index truth names five private
repositories and their commit SHAs. If `CODEGRAPH_MCP_TOKEN` is unset the
service **fails closed**: every path except `/health` returns 503.
`tests/test_codegraph_auth.py` pins both.

## Env

| Var | Purpose |
|-----|---------|
| `GITHUB_TOKEN` | Read-only key for the private repos (fine-grained, Contents: read). Git gets it from the environment on every run; it is never written into a checkout's `.git/config`, and older clones that had it in origin's URL are cleaned on their next refresh |
| `CODEGRAPH_MCP_TOKEN` | Bearer auth for `/mcp` and `/readiness`; unset ⇒ 503 |
| `CODEGRAPH_REPOS` | Optional `slug\|org/repo,...` override |
| `CODEGRAPH_REINDEX_HOURS` | Background reindex interval (default 24; `0` off) |
| `CODEGRAPH_SOURCE_ONLY_REPOS` | CSV checkouts refreshed for source tools but not GitNexus (default: all six). Remove a slug to rebuild its graph again, only on a plan that can hold the rebuild |
| `CODEGRAPH_SKIP_REPOS` | CSV repos left completely untouched (code and graph frozen). Empty by default; use only to protect a checkout during recovery |
| `CODEGRAPH_SKIP_INDEX_ON_BOOT` | `1` to skip boot reindex |
| `CODEGRAPH_MIN_FREE_MB` | Free-space reserve (default 1024). Refresh prunes unused cached Git objects; if the reserve is still unmet, it preserves the checkout/index and reports failure. Unknown disk readings never trigger destructive recovery. `/health` uses the same threshold |
| `PORT` | Bind port (Render sets this) |

## Recovery and verification

Boot, scheduled and manual reindex runs share a nonblocking file lock. A second
run leaves the first run and its status alone. Frozen repositories are untouched;
an existing failure marker still counts as failed. A recent freshness stamp cannot
hide a failed refresh. Partial failure returns a nonzero exit code after trying
the other configured repositories.

Cleanup never deletes a checkout, `.git`, `.gitnexus`, or Git lock files. It expires
old reflogs, removes unused remote-tracking refs and prunes cached objects only for
configured, non-frozen checkouts being refreshed. Git cleanup retains the normal
90/30-day reflog and two-week object grace periods to protect independent writers;
it never uses immediate pruning. A retry remains unverified until it finishes;
starting it cannot erase a degraded health signal. If a full disk prevents Git GC,
or a checkout is incomplete, the run stops refreshing that repository and keeps
the last available data for an operator. Do not remove an index to make space:
rebuilding it can exceed this service's memory limit.

After an authorized release, verify the serving commit in Render, disk headroom,
and a completed scheduled refresh with no errors. Public `/health` must remain
HTTP 200 without exposing repository names; its `ok` status is not sufficient
proof. Authenticated `/readiness` must show the expected repository SHAs and fresh
timestamps, and a source read must resolve an immutable link at that SHA. Frozen
repositories remain explicitly stale. No endpoint or credential setting needs to
change for this repair.

The regression suite uses disposable local Git repositories and a stub graph CLI:
`python -m pytest tests/test_codegraph_auth.py`. It does not rebuild production
indexes or prove that the production disk has recovered.

## Local

```bash
docker build -t hlt-codegraph .
docker run --rm -p 8080:8080 \
  -e GITHUB_TOKEN \
  -e CODEGRAPH_MCP_TOKEN=dev \
  -v codegraph-data:/data \
  hlt-codegraph
```

Point GPT Researcher at it:

```
CODEGRAPH_MCP_URL=https://<service>/mcp
CODEGRAPH_MCP_TOKEN=dev
```
