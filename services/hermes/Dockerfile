FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash hermes

WORKDIR /app

# Hermes agent from upstream. This install is REQUIRED: a container without the
# CLI can only ever serve a health stub, so fail the build loudly rather than
# shipping a service that looks green and does nothing. Render keeps the last
# live deploy when a build fails, so a broken upstream never takes the
# service down.
ARG HERMES_REF=main
RUN git clone --depth=1 --branch "${HERMES_REF}" \
      https://github.com/NousResearch/hermes-agent.git /opt/hermes \
    && pip install --no-cache-dir -e /opt/hermes \
    && { command -v hermes >/dev/null || { echo "hermes CLI missing after install"; exit 1; }; }

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN chmod +x /app/entrypoint.sh /app/seed_memory.py \
    && mkdir -p /data/hermes \
    && chown -R hermes:hermes /app /data /opt/hermes

ENV HERMES_HOME=/data/hermes
ENV PORT=8080
ENV HOME=/home/hermes

USER hermes

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
