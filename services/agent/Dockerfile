FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash hermes

WORKDIR /app

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
ARG HERMES_REF=83a1ca686207ef797e4eb86a46725dfe7d9a2f10
COPY hermes_patches /tmp/hermes-patches
RUN git init /opt/hermes \
    && git -C /opt/hermes remote add origin \
      https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes fetch --depth=1 origin "${HERMES_REF}" \
    && git -C /opt/hermes checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/hermes rev-parse HEAD)" = "${HERMES_REF}" \
    && git -C /opt/hermes apply --check /tmp/hermes-patches/pre_gateway_dispatch_before_typing.patch \
    && git -C /opt/hermes apply /tmp/hermes-patches/pre_gateway_dispatch_before_typing.patch \
    && pip install --no-cache-dir -e "/opt/hermes[slack,mcp,tts-premium,fal,firecrawl]" \
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
    && grep -q '"pre_llm_call"' /opt/hermes/agent/turn_context.py \
    && python /tmp/hermes-patches/assert_pre_gateway_dispatch.py /opt/hermes

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
