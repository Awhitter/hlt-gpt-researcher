#!/usr/bin/env bash
# Smoke the two hosted Mastery Research doors after the Firecrawl env change:
#   1. MCP quick_search (session init + tools/call)
#   2. Nonblocking automation job start (idempotent request_id)
# Receipts print sizes and status only — never token material.
set -euo pipefail

BASE="${GPTR_MCP_BASE:-https://gpt-researcher-mcp-production.up.railway.app}"
TOK="${GPTR_MCP_TOKEN:?set GPTR_MCP_TOKEN (never echo it)}"
QUERY="${SMOKE_QUERY:-US nurse residency program turnover reduction evidence 2026}"
JOB_ID="${SMOKE_JOB_ID:-estate-smoke-$(date +%Y-%m-%d)-a}"
JOB_QUERY="${SMOKE_JOB_QUERY:-What are the strongest evidence-backed drivers of US hospital nurse turnover in 2025-2026, and which retention interventions show measurable effect sizes?}"
tmp="$(mktemp -d)"

curl -sS -D "$tmp/h.txt" --max-time 30 -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"estate-smoke","version":"1.0"}}}' \
  -o "$tmp/init.txt"
SID="$(grep -i '^mcp-session-id' "$tmp/h.txt" | awk '{print $2}' | tr -d '\r')"
echo "MCP_SESSION_OK len=${#SID}"

curl -sS --max-time 15 -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOK" -H "mcp-session-id: $SID" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' -o /dev/null

curl -sS --max-time 180 -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOK" -H "mcp-session-id: $SID" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data "$(printf '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"quick_search","arguments":{"query":"%s"}}}' "$QUERY")" \
  -o "$tmp/qs.txt"
echo "QS_BYTES=$(wc -c < "$tmp/qs.txt" | tr -d ' ')"
echo "QS_IS_ERROR=$(grep -o '"isError":[a-z]*' "$tmp/qs.txt" | head -1 || echo 'not-reported')"
echo "QS_FIRECRAWL_MENTIONS=$(grep -ci firecrawl "$tmp/qs.txt" || true)"
head -c 300 "$tmp/qs.txt"; echo

echo "---JOB-START---"
curl -sS --max-time 30 -X POST "$BASE/automation/research/jobs/v1/start" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  --data "$(printf '{"request_id":"%s","query":"%s","report_type":"research_report","tone":"Objective"}' "$JOB_ID" "$JOB_QUERY")" \
  | head -c 600
echo
echo "JOB_REQUEST_ID=$JOB_ID"
