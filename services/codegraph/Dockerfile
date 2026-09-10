FROM node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS gitnexus-build
ARG NPM_VERSION=12.0.2
ARG GITNEXUS_VERSION=1.6.11

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global "npm@${NPM_VERSION}" \
    && npm install --global "gitnexus@${GITNEXUS_VERSION}"

# The Python facade has no Hermes dependency or <3.14 constraint. Keep the
# native Node modules on the same Bookworm ABI while updating its Python host.
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates libstdc++6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gitnexus-build /usr/local/bin/node /usr/local/bin/node
COPY --from=gitnexus-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -sf /usr/local/lib/node_modules/gitnexus/dist/cli/index.js /usr/local/bin/gitnexus

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PATH="/opt/venv/bin:$PATH"
ENV GITNEXUS_HOME=/data/gitnexus
ENV REPOS_DIR=/data/repos
ENV PORT=8080
ENV GITNEXUS_MCP_READ_ONLY=1

RUN mkdir -p /data/gitnexus /data/repos \
    && chmod +x /app/entrypoint.sh /app/reindex.sh

# Import/auth/native-module proof only; never starts the server or reindexer.
RUN python /app/assert_runtime.py

EXPOSE 8080

CMD ["/app/entrypoint.sh"]
