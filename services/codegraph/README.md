# HLT Codegraph MCP (GitNexus)

Maintains seven current estate checkouts and exposes source tools over
streamable HTTP for GPT Researcher and agent consumers. Five repositories have
GitNexus structural indexes; `HLT-Master/hlt-web-service` and Mastery Research
itself are deliberately source-only so account and provider answers can inspect
current code without risking another memory-heavy graph rebuild.

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
| `/health` | public | liveness only — `{"status","service"}` |
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
| `GITHUB_TOKEN` | Clone private/public repos |
| `CODEGRAPH_MCP_TOKEN` | Bearer auth for `/mcp` and `/readiness`; unset ⇒ 503 |
| `CODEGRAPH_REPOS` | Optional `slug\|org/repo,...` override |
| `CODEGRAPH_REINDEX_HOURS` | Background reindex interval (default 24; `0` off) |
| `CODEGRAPH_SOURCE_ONLY_REPOS` | CSV checkouts refreshed for source tools but not GitNexus (default `hlt-web-service,hlt-gpt-researcher`) |
| `CODEGRAPH_SKIP_INDEX_ON_BOOT` | `1` to skip boot reindex |
| `PORT` | Bind port (Render sets this) |

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
