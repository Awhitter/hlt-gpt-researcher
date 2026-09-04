ARG HERMES_REF=29112bef099274229cadff79cdff7bf7b99c4b77

FROM node:22-bookworm-slim AS hermes-web
ARG HERMES_REF

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git init /opt/hermes \
    && git -C /opt/hermes remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes fetch --depth=1 origin "${HERMES_REF}" \
    && git -C /opt/hermes checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/hermes rev-parse HEAD)" = "${HERMES_REF}"

WORKDIR /opt/hermes
# Match the mounted computer surface for lazy-loaded JS/CSS too; upstream's
# HTML rewriting alone leaves Vite preload requests pointing at /assets/.
RUN npm ci --workspace=web --workspace=ui-tui --include-workspace-root=false \
    && npm run build --workspace=web -- --base=/computer/ \
    && npm run build --workspace=ui-tui

FROM python:3.12-slim-bookworm
ARG HERMES_REF

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash hermes

WORKDIR /app

# Hermes launches the gateway as a child process whose working directory is the
# durable agent home, not /app.  The numeric-grounding overlay is an HLT module
# imported by patched upstream code, so make its application directory part of
# every Python process' import path rather than only the build-time assertions.
ENV PYTHONPATH=/app
# This HLT image owns one reviewed model ladder. The upstream overlay uses the
# marker to ignore durable /model and channel overrides: Sol through the
# managed Codex profile pool, then authenticated Grok 4.6 only.
ENV HLT_MANAGED_MODEL_ROUTE=1

# Hermes agent from upstream WITH every extra this agent actually uses.
# Upstream ships these as OPTIONAL extras, and every one of them fails the
# same way when missing: the feature is configured, reported healthy, and
# quietly does nothing.
#
#   slack        without it: "No adapter available for slack" — bot never connects
#   mcp          without it: "mcp package not installed -- MCP tool support
#                disabled" at DEBUG. Every MCP server still reports as mounted
#                and granted while the agent has NONE of their tools. This cost
#                a day: Cleo told a teammate her Linear and codegraph tools were
#                "not actually available to me in this session" and she was right.
#   tts-premium  the ElevenLabs client for text_to_speech
#   fal          the FAL client for image_generate
#   firecrawl    the SDK behind the configured Firecrawl web-search provider;
#                lazy installs are disabled in config, so a key without this
#                extra leaves web search selected and impossible to run
#
# Each is asserted below, so a missing extra is a red build rather than a
# silent capability hole. Render keeps the last
# live deploy when a build fails, so a broken upstream never takes the
# service down.
# Pin the upstream runtime. Pulling `main` made rebuilds non-reproducible and
# left no way to know whether Cleo had the MCP naming repair that registers
# tools as `mcp__<server>__<tool>`. Upgrade this SHA deliberately, with the
# contract assertion below and the service gates green.
COPY hlt_numeric_grounding.py /app/hlt_numeric_grounding.py
COPY fleet_run_budget.py /app/fleet_run_budget.py
COPY hermes_patches /tmp/hermes-patches
RUN git init /opt/hermes \
    && git -C /opt/hermes remote add origin \
      https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes fetch --depth=1 origin "${HERMES_REF}" \
    && git -C /opt/hermes checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/hermes rev-parse HEAD)" = "${HERMES_REF}" \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/upstream_stream_final_content_reconciliation.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/upstream_stream_final_content_reconciliation.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/upstream_stream_final_draft_gate.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/upstream_stream_final_draft_gate.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/pre_gateway_dispatch_before_typing.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/pre_gateway_dispatch_before_typing.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/slack_bare_transfer_full_context.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/slack_bare_transfer_full_context.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/quiet_slack_provider_failover.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/quiet_slack_provider_failover.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/codex_pool_readiness_counts.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/codex_pool_readiness_counts.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/slack_single_stream_progress.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/slack_single_stream_progress.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/k2_runtime_pack_prompt_refresh.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/k2_runtime_pack_prompt_refresh.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/api_runs_numeric_grounding.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/api_runs_numeric_grounding.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/progressive_tool_result_compaction.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/progressive_tool_result_compaction.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/platform_turn_budget.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/platform_turn_budget.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/slack_native_agent_lifecycle_recovery.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/slack_native_agent_lifecycle_recovery.patch \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/scheduled_run_budget.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/scheduled_run_budget.patch \
    && pip install --no-cache-dir -e "/opt/hermes[slack,mcp,tts-premium,fal,firecrawl,web]" \
    && { command -v hermes >/dev/null || { echo "hermes CLI missing after install"; exit 1; }; } \
    && { python -c "import slack_bolt, slack_sdk" || { echo "slack extra missing after install"; exit 1; }; } \
    && { python -c "import mcp" || { echo "mcp extra missing after install"; exit 1; }; } \
    && { python -c "import elevenlabs" || { echo "tts-premium extra missing after install"; exit 1; }; } \
    && { python -c "import fal_client" || { echo "fal extra missing after install"; exit 1; }; } \
    && { python -c "from firecrawl import Firecrawl" || { echo "firecrawl extra missing after install"; exit 1; }; }

# Regression for the exact Slack failure Alec saw:
# `Model generated invalid tool call: mcp__codegraph__context`.
# The name must be the registry name, not merely a model-facing alias.
RUN python -c "from tools.mcp_tool import mcp_prefixed_tool_name; assert mcp_prefixed_tool_name('codegraph', 'context') == 'mcp__codegraph__context'"

# Cleo's K2 body adapter intentionally builds on these two supported Hermes
# seams. Keep an upstream upgrade from deploying a wrapper whose async dispatch
# or one-per-turn context hook has silently disappeared.
RUN grep -q 'POST /v1/runs' /opt/hermes/gateway/platforms/api_server.py \
    && grep -q 'GET  /v1/runs/{run_id}' /opt/hermes/gateway/platforms/api_server.py \
    && grep -q '"pre_gateway_dispatch"' /opt/hermes/hermes_cli/plugins.py \
    && grep -q 'skip before adapter processing' /opt/hermes/gateway/platforms/base.py \
    && grep -q '_hermes_pre_gateway_dispatch_done' /opt/hermes/gateway/run.py \
    && grep -q 'pre_gateway_dispatch skip' /opt/hermes/gateway/run.py \
    && grep -q 're.escape(COMPACTION_DONE_STATUS)' /opt/hermes/gateway/run.py \
    && grep -q '"pre_llm_call"' /opt/hermes/agent/turn_context.py \
    && python /tmp/hermes-patches/assert_pre_gateway_dispatch.py /opt/hermes \
    && python /tmp/hermes-patches/assert_provider_failover_request_contract.py /opt/hermes \
    && python -c "from agent.credential_pool import CredentialPool; assert callable(getattr(CredentialPool, 'readiness_counts', None))" \
    && python /tmp/hermes-patches/assert_slack_single_stream_progress.py /opt/hermes \
    && python /tmp/hermes-patches/assert_k2_runtime_pack_prompt_refresh.py /opt/hermes \
    && python /tmp/hermes-patches/assert_progressive_tool_result_compaction.py /opt/hermes \
    && python /tmp/hermes-patches/assert_platform_turn_budget.py /opt/hermes \
    && python /tmp/hermes-patches/assert_scheduled_run_budget.py /opt/hermes \
    && PYTHONPATH=/app python /tmp/hermes-patches/assert_api_runs_numeric_grounding.py /opt/hermes \
    && PYTHONPATH=/app python -c "from hlt_numeric_grounding import NumericGroundingLedger; assert NumericGroundingLedger('applications: 2').validate('Applications: 2').ok"

# Upstream intentionally ignores the generated dashboard and TUI bundles.
# Build both at the exact pinned SHA. The native Chat tab opens a PTY whose
# child is the TUI bundle, so the web assets alone produce a polished shell
# with a dead Chat surface.
COPY --from=hermes-web /usr/local/bin/node /usr/local/bin/node
COPY --from=hermes-web /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version
COPY --from=hermes-web /opt/hermes/hermes_cli/web_dist /opt/hermes/hermes_cli/web_dist
COPY --from=hermes-web /opt/hermes/ui-tui/dist /opt/hermes/ui-tui/dist
ENV HERMES_TUI_DIR=/opt/hermes/ui-tui
RUN test -s /opt/hermes/hermes_cli/web_dist/index.html \
    && test -s /opt/hermes/ui-tui/dist/entry.js \
    && python -c "from pathlib import Path; from hermes_cli.main import _make_tui_argv; argv, cwd = _make_tui_argv(Path('/opt/hermes/ui-tui'), False); assert argv[0].endswith('/node') and argv[-1] == '/opt/hermes/ui-tui/dist/entry.js' and cwd == Path('/opt/hermes/ui-tui'), (argv, cwd)"

ENV HERMES_UPSTREAM_REF=${HERMES_REF}

# The gateway argv must be one upstream actually accepts. `hermes gateway -v`
# parses as an unknown arg: -v/-q/--external-supervisor are declared on the
# `gateway run` sub-subparser, not on `gateway`. argparse then prints usage and
# exits non-zero, which the supervisor sees only as a crash-looping child — the
# flag we added to make failures visible instead made the gateway unbootable.
# Assert the real parser accepts them, so an upstream rename is a red build.
RUN set -e; \
    hermes gateway run --help > /tmp/gw-help.txt 2>&1 \
      || { echo "hermes gateway run --help failed:"; cat /tmp/gw-help.txt; exit 1; }; \
    for flag in --verbose --external-supervisor; do \
      grep -q -- "$flag" /tmp/gw-help.txt \
        || { echo "gateway run no longer accepts $flag"; cat /tmp/gw-help.txt; exit 1; }; \
    done; \
    rm -f /tmp/gw-help.txt

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
# /app/grounding holds AGENTS.md — the selected agent's durable briefing, loaded by Hermes'
# project-context discovery from terminal.cwd. It stays root-owned and
# world-readable so the agent can read it but never rewrite its own facts.
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /data/hermes \
    && chown -R hermes:hermes /app /data /opt/hermes \
    && chown -R root:root /app/grounding \
    && chmod -R a-w,a+rX /app/grounding

ENV HERMES_HOME=/data/hermes
ENV PORT=8080
ENV HOME=/home/hermes

USER hermes

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
