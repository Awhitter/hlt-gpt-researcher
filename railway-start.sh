#!/usr/bin/env bash
# Start dispatcher for the two HLT services that share this repo root and its
# Dockerfile: Render `hlt-research-api` and `hlt-research-mcp` (render.yaml),
# chosen by GPT_RESEARCHER_SERVICE. The name is historical: the Railway services
# they replace run the same script.

set -euo pipefail

port="${PORT:-8000}"
service="${GPT_RESEARCHER_SERVICE:-${RAILWAY_SERVICE_NAME:-api}}"

echo "Starting GPT Researcher service target: ${service}"

case "$service" in
  gpt-researcher-mcp|mcp)
    exec python -m uvicorn mcp_server.server:app --host=0.0.0.0 --port="${port}"
    ;;
  *)
    exec python -m uvicorn backend.server.app:app --host=0.0.0.0 --port="${port}"
    ;;
esac
