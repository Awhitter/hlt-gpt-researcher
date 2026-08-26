# Hosted GPT Researcher MCP

> Operator front door: [`START-HERE.md`](./START-HERE.md).

HLT hosts this fork as two Railway services:

- API/frontend: `https://gpt-researcher-api-production.up.railway.app`
- MCP: `https://gpt-researcher-mcp-production.up.railway.app/mcp`

The browser frontend uses the team-password sign-in. REST and MCP use separate
machine credentials.

For operational guidance, decision rules, and Sidecar/Katailyst use cases, see
[`docs/usage/owners-manual.md`](./owners-manual.md).

## MCP Client Config

Claude Code / Cursor `.mcp.json`:

```json
{
  "mcpServers": {
    "gpt-researcher": {
      "type": "http",
      "url": "https://gpt-researcher-mcp-production.up.railway.app/mcp",
      "headers": {
        "Authorization": "Bearer ${GPTR_MCP_TOKEN}"
      }
    }
  }
}
```

Claude Desktop:

```json
{
  "mcpServers": {
    "gpt-researcher": {
      "type": "http",
      "url": "https://gpt-researcher-mcp-production.up.railway.app/mcp",
      "headers": {
        "Authorization": "Bearer ${GPTR_MCP_TOKEN}"
      }
    }
  }
}
```

Codex `~/.codex/config.toml`:

```toml
[mcp_servers.mastery_research]
url = "https://gpt-researcher-mcp-production.up.railway.app/mcp"
bearer_token_env_var = "GPTR_MCP_TOKEN"
```

K2/Hermes agents need only these runtime variables; the shared adapter creates
the mount automatically:

```text
GPTR_MCP_URL=https://gpt-researcher-mcp-production.up.railway.app/mcp
GPTR_MCP_TOKEN=<same value as the service MCP_AUTH_TOKEN>
```

Recommended agent flow: `deep_research` → keep the returned `research_id` →
`write_report` → `get_research_sources`, `get_research_context`, or
`get_research_images`.

## MCP Curl Smoke

Local run from the repo root:

```bash
MCP_AUTH_TOKEN=dev-token \
RESEARCH_RUN_STORE_PATH=data/research_runs.sqlite3 \
OUTPUTS_DIR=outputs \
MCP_ALLOWED_HOSTS=127.0.0.1:8001,localhost:8001,127.0.0.1,localhost,0.0.0.0 \
python -m uvicorn mcp_server.server:app --host=0.0.0.0 --port=8001
```

FastMCP validates the full `Host` header, so include the local port in
`MCP_ALLOWED_HOSTS` when overriding the default.

Initialize and capture the MCP session id:

```bash
headers="$(mktemp)"
curl -sS -D "$headers" -X POST "https://gpt-researcher-mcp-production.up.railway.app/mcp" \
  -H "Authorization: Bearer $GPTR_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0.0"}}}'
session_id="$(awk 'tolower($1) == "mcp-session-id:" {print $2}' "$headers" | tr -d '\r' | head -1)"
```

List tools:

```bash
curl -sS -X POST "https://gpt-researcher-mcp-production.up.railway.app/mcp" \
  -H "Authorization: Bearer $GPTR_MCP_TOKEN" \
  -H "mcp-session-id: $session_id" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Call quick search:

```bash
curl -sS -X POST "https://gpt-researcher-mcp-production.up.railway.app/mcp" \
  -H "Authorization: Bearer $GPTR_MCP_TOKEN" \
  -H "mcp-session-id: $session_id" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"quick_search","arguments":{"query":"NCLEX-RN pass rate 2026","summary":true}}}'
```

## REST API Curl

Health is public:

```bash
curl -fsS "https://gpt-researcher-api-production.up.railway.app/health"
```

API calls require `X-API-Key`:

```bash
curl -sS -X POST "https://gpt-researcher-api-production.up.railway.app/api/quick_search" \
  -H "X-API-Key: $API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"NCLEX-RN pass rate 2026","summary":true}'
```

Deep report:

```bash
curl -sS -X POST "https://gpt-researcher-api-production.up.railway.app/report/" \
  -H "X-API-Key: $API_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task":"Research NCLEX-RN pass rate changes in 2026","report_type":"research_report","report_source":"web","tone":"Objective","repo_name":"","branch_name":"","generate_in_background":false}'
```

## Make.com Strict Research Adapter

Make.com and other simple HTTP clients can run the proven strict MCP sequence
without creating an MCP session:

```bash
curl -sS -X POST \
  "https://gpt-researcher-mcp-production.up.railway.app/automation/research/v1" \
  -H "Authorization: Bearer $GPTR_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "research_automation_request.v1",
    "request_id": "make-research-2026-08-26-001",
    "query": "What does the specified authority document?",
    "report_prompt": "Cite every required source and state scope limits.",
    "scope": "none",
    "depth": "balanced",
    "max_sources_per_query": 8,
    "include_generated_images": false,
    "source_policy": {
      "version": "source_policy.v1",
      "enforcement": "strict",
      "discovery_mode": "required_only",
      "required_sources": [
        {
          "id": "official-program",
          "family": "official",
          "url": "https://authority.example/program"
        }
      ],
      "min_accepted_sources": 1,
      "min_content_chars": 100,
      "require_title": true,
      "require_required_sources_cited": true,
      "independent_judge_required": true
    }
  }'
```

The route uses the MCP service's existing Bearer middleware. Its request and
result versions are `research_automation_request.v1` and
`research_automation_result.v1`. `request_id` is the idempotency key: the same
ID and canonical payload return the stored result with
`idempotent_readback=true`; a changed payload under that ID returns HTTP 409
without starting research. A currently owned request returns HTTP 202 with
`Retry-After`. Long runs heartbeat their durable lease. After the one-hour
default stale boundary (`AUTOMATION_RESEARCH_STALE_SECONDS`, minimum five
minutes), a replacement worker rotates the lease generation before reconciling
or resuming the deterministic core run; the prior worker is fenced from writing
the terminal receipt or starting report generation after it loses ownership.
Core runs marked `interrupted_by_restart` remain recoverable HTTP 202 receipts
until that boundary, then are re-run under the replacement lease.

Expected source/report rejection returns HTTP 200 with `status="failed"`,
`publishable=false`, and the durable manifest/quality receipt. A successful
result requires a passed source manifest, no missing or unadmitted citations,
and an independent-judge pass. The facade returns the accepted report, cost,
source/image counts, and `delivery.attempted=false`. It never publishes, sends,
or writes Airtable; those remain separate governed effects.

## Auth And Rotation

- API service secret: `API_AUTH_KEY`, sent as `X-API-Key`.
- MCP service secret: `MCP_AUTH_TOKEN`, sent as `Authorization: Bearer ...`.
- Local MCP client secret: `GPTR_MCP_TOKEN`; set it to the same value as
  `MCP_AUTH_TOKEN` for clients.
- Durable run metadata: `RESEARCH_RUN_STORE_PATH`, backed by SQLite. On Railway,
  set this to a mounted volume path such as `/data/research_runs.sqlite3`.
- Generated report/log files: `OUTPUTS_DIR`. On Railway, set this to the same
  mounted volume, such as `/data/outputs`.
- Volume permissions: Railway mounts volumes as `root`; these hosted Docker
  services set `RAILWAY_RUN_UID=0` so SQLite metadata and report files are
  writable on the mounted volume.

To rotate:

1. Set a new `API_AUTH_KEY` or `MCP_AUTH_TOKEN` in Railway.
2. Redeploy the affected service.
3. Update local client env (`API_AUTH_KEY` or `GPTR_MCP_TOKEN`).
4. Re-run the smoke commands above.

## Katailyst Integration

Katailyst integration lives outside the public GPT Researcher browser UI. The
canonical path is:

1. Register/discover `tool:gpt-researcher.mcp` in Katailyst.
2. Let agents mount the hosted GPT Researcher MCP endpoint with
   `Authorization: Bearer ${GPTR_MCP_TOKEN}`.
3. Keep Katailyst credentials in the calling agent/runtime, not in browser
   source, local storage, or a public MCP textarea.

This keeps the upstream-tracked GPT Researcher UI close to upstream while still
letting HLT agents compose GPT Researcher with Katailyst.

## UI WebSocket Smoke

After deploying API changes, verify the browser auth path and WebSocket startup
without running a full report:

```bash
.venv/bin/python scripts/smoke_websocket_ui.py \
  --ui-url https://gpt-researcher-ui.vercel.app \
  --api-url https://gpt-researcher-api-production.up.railway.app \
  --scope codebase,metrics,firecrawl \
  --allow-degraded-scope
```

The command should print `hlt_scope_active` / `hlt_scope_degraded` lines and
exit before a full report is generated. Omit `--allow-degraded-scope` when the
deployment should fail the smoke test unless all requested scopes are ready.

## Tools

- `deep_research` creates a `research_id` and returns context, sources, source
  URLs, attributed images, and counts. `depth` uses 5/8/12 results per query for
  fast/balanced/deep; `max_sources_per_query` can override that from 3–20.
  `include_generated_images=true` opts into contextual illustrations. Source
  images remain automatic for ordinary runs. Strict runs deliberately do not
  fetch page images from the MCP service network; their only image option is
  the separately attributed, opt-in generated-image path. Completed run
  metadata is persisted in SQLite and can be recovered after an MCP/API restart.
- `deep_research.source_policy` can make evidence selection strict. Use
  `enforcement: "strict"`, `discovery_mode: "required_only"`, and typed
  `required_sources` when a canary or regulated brief must use exact sources.
  The run returns `source_manifest.v1`; disallowed candidates are blocked before
  context, required pages with missing titles/content fail closed, and the
  normalized manifest survives restart.

  Strict mode supports at most 32 required sources and 64 allow/deny domains,
  requires at least 100 characters of titled page content, and caps the judged
  report at 40,000 characters. It always uses the remote Firecrawl scraper and
  fails before model or retrieval spend with `strict_scraper_unavailable` unless
  the Firecrawl Python package, `FIRECRAWL_API_KEY`, and a public
  `FIRECRAWL_SERVER_URL` are available. The MCP service validates each requested
  target plus Firecrawl's requested/resolved URL attestations, and bypasses the
  Firecrawl content cache for strict evidence. Redirect-hop egress inside the
  remote Firecrawl service remains a provider control.

  ```json
  {
    "source_policy": {
      "enforcement": "strict",
      "discovery_mode": "required_only",
      "min_accepted_sources": 2,
      "required_sources": [
        {
          "id": "official-program",
          "family": "official",
          "url": "https://authority.example/program"
        },
        {
          "id": "peer-reviewed-outcomes",
          "family": "peer-reviewed",
          "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC10907523/"
        }
      ]
    }
  }
  ```
- `quick_search` returns fast search results or a summary.
- `write_report` accepts a prior `research_id`; if the hot cache was lost, it
  hydrates from persisted context and source metadata.
- For strict runs, `write_report` also extracts every report URL, requires the
  required sources to be cited, and invokes a separate evidence judge. Draft
  text such as `PASS` has no effect on acceptance. Failed drafts remain
  inspectable but return `publishable: false` and `report_quality.v1`. A failed
  first candidate closes that run to further writes. If an accepted report
  already exists, a later rejected custom-prompt revision is stored separately;
  the accepted artifact and publishable run remain intact, and readback exposes
  both the accepted and rejected revision receipts.
- `get_research_sources` accepts a prior `research_id`.
- `get_research_context` accepts a prior `research_id`.
- `get_research_images` accepts a prior `research_id` and returns image URLs,
  source-page URLs, and alt text.
- `research://{topic}` returns cached, persisted, or newly generated research
  context.
