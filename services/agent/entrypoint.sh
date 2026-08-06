#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data/hermes}"
mkdir -p "$HERMES_HOME"

# health_gateway.py owns $PORT (Render's health check needs it bound), installs
# this agent's grounding, renders config.yaml from the environment, and supervises
# `hermes gateway` as a child when AGENT_ENABLE_GATEWAY=1. The gateway talks to
# Slack over Socket Mode and binds nothing itself, so it cannot be PID 1.
exec python /app/health_gateway.py
