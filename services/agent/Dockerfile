FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash hermes

WORKDIR /app

# Hermes agent from upstream, WITH the [slack] extra. slack-bolt and slack-sdk
# are optional dependencies upstream, so a plain `-e /opt/hermes` builds and
# runs fine and then logs "No adapter available for slack" at boot — the
# gateway process stays alive serving cron, the health check stays green, and
# the bot never connects to Slack at all. Both the CLI and the extra are
# asserted here so that failure is a red build, not a silent one. Render keeps the last
# live deploy when a build fails, so a broken upstream never takes the
# service down.
ARG HERMES_REF=main
RUN git clone --depth=1 --branch "${HERMES_REF}" \
      https://github.com/NousResearch/hermes-agent.git /opt/hermes \
    && pip install --no-cache-dir -e "/opt/hermes[slack]" \
    && { command -v hermes >/dev/null || { echo "hermes CLI missing after install"; exit 1; }; } \
    && { python -c "import slack_bolt, slack_sdk" || { echo "slack extra missing after install"; exit 1; }; }

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
# /app/grounding holds AGENTS.md — Brian's durable briefing, loaded by Hermes'
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
