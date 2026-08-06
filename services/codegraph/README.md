# HLT Codegraph MCP (GitNexus)

Indexes the five estate repos into a GitNexus knowledge graph and exposes
structural tools over streamable HTTP for GPT Researcher + Hermes.

## Tools

- `list_repos`, `query`, `context`, `impact`, `trace`, `repo_overview`

## Endpoints

| Path | Auth | Returns |
|------|------|---------|
| `/health` | public | liveness only — `{"status","service"}` |
| `/readiness` | bearer | per-repo index status, branch, commit SHA, `gitnexus_home` |
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
