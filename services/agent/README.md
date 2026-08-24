# HLT agent runtime adapter

Mastery Research is the reusable research core. Katailyst2 is the canonical
registry/orchestration layer for product and facilitator agents. This directory
packages those agents for the current Hermes/Slack deployment; it is not the
authority that turns Mastery Research into a Nursing-Mastery-only tool.

One image, two personas. `AGENT_ID` picks which boots: **`cleo`** (product
owner/facilitator for Nursing Mastery — one consumer of the research core and
the default) or **`brian`** (general Mastery Researcher).
One Hermes gateway binds to exactly one Slack app, so a second agent means a
second Render service off this same image.

The Slack face of the estate. Runs the NousResearch Hermes runtime with
MCP mounts to GPT Researcher, codegraph, Katailyst2 and Linear, with memory on a
Render disk.

**"Hermes" is the runtime, not the agent.** `agent:hermes`
in Katailyst2 is the fleet orchestrator persona — a different thing — so never
call these agents Hermes in docs, Slack, or the registry. The Render service keeps the
hostname `hlt-hermes` only because Render cannot rename a service in place.

Do not preserve a dated “live” claim here. `/health` is the current authority:
it separates configured routes from observed provider success, verifies Slack's
actual OAuth grant, proves the canonical Katailyst2 door and Cleo contract, and
reports the reversible retirement state of the old recurring briefs.

## How the container is put together

`health_gateway.py` is the main process. It:

1. installs the reviewed bundled `SOUL.md`/`AGENTS.md` outage fallback and the
   `hlt-k2-context` Hermes plugin ([`grounding.py`](./grounding.py)),
2. renders `$HERMES_HOME/config.yaml` from the environment
   ([`render_config.py`](./render_config.py)),
3. asks canonical Katailyst2 for the bearer-bound `agent:cleo` runtime pack,
   installs that pack into Hermes' real prompt files only when it is active,
   and proves the independent mission-time wishing well,
4. selects one addressed HLT Slack agent before Hermes can type or call a model,
5. supervises `hermes gateway` as a child when `AGENT_ENABLE_GATEWAY=1`,
6. serves operator health plus authenticated K2 activation/dispatch routes on
   `$PORT`.

The supervisor exists for a specific reason: Hermes reaches Slack over **Socket
Mode**, an outbound WebSocket. It never binds a port. Render runs this as a *web
service* with a health check, so if `hermes gateway` were the main process
nothing would answer on `$PORT` and every deploy would roll back.

## Security posture — read before widening anything

Upstream's default Slack toolset is `hermes-slack`, whose own description is
*"full access for workspace use"*: `terminal`, `execute_code`, `write_file`,
`patch`, `cronjob`, `computer_use`, `browser_cdp`. Left at that default, anyone
who can @mention the agent gets arbitrary code execution on this container.

These agents also read untrusted third-party web pages. Shell access plus untrusted
input is a confused deputy: a hostile page talks the model into running a
command. Upstream constrains its own webhook toolset for exactly this reason.

So `render_config.SLACK_TOOLSETS` pins a narrow list — web, search, vision,
skills, todo, memory, session_search, clarify — and
`tests/test_agent_boot.py::test_slack_toolset_excludes_host_access` fails the
build if a host-access tool creeps back in. **Do not widen it without reading
that test.**

Two env vars matter as much as the toolset:

- `SLACK_ADMIN_USERS` — **set this before going live.** With no admin list,
  Hermes disables slash-command gating entirely and every workspace member can
  run `/model`, `/yolo` and `/cron`. `/health` reports
  `config.slack_admins_configured: false` when it is missing, and boot logs a
  warning.
- `SLACK_ALLOWED_CHANNELS` — channel whitelist. Covers group DMs but **not**
  1:1 DMs.

## Where an agent's knowledge lives

| Slot | Content | Why there |
|---|---|---|
| Katailyst2 `agents.runtime_pack` for `agent:cleo` | active identity, doctrine, proclivities, bindings and policies | canonical brain, resolved for `paperclip_hermes` with the agent-bound token |
| [`grounding/cleo/SOUL.md`](./grounding/cleo/SOUL.md) + bundled AGENTS files | reviewed snapshot | used only during a declared K2 service/transport outage, never for a missing agent or bad token |
| `hlt-k2-context` Hermes plugin | choose one Slack lead, then draw one bounded `katailyst.well` packet for each substantive turn | lead decisions are private local receipts; mission context is ephemeral and never persisted into transcript or memory |
| `$HERMES_HOME/memories/MEMORY.md` | genuinely learned deltas | agent-written, approval-gated, ~2200 chars |

Cleo's durable registry identity is the unversioned `agent:cleo`; K2 owns the
current revision. This hosted body reports `runtime_lane: hermes`. At boot it
discovers either MCP dialect (`agents.runtime_pack`/`agents_runtime_pack` and
`katailyst.well`/`katailyst_well`) from `tools/list`; the model-facing Hermes
names are separately prefixed `mcp__katailyst2__...`. Mounting the MCP is not
identity proof, so the runtime-pack call deliberately omits `agentRef`: only an
agent-bound token can return Cleo.

Company facts do **not** go in `MEMORY.md`: it is capped, frozen per session,
agent-mutable, and the background reviewer edits it.

> Historical trap: an earlier version seeded `$HERMES_HOME/memory/*.md`. Hermes
> reads `$HERMES_HOME/memories/MEMORY.md` — plural directory, fixed filenames —
> so nothing ever read those files.

Config is generated, never hand-written. `render_config.py` writes it at boot,
mounting only the MCP servers whose URL is set and writing tokens as `${VAR}`
references so the disk never holds a secret. Generated files carry
`_generated_by: hlt-render-boot`; drop that line to hand-edit and boot will
leave your file alone and say so in `/health`.

## Env

| Var | Purpose |
|-----|---------|
| SuperGrok / Premium+ OAuth | Primary Grok 4.6 credentials, stored by `hermes auth add xai-oauth` in the persistent Hermes auth store |
| ChatGPT subscription OAuth | First recovery route, stored by `hermes auth add openai-codex` in the persistent Hermes auth store |
| `OPENROUTER_API_KEY` | Independent Kimi/Qwen/DeepSeek recovery routes |
| `AGENT_ENABLE_GATEWAY` | `1` starts the Slack gateway; anything else = health only |
| `AGENT_ID` | `cleo` (default) or `brian` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `xoxb-…` / `xapp-…` |
| `SLACK_ADMIN_USERS` | CSV Slack user IDs allowed privileged slash commands |
| `SLACK_ALLOWED_CHANNELS` | CSV channel allowlist |
| `HERMES_INFERENCE_PROVIDER` | Provider override; default `xai-oauth` |
| `HERMES_MODEL` | Model override; default `grok-4.6` |
| `HERMES_FALLBACK_PROVIDERS` | Optional JSON route objects or CSV `provider:model` override; `[]` explicitly disables recovery |
| `FIRECRAWL_API_KEY` | Selects Firecrawl for Hermes web search; the image bakes the matching SDK extra |
| `WEB_SEARCH_BACKEND` | Explicit Hermes web backend override; keyless default is DDGS |
| `GPTR_MCP_URL` / `GPTR_MCP_TOKEN` | Hosted researcher MCP |
| `CODEGRAPH_MCP_URL` / `CODEGRAPH_MCP_TOKEN` | Estate code graph |
| `KATAILYST2_MCP_URL` / `KATAILYST2_MCP_TOKEN` | Registry |
| `LINEAR_MCP_URL` / `LINEAR_MCP_TOKEN` | Roadmap (optional) |
| `HLT_AGENT_REF` | Canonical unversioned identity; `agent:cleo` |
| `OPENCLAW_HQ_HOOK_TOKEN` | Shared strong bearer for K2's hosted-agent hook and Hermes' loopback run API |
| `K2_ACTIVATION_POLL_SECONDS` | Optional offline-to-active polling interval, bounded to 5–300 seconds (default 10) |
| `HERMES_HOME` | Persistent disk path (default `/data/hermes`) |

Render supplies `RENDER_GIT_COMMIT`; `/health.config.deploy_commit` exposes it
so a live agent can be tied to the exact merged build.

`/health.config.configured_model_route` exposes the intended ordered route:
Grok 4.6, Codex GPT-5.6 Sol, then the current Kimi K3, Qwen 3.8 Max, and
DeepSeek V4 Pro OpenRouter routes. `model_route_readiness` checks each route's
credential without exposing tokens. `gateway.observed_model_route` remains
empty until a successful model call, then names the provider/model that really
answered — including a fallback. Cleo caps one generated provider reply at
32,768 tokens, caps a run at 24 model iterations, and compacts the working
prompt at 80,000 tokens. These provider-independent limits keep a useful run
from consuming a whole subscription rate window without reducing the models'
readable context or Cleo's tool access. `/health.config.max_turns` and
`/health.config.compression_threshold_tokens` expose the active limits.

`/health.config.k2_agent_readiness` is deliberately stricter than
`mcp_mounted`. It verifies the `x-katailyst-repo: katailyst2` response header,
the runtime-pack and well tools, an agent-bound `agent:cleo` pack, the
`paperclip_hermes` compatibility decision, active/online state, the applied
pack digest, and an actual well call. A legacy v1 bridge, broad/misbound token,
inactive pack, or unavailable well is named separately.

K2 activation has two stages. Authenticated `GET /activationz` returns the
versioned `agent_host_activation_readiness.v1` contract and its exact 21
non-circular checks: hosted body, durable admission ledger, credentials,
dependencies, exact token-bound Cleo pack and host compatibility, without
requiring Cleo to already be online. K2 can then activate the agent; the bounded
watcher repeats `agents.runtime_pack` with `requireActive:true`, installs it,
proves the well and starts Hermes. `GET /readyz` is the stricter post-activation
receipt: canonical pack applied, one-turn context door callable, Slack socket
live, primary model ready, durable ledger ready and the Hermes run API reachable.

Authenticated `GET /slack-identityz` performs fresh Slack `auth.test` and
`bots.info` reads and returns `slack_agent_identity.v1`: the stable workspace,
app, bot and bot-user IDs plus scope checks. K2 uses this when the Slack token
already lives on the hosted body, so owner verification never requires copying
that token into a second vault. The endpoint sends no message and exposes no
credential.

## K2 hosted-agent contract

All routes below require `Authorization: Bearer $OPENCLAW_HQ_HOOK_TOKEN`.

- Before `POST /hooks/agent`, K2 persists the deterministic wrapper id
  `run_<katailyst_run_id UUID without hyphens>` and its same-origin status URL.
- `POST /hooks/agent` durably admits the exact K2 run/session before calling
  Hermes, then returns HTTP 202 with that wrapper `{runId, statusUrl}`. Exact
  retries return the same receipt and never create a second provider run.
- `GET /hooks/agent/runs/{runId}` reads the durable wrapper ledger and, once
  bound, the native Hermes run. Only `completed`, `failed` and `cancelled` are
  terminal; `waiting_for_approval` stays nonterminal. Terminal output and usage
  are bounded, secret-redacted, and persisted across wrapper restarts.
- The wrapper dispatches to Hermes' loopback-only `/v1/runs` surface. It does
  not synthesize a second agent loop, does not post into Slack, and retains
  Hermes' normal model, tools, session and `pre_llm_call` behavior.

The ledger moves monotonically through `queued -> dispatching ->
provider_bound -> terminal`. A restart before `dispatching` can safely resume;
a lost response after that boundary is surfaced as nonterminal `unknown` with
`provider_admission_ambiguous` and is never automatically redispatched. K2 must
poll `statusUrl` and terminalize its run from that receipt. Treating the initial
202 as completed is a control-plane bug, not a successful Cleo run.

Slack uses Hermes' single-message edit stream. The answer progressively updates
in place, the ephemeral Assistant status shows the current useful action, and
per-tool/interim commentary bubbles stay off. The model returns one final answer
instead of using a Slack send tool to duplicate its own response.

One-to-one DMs belong to the agent the teammate opened. Channels and group DMs
require a fresh native Slack mention; the first recognized, nonquoted mention
among Victoria, Lila, Julius, and Cleo is the only agent allowed to dispatch.
Later mentions stay silent, edits never reopen the choice, and a bot-to-bot
handoff works only when a recognized peer explicitly mentions its target. A
hash-pinned local roster is the bounded v1 fallback until K2 publishes a typed
roster projection. Every decision is written privately to
`$HERMES_HOME/slack-agent-lead.sqlite3` as
`slack_agent_lead_decision.v1`; retries and restarts hit the durable tombstone
before typing or model work, and no message text is recorded.
Native slash and message-form control commands keep their local Hermes session
semantics; they already address one installed app and do not enter arbitration.
Nonparticipant personas in this image, such as Brian, remain outside this
four-agent Slack election.

`/health.config.hermes_upstream_ref` exposes the immutable Hermes runtime SHA
baked into the image. The image build also asserts that `codegraph.context`
registers as `mcp__codegraph__context`, pinning the exact invalid-tool failure
that previously escaped as a raw Slack error.

A URL without its token still mounts, unauthenticated — `/health` lists those
under `config.mcp_without_token` so a half-set pair is visible rather than
silently degraded. All three of codegraph, gpt-researcher and katailyst2 now
reject unauthenticated calls, so a missing token there means a dead tool.

## Turning an agent on (one-time, ~5 minutes)

Steps 1-3 need Alec's login. **Cleo's app already exists** — for her, skip to
"Fixing Cleo's scopes" below.

1. https://api.slack.com/apps → **Create New App** → **From an app manifest** →
   HLT workspace → paste [`slack-app-manifest.yaml`](./slack-app-manifest.yaml)
   → **Create**
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**, scope
   `connections:write` → copy the `xapp-…` token
3. **Install App → Install to Workspace** → copy the **Bot User OAuth Token**
   (`xoxb-…`)
4. Render → `hlt-hermes` → **Environment**:
   - `SLACK_BOT_TOKEN` = the `xoxb-…`
   - `SLACK_APP_TOKEN` = the `xapp-…`
   - `SLACK_ADMIN_USERS` = your Slack user ID
   - `AGENT_ENABLE_GATEWAY` = `1`
5. Save. Render redeploys automatically.

### Connect the model subscription

Cleo's primary model uses the owner's SuperGrok / Premium+ entitlement through
Hermes' xAI device-code OAuth provider. Her first fallback uses ChatGPT
subscription OAuth. From a Render shell run both:

```bash
hermes auth add xai-oauth
hermes auth add openai-codex
```

Open each displayed URL, approve its code, then verify both subscription routes
under `/health.config.model_route_readiness`. Refresh tokens stay in the
service's persistent `HERMES_HOME`; they are never Render environment variables
or Slack messages. A successful login is not the final proof: run a real Slack
task and read `gateway.observed_model_route`, because a provider can still deny
inference after issuing OAuth tokens.

## Smoke

```bash
curl -s https://hlt-hermes.onrender.com/health | jq
```

Read the answer, not the status code — the service intentionally stays up (HTTP
200) even when the agent is down, so you can see *why*:

| `mode` | Meaning |
|--------|---------|
| `readiness_gateway` | Gateway not requested. Expected before step 4. |
| `gateway` | The agent is running. `gateway.uptime_seconds` climbs. |
| `gateway_down` | Requested but not running. `status: degraded`, and `gateway.stopped_reason` / `last_exit_code` say what happened. |
| `gateway_k2_brain_unavailable` | The active, bearer-bound Cleo runtime pack was not applied. |
| `gateway_k2_wrong_server` | The endpoint answered but did not identify itself as Katailyst2 (usually the legacy v1 bridge). |
| `gateway_k2_outage_fallback` | K2 declared a service/transport outage, so the reviewed bundled snapshot is running visibly degraded. |
| `gateway_k2_context_unavailable` | The canonical brain is active but the mission-time wishing well is unavailable. |
| `gateway_k2_context_plugin_missing` | The pack is active but Hermes did not load the one-turn K2 hook. |
| `gateway_external_dispatch_unavailable` | Slack can run, but the K2 hook bearer/run bridge is not configured. |
| `gateway_web_search_degraded` | The selected web backend is missing its credential or installed SDK. |
| `gateway_model_fallback_degraded` | Primary can answer, but at least one configured recovery route is positively unavailable. |

Check `config.mcp_mounted` lists the configured servers,
`config.mcp_without_token` is empty, `config.slack_auth.auth_ok` is true, and
`config.slack_auth.missing_core_scopes` is empty. Then DM the bot a real task
and confirm it delivers the result or artifact rather than just describing its
tools.

**Adversarial check, worth doing once:** from a non-admin account, confirm
`/model` is refused, and ask the agent to run a shell command — it should say it
can't, because the tool isn't loaded.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `mode: gateway_down`, `cli_present: false` | Image built without the CLI. The build fails loudly on this now, so it means an old image is live — redeploy. |
| `mode: gateway_down`, repeated `last_exit_code` | Crash loop; the supervisor stops after 5 attempts and leaves the reason in `stopped_reason`. Check Render logs. |
| `gateway_slack_auth_failed` | Slack rejected the installed bot token. Reinstall/update the bot token. |
| `gateway_slack_scopes_missing` | The token works, but live OAuth grants are missing a core channel/DM/file scope named in `/health`. |
| Answers but knows nothing about the estate | Check `config.mcp_mounted` and `config.mcp_without_token`. |
| Anyone can run `/model` | `SLACK_ADMIN_USERS` is unset. |
| `subscription_auth.logged_in: false` with provider `xai-oauth` | The SuperGrok OAuth grant is absent or expired; run `hermes auth add xai-oauth`. |
| Codex fallback unavailable | Run `hermes auth add openai-codex`, then prove it with a bounded canary rather than token presence alone. |
| `openrouter_key_present: false` | Grok may still answer, but the independent OpenRouter recovery routes are unavailable and health is degraded. |

## Retired recurring briefs

Boot no longer creates the three legacy jobs `nm-monday-brief`,
`nm-board-health`, and `nm-product-owner-work`. Before the gateway starts, it
reads the durable Hermes cron store, writes the exact matching records once to
`$HERMES_HOME/cron/retired/nm-legacy-briefs-before-retirement.json`, and pauses
each job by ID. It never deletes jobs or run history. If the source record or
recovery export is unreadable, it pauses nothing. To restore one intentionally:

```bash
hermes cron resume <job-id>
```

## Repairing Cleo's scopes when `/health` names a gap

Do not infer installed scopes from an old template or this README. Read
`config.slack_auth` first. If `scopes_known` is true and
`missing_core_scopes` is non-empty, add the named scopes to the Cleo app.

https://api.slack.com/apps → **Cleo** → **OAuth & Permissions** → add bot scopes:

```
app_mentions:read  channels:read  commands  files:read  files:write
groups:history  groups:read  im:history  im:read  im:write
mpim:history  mpim:read  users:read  reactions:read  reactions:write
assistant:write
```

Then **Reinstall to Workspace**, and confirm **Event Subscriptions** has
`app_mention`, `message.im`, `message.channels`, `message.groups`,
`message.mpim`. The bot token changes on reinstall — update `SLACK_BOT_TOKEN`.
