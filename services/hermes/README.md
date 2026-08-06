# HLT Hermes Brain (Render)

Persistent NousResearch Hermes agent for Mastery Research: Slack gateway,
memory/skills on a Render disk, MCP mounts to GPT Researcher, codegraph,
Katailyst2, and Linear.

**Status: installed and configured, gateway off.** The container has the
Hermes CLI and writes a real `config.yaml` on every boot, but it will not talk
to anyone until the Slack tokens exist. That last step needs a browser and is
below.

## How the container is put together

`health_gateway.py` is the main process. It:

1. seeds `$HERMES_HOME/memory` from [`seed/`](./seed) (existing files win),
2. renders `$HERMES_HOME/config.yaml` from the environment
   ([`render_config.py`](./render_config.py)),
3. supervises `hermes gateway` as a child when `HERMES_ENABLE_GATEWAY=1`,
4. serves `/health` on `$PORT`.

The supervisor exists for a specific reason: Hermes reaches Slack over **Socket
Mode**, an outbound WebSocket. It never binds a port. Render is running this as
a *web service* with a health check, so if `hermes gateway` were the main
process nothing would answer on `$PORT` and every deploy would be rolled back.
Something has to own the port; that something also gets to report the truth
about whether the agent is up.

### Config is generated, not hand-written

Hermes reads one `config.yaml` under `HERMES_HOME`. `render_config.py` writes
it at boot, mounting only the MCP servers whose URL is actually set, and
writing tokens as `${VAR}` references so the persistent disk never holds a
secret in plaintext. Generated files carry `_generated_by: hlt-render-boot`.

If you hand-edit `config.yaml` on the disk, drop that marker line — boot will
then leave your file alone and say so in `/health`
(`config.preserved_operator_config: true`).

## Env

| Var | Purpose |
|-----|---------|
| `OPENROUTER_API_KEY` | Model credentials (required for the agent to answer) |
| `HERMES_ENABLE_GATEWAY` | `1` starts the Slack gateway; anything else = health only |
| `HERMES_MODEL` | Model override; default `anthropic/claude-sonnet-5` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack gateway (`xoxb-…` / `xapp-…`) |
| `GPTR_MCP_URL` / `GPTR_MCP_TOKEN` | Hosted researcher MCP |
| `CODEGRAPH_MCP_URL` / `CODEGRAPH_MCP_TOKEN` | Estate code graph |
| `KATAILYST2_MCP_URL` / `KATAILYST2_MCP_TOKEN` | Registry |
| `LINEAR_MCP_URL` / `LINEAR_MCP_TOKEN` | Roadmap (optional) |
| `HERMES_HOME` | Persistent disk path (default `/data/hermes`) |

A URL without its token still mounts, unauthenticated — `/health` lists those
under `config.mcp_without_token` so a half-set pair is visible rather than
silently degraded.

## Turning the gateway on (one-time, ~3 minutes in a browser)

Only Alec can do steps 1–3: they create a Slack app and mint tokens.

1. Open https://api.slack.com/apps → **Create New App** → **From an app
   manifest** → choose the HLT workspace → paste
   [`slack-app-manifest.yaml`](./slack-app-manifest.yaml) → **Create**.
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**, scope
   `connections:write`. Copy the `xapp-…` token.
3. **Install App → Install to Workspace** → copy the **Bot User OAuth Token**
   (`xoxb-…`).
4. In Render → `hlt-hermes` → **Environment**, set:
   - `SLACK_BOT_TOKEN` = the `xoxb-…` token
   - `SLACK_APP_TOKEN` = the `xapp-…` token
   - `HERMES_ENABLE_GATEWAY` = `1`
   - `OPENROUTER_API_KEY` if it is not already set
5. Save. Render redeploys automatically.

## Smoke

```bash
curl -s https://hlt-hermes.onrender.com/health | jq
```

Read the answer rather than the status code — the service intentionally stays
up (HTTP 200) even when the agent is down, so you can see *why*:

| `mode` | Meaning |
|--------|---------|
| `readiness_gateway` | Gateway not requested. `status: ok`. Expected before step 4. |
| `gateway` | Agent running. `gateway.uptime_seconds` climbs. |
| `gateway_down` | Gateway requested but not running. `status: degraded`, and `gateway.stopped_reason` / `gateway.last_exit_code` say what happened. |

`config.mcp_mounted` lists the servers Hermes actually received. If a mount you
expect is missing it will be named in `config.mcp_unconfigured`.

Then DM the Slack bot “what can ScraperVault do?” and confirm the reply cites
codegraph and the researcher.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `mode: gateway_down`, `cli_present: false` | The image built without the Hermes CLI. The build now fails loudly on this, so it means an old image is live — redeploy. |
| `mode: gateway_down`, repeated `last_exit_code` | Gateway is crash-looping; the supervisor stops after 5 attempts and leaves the reason in `stopped_reason`. Check Render logs for Hermes' own output. |
| Agent answers but knows nothing about the estate | Check `config.mcp_mounted` — a missing URL means the mount was skipped. |
| `openrouter_key_present: false` | `OPENROUTER_API_KEY` is unset; the agent has no model credentials. |
