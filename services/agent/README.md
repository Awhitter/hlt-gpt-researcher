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
   and proves the independent async mission-time Well door,
4. resolves the exact human-invited HLT agent participant set before Hermes can
   type or call a model,
5. supervises `hermes gateway` as a child when `AGENT_ENABLE_GATEWAY=1`,
6. serves operator health plus authenticated K2 activation/dispatch routes on
   `$PORT`.

The supervisor exists for a specific reason: Hermes reaches Slack over **Socket
Mode**, an outbound WebSocket. It never binds a port. Render runs this as a *web
service* with a health check, so if `hermes gateway` were the main process
nothing would answer on `$PORT` and every deploy would roll back.

## Capability and effect boundary

Slack agents receive the practical workbench required to finish real missions:
web and vision, files and code, browser/computer work, schedules, delegation,
media, and progressively discovered MCP/K2 tools. Access to a capable tool is
not itself a reason to stop. Reads, research, reasoning, drafts, staging, and
reversible internal configuration proceed automatically.

The `hlt-k2-context` pre-tool hook applies one shared effect contract. It asks
at external send/publish, paid generation, deletion, credential/access changes,
and protected production changes. AgentMail identity is resolved per agent;
reads and draft work remain automatic while send/reply/forward/schedule cross
the approval boundary. Browser/computer observation stays automatic and
effectful input is classified at execution time.

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
| `hlt-k2-context` Hermes plugin | resolve the exact human-invited Slack participant set, then start one durable `katailyst.well.start` draw for each substantive turn and hand its exact `get` handle to the model without waiting | participation decisions are private local receipts; compact registry search is the fallback for an incomplete Well tool surface; injected mission context is ephemeral and never persisted into transcript or memory |
| `$HERMES_HOME/memories/MEMORY.md` | genuinely learned deltas | agent-written, approval-gated, ~2200 chars |

Cleo's durable registry identity is the unversioned `agent:cleo`; K2 owns the
current revision. This hosted body reports `runtime_lane: hermes`. At boot it
discovers either MCP dialect (`agents.runtime_pack`/`agents_runtime_pack`,
`katailyst.well.start`/`katailyst_well_start`, and their `get`/compatibility
forms) from `tools/list`; the model-facing Hermes
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
| ChatGPT subscription OAuth | Primary GPT-5.6 Sol credentials. Hermes serves from any selectable `openai-codex` profile and reports the three-profile redundancy target separately before falling back. |
| SuperGrok / Premium+ OAuth | The one independent Grok 4.6 recovery route, stored by `hermes auth add xai-oauth` in the persistent Hermes auth store |
| `AGENT_ENABLE_GATEWAY` | `1` starts the Slack gateway; anything else = health only |
| `AGENT_ID` | `cleo` (default) or `brian` |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `xoxb-…` / `xapp-…` |
| `SLACK_ADMIN_USERS` | CSV Slack user IDs allowed privileged slash commands |
| `SLACK_ALLOWED_CHANNELS` | CSV channel allowlist |
| `FIRECRAWL_API_KEY` | Selects Firecrawl for Hermes web search; the image bakes the matching SDK extra |
| `WEB_SEARCH_BACKEND` | Explicit Hermes web backend override; keyless default is DDGS |
| `GPTR_MCP_URL` / `GPTR_MCP_TOKEN` | Hosted researcher MCP |
| `CODEGRAPH_MCP_URL` / `CODEGRAPH_MCP_TOKEN` | Estate code graph |
| `KATAILYST2_MCP_URL` / `KATAILYST2_MCP_TOKEN` | Registry |
| `LINEAR_MCP_URL` / `LINEAR_MCP_TOKEN` | Roadmap (optional) |
| `HLT_AGENT_REF` | Canonical unversioned identity; `agent:cleo` |
| `HLT_AGENT_PUBLIC_ORIGIN` | Exact HTTPS origin used by K2's one-click computer handoff |
| `OPENCLAW_HQ_HOOK_TOKEN` | Shared strong bearer for K2's hosted-agent hook and Hermes' loopback run API |
| `K2_ACTIVATION_POLL_SECONDS` | Optional offline-to-active polling interval, bounded to 5–300 seconds (default 10) |
| `HERMES_HOME` | Persistent disk path (default `/data/hermes`) |

Render supplies `RENDER_GIT_COMMIT`; `/health.config.deploy_commit` exposes it
so a live agent can be tied to the exact merged build.

`/health.config.configured_model_route` exposes the reviewed ordered route:
GPT-5.6 Sol at high reasoning through the managed Codex profile pool, then
Grok 4.6 through xAI OAuth. There is no weak third agentic route: if both are
unavailable, Hermes preserves the request and Slack receives one concise
degraded-service answer. `model_route_readiness` checks each route's credential
without exposing tokens; the Codex detail includes only profile count and
selectability, never labels or tokens. Fewer than three selectable Codex
profiles keeps `/activationz` and `/readyz` red while `/health` remains a 200
liveness receipt with the exact safe counts. It no longer blocks the Slack
gateway when at least one reviewed Sol or Grok route can answer; the agent
serves visibly degraded and keeps recovering its preferred route instead of
going silent. `gateway.observed_model_route` remains
empty until a successful model call, then names the provider/model that really
answered — including Grok failover. Cleo caps one generated provider reply at
32,768 tokens and keeps 24 model iterations available for long-running API/K2
work. Interactive Slack turns have a stricter seven-iteration ceiling plus five
tool-calling rounds; after that, the plugin blocks further tools and
tells the model to synthesize from completed evidence with missing values
labeled unknown. This preserves parallel reads and the full catalog while
preventing a routine funnel question from becoming an open-ended research run.
The working prompt still compacts at 80,000 tokens.
`/health.config.max_turns`, `/health.config.slack_max_turns`,
`/health.config.slack_tool_round_limit`, and
`/health.config.compression_threshold_tokens` expose the active limits.

For Slack tables, the prompt contract requires plain Markdown pipe syntax with
a header and separator row. Hermes can then render the table as native Slack
blocks; fenced monospace tables are explicitly disallowed, with compact bullets
as the fallback for data that is too wide.

`/health.config.k2_agent_readiness` is deliberately stricter than
`mcp_mounted`. It verifies the `x-katailyst-repo: katailyst2` response header,
the runtime-pack and async Well start/get tools, an agent-bound `agent:cleo` pack, the
`paperclip_hermes` compatibility decision, active/online state, the applied
pack digest, resolved shared-doctrine refs/body size, and an actual durable Well
start plus immediate get. A legacy v1 bridge, broad/misbound token,
inactive pack, or unavailable well is named separately. Once the canonical pack
is verified, `contract_status` remains `pack_loaded` if only the optional Well
probe fails; `well_mode`, `well_status`, and
`well_outage_declared` carry that narrower truth.

K2 activation has two stages. Authenticated `GET /activationz` returns the
versioned `agent_host_activation_readiness.v2` contract and its exact 25
non-circular checks: hosted body, durable admission ledger, credentials,
dependencies, reviewed model-route contract, exact token-bound Cleo pack, and
host compatibility, without requiring Cleo to already be active. When K2
returns `activation.status=offline`, `isOnline=true`, and a reviewed,
curated/published, agent-bound, host-compatible, revision-valid pack, Hermes may
install that exact preactivation brain so Slack can produce the missing proof.
Health stays degraded, `/readyz` and the external run hook stay closed, and the
bounded watcher continues polling. K2 can then activate the agent; the watcher
repeats `agents.runtime_pack` with `requireActive:true`, installs it, proves the
async Well door, and starts Hermes. If the canonical active pack succeeds but
the independent Well probe times out, Hermes keeps that K2 brain and starts
with a visible optional-enrichment advisory, and retries K2 on ordinary Slack
turns instead of replacing the current doctrine with a bundled fallback. That
optional timeout does not fail whole-agent health or the hosted run door because
K2 handoffs already carry mission context and can read exact refs directly.
During a declared K2 transport outage, Cleo may start from the reviewed bundled
fallback, but the same bounded watcher stays alive. When K2 recovers it installs
the canonical pack in place, clears the stale outage receipt, and Hermes detects
the managed pack epoch on the next message so existing Slack conversations adopt
the new SOUL and doctrine without losing their history or restarting the socket.
Both activation stages carry `agent_host_runtime_inputs.v1`: a stable digest of
the model ladder, reasoning, pinned Hermes runtime, active K2 pack, toolsets, and
MCP mounts. Presentation-only fields such as portrait, card copy, display name,
owner tuning, and deploy SHA are intentionally excluded, so visual edits cannot
deactivate a working body.
`GET /readyz` is the stricter post-activation receipt: canonical pack applied,
Slack socket live, primary model ready, durable ledger ready and the Hermes run
API reachable; `optionalChecks.k2_well_enrichment_callable` preserves the
independent Well readback without misclassifying the working agent as offline.

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
  Hermes' normal model, tools and session behavior.
- `timeoutSeconds` is the hard end-to-end provider budget. The per-run system
  instructions cap retrieval at 25%, reserve time for the requested final, use
  K2-selected context refs directly, and allow at most one focused recovery
  search. Because K2's handoff already carries the canonical mission reading
  and selected refs, `hook:k2:*` API runs skip the plugin's second automatic
  Wishing Well draw; ordinary Slack turns start one idempotent async draw and
  give the model its exact run handle and server-requested poll interval instead
  of blocking the model loop. Compact registry search remains the fallback when
  the durable Well tool pair is not available.
- Before that surface may emit `run.completed`, the HLT overlay reconciles each
  factual business count, rate, dollar value, and percentage against the
  current request and successful current-run tool results. Explicitly labelled
  future targets and arithmetic with grounded operands remain valid;
  unsupported facts end as `run.failed` with no answer output. The per-run
  agent then closes its owned SQLite session so `ended_at` and `end_reason`
  cannot remain null after terminal work.
- This deterministic gate belongs specifically to the hosted `/v1/runs`
  boundary used by K2. Direct Slack gateway delivery does not traverse this
  route and must not be described as covered by this gate.

The ledger moves monotonically through `queued -> dispatching ->
provider_bound -> terminal`. A restart before `dispatching` can safely resume;
a lost response after that boundary is surfaced as nonterminal `unknown` with
`provider_admission_ambiguous` and is never automatically redispatched. K2 must
poll `statusUrl` and terminalize its run from that receipt. Treating the initial
202 as completed is a control-plane bug, not a successful Cleo run.

Slack uses one native `chat.startStream` response for the whole turn. The host
seeds it immediately with a short acknowledgement; polished model commentary
then extends that same append-only stream with human-readable progress, and the
turn-final answer seals it exactly once. Raw tool progress, commands, paths,
provider warnings, logs, and private reasoning remain off. The model does not
use a Slack send tool to duplicate its own response.

## Open Cleo's computer

K2's Cleo profile is the front door to the full native Hermes dashboard. Choose
**Open computer** there; K2 authenticates the teammate, redeems a one-use handoff
server-to-server, and opens `/computer/chat` with an HttpOnly browser session.
There is no gateway token to copy or share. Direct visits to the Render URL
return to Cleo's K2 profile so the next launch is always fresh.

The browser workbench is deliberately the real pinned Hermes UI, not a reduced
admin page: Chat/TUI, sessions, models, MCP, skills, browser work, usage, and
approvals stay available. Its web and TUI bundles are built from the exact
`HERMES_REF`, and the container build fails if either native surface is absent.
The outer K2 session gate covers both HTTP and WebSocket traffic; a deploy simply
expires the local session and the profile button opens another one.

`HLT_AGENT_PUBLIC_ORIGIN` is the exact public HTTPS origin used by the handoff
(production: `https://hlt-hermes.onrender.com`). It must match Cleo's
`computerTargetOrigin` in K2. `OPENCLAW_HQ_HOOK_TOKEN` remains server-only and
is shared with K2's logical `hlt_hermes_hook_token` binding; neither value enters
the browser URL or response body.

The checked-in manifest uses Slack's current Agent View and Agent Sessions.
Pinned Hermes handles `app_home_opened`, `app_context_changed`, active-view
context, threadless suggested prompts, and `agent_session_stopped`. Stop first
confirms the exact model/tool task has ended, then suppresses every late frame
and returns the native session to active without another message. Native task
cards remain off by default because
ordinary work belongs in the one response stream; they are reserved for
genuinely structured multi-step work after a focused lifecycle proof. The Home
tab also remains disabled because this runtime does not publish a Home view.
File delivery and rich Block Kit answers are supported; Canvas creation is not.

The canonical K2 runtime pack is installed at boot. Turns use `registry.get`
at card or concise depth first and load a full body only when needed; fetching
the runtime pack again or opening several full registry bodies just burns the
working context without adding authority.

Hermes' own progressive bridge is the first discovery door: search for a
direct `mcp__katailyst2__<verb>`, describe one candidate, then call it. Host
search returns three candidates by default (eight maximum), and a multi-name
describe reveals three tools at a time while retaining each complete input
schema. K2's compatibility `tool_describe` starts at `detailLevel: summary` and
expands only the exact action being invoked. The PostHog `exec` bridge follows
its live CLI contract: `search`, one `info`, optional field-level `schema`, then
`call --json`.

An entity, registered tool, or authoritative source already named in a request
is a resolved route, not a cue for another discovery sweep. Cleo describes and
executes that exact K2 route before polling the pending Well, searching the
catalog or registry, or loading adjacent skills; one focused recovery search is
available if the direct route fails. Standard Nursing Mastery funnel pulses use
the HLT-org `tool:nm-analytics-readout` for the current 7-day and 28-day
decision readout. Cleo skips discovery for this known route and executes both
windows in one parallel round with the bounded key set
`humans,walk_started,email_given,applications`; a requested unreadable window
gets one same-key retry. The response keeps the requested unfenced 7d/28d Slack
table, measured zeroes, and explicit unreadable states. Raw PostHog remains
available for deeper custom behavior analysis, but do not reconstruct the
standard readout from it when the governed tool reports a gap.

An MCP result over 16,000 characters is kept in full under Hermes' durable
spillover store and replaced in the prompt by a short preview. Slack can page
that exact saved result with a session-bound `read_spillover` byte cursor; the
cursor prevents a giant tool payload from crowding out the original request,
and the external API hook never receives the reader.
`/health.config.mcp_result_size_chars` and `/health.config.tool_search` expose
the deployed limits. Automatic Hermes background review is disabled for this
managed agent: K2 owns durable learning, and replaying a finished Slack turn
through more model calls cannot update the user-owned skill anyway.

One-to-one DMs belong to the agent the teammate opened. Channels and group DMs
require a fresh native Slack mention; every explicitly named Victoria, Lila,
Julius, or Cleo is invited to answer, while unmentioned agents stay silent. The
human-invited participant set remains admitted for ordinary unmentioned human
follow-ups in that thread. A later explicit human mention replaces and may
narrow the set. Bot-authored replies never inherit, replace, or expand human
participation—even if they mention another agent. Edits never reopen the choice. A
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

## Applying Cleo's reviewed Agent View manifest

[`slack-app-manifest.yaml`](./slack-app-manifest.yaml) is the complete remote
manifest, not a patch. Committing it does not change the installed Slack app;
the steps below are the separate live migration. Updating to Agent View is
one-way in Slack.

Before applying it, run
`uv run python services/agent/validate_slack_manifest.py`. This read-only local gate
checks the schema version and the source/runtime seam: Cleo's identity, Agent
View starters, writable Messages tab, supported events, OAuth scopes, and real
Hermes commands. Slack's `apps.manifest.validate` remains the generic remote
schema check and needs a short-lived app-configuration token; a bot token
cannot substitute for it.

1. Read authenticated `/slack-identityz` and `/health` first. Confirm the
   workspace, app, bot, `agent_ref`, deployed commit, and current OAuth grants.
2. In https://api.slack.com/apps open **Cleo → App Manifest → Edit**, replace
   the remote manifest with this file, review the Agent View warning, and save.
   The API equivalent is `apps.manifest.update`; it requires Cleo's app ID and
   an app-configuration token with `app_configurations:write` and replaces the
   whole manifest.
3. **OAuth & Permissions → Reinstall to Workspace** so the installed app gains
   any changed scopes/events. If Slack reissues the `xoxb-…` token, update only
   `SLACK_BOT_TOKEN` on the Render `hlt-hermes` service without logging it.
4. Deploy or restart from the exact merged commit. Re-read `/health` and
   `/slack-identityz`; require Slack auth, no missing core scopes,
   `assistant:write` among the granted scopes, the expected identity, and a
   connected gateway.
5. Use a private DM canary: open Cleo, confirm the Agent View description and
   starters, switch views once to exercise `app_context_changed`, and send one
   real read-only task. Expect an immediate acknowledgement, meaningful updates
   inside one evolving native stream, and the polished final sealing that same
   stream. Press Stop during a second task and require confirmed cancellation
   with no later output.

Official references: [Agent View and Agent Sessions](https://docs.slack.dev/changelog/2026/08/20/agent-updates/),
[app manifest fields](https://docs.slack.dev/reference/app-manifest/), and
[`apps.manifest.update`](https://docs.slack.dev/reference/methods/apps.manifest.update/).

## Turning an agent on (one-time, ~5 minutes)

Steps 1-3 need Alec's login. **Cleo's app already exists** — use the reviewed
Agent View migration above for her. This section is for a new agent app.

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

Cleo's primary model uses the managed ChatGPT subscription pool. Add or repair
at least three profiles with Hermes' Codex OAuth flow; profile labels and token
material must never be copied into health output:

```bash
hermes auth add openai-codex
```

The fallback uses the owner's independently authenticated SuperGrok / Premium+
entitlement. Connect it with `hermes auth add xai-oauth`, then verify both route
entries under `/health.config.model_route_readiness`. Refresh tokens stay in
the service's persistent `HERMES_HOME`; they are never Render environment
variables or Slack messages. A login flag alone is not proof that inference
works: require selectability plus a bounded private tool-call canary on each
route before rollout.

## Smoke

```bash
curl -s https://hlt-hermes.onrender.com/health | jq
```

Read the answer, not the status code — the service intentionally stays up (HTTP
200) even when the agent is down, so you can see *why*. `liveness.ok` proves the
HTTP supervisor answered; `readiness.ready` independently requires the exact
model ladder, selectable profiles, active K2 runtime, and Slack transport:

### Fleet monitor and real canary

Boot installs three idempotent native Hermes jobs through `cron.jobs`, without
writing the cron store by hand or re-enabling the retired product briefs:

| Job | Schedule | Work |
| --- | --- | --- |
| `hlt-fleet-readiness-cleo-v1` | Every five minutes | Read local `/health`; no model |
| `hlt-fleet-release-cleo-v1` | Daily 15:20 UTC (default container timezone) | Read Hermes stable release and exact commit metadata; no model/build/update |
| `hlt-fleet-daily-canary-cleo-v1` | Daily 14:45 UTC (default container timezone) | One read-only K2 identity task, authenticated Grok 4.6/high |

**The daily canary verifies K2 plus Cleo's authenticated backup, not her primary.**
Codex's subscription wire rejects output caps, so only this explicitly budgeted
job uses Grok. Ordinary chat remains Sol/high with the existing managed Codex
profiles and Grok recovery route. Real Slack acceptance must independently
verify Sol. The installer receipt at `/health.config.fleet_checks.canaryRoute`
records this distinction; scheduled success is not evidence of primary health.

All three deliver only to `slack:C0BH5997USK` (`#agent-logs`). Health and release
scripts emit only changed findings or recovery, retrying failed native delivery.
A red observation exits successfully so native cron keeps its five-minute
cadence. Receipts live under `$HERMES_HOME/fleet/`; native cron retains job
execution, model usage, and delivery history. Installation status is visible at
`/health.config.fleet_checks`. A paused managed job stays paused on deployment.

Treat `liveness.ok: true` as HTTP/process availability and `readiness.ready: true`
as full readiness. `readiness.servingReady` requires Slack, K2, and at least one
reviewed working route; `redundancyReady` describes the backup and profile pool
separately. The canary can run when serving works even if a backup is degraded.
The readiness receipt names each non-model check under
`readiness.checks` and carries the runtime-only activation digest under
`readiness.runtimeProof`. `primary_model_profile_ready` means at least one real
Codex credential can serve; `primary_model_pool_redundancy_ready` stays false
until at least three managed Codex profiles are present *and selectable*. A
stale login flag cannot make either green. `k2_activation_ready` also remains
false while the reviewed preactivation brain is serving Slack, so liveness and
useful degraded service never masquerade as completed K2 activation.

The daily canary is budgeted in code: at most four provider attempts, 1,200
cumulative output tokens, 64,000 aggregate serialized input bytes, and a native
120-second run budget. The small `scheduled_run_budget.patch` passes opt-in job
limits into Hermes and clamps each provider request, including retry boosts.
Unknown usage retains its reservation instead of authorizing another attempt.
Budgeted jobs disable the transport's internal stream retry and model fallback;
budget exhaustion is recorded as failure, never an empty successful reply.
The existing mission-context hook uses the canary's supplied record instead of
starting an additional paid wishing-well draw outside that budget.
These limits affect only jobs carrying `hlt_run_budget`, not normal Slack/API
work. No custom model runner, copied credentials, or GitHub Actions is involved.
Keep a separate ordinary Slack acceptance receipt for acknowledgement latency,
streaming, thread continuation, and Stop: a scheduled K2 read does not prove
those interactive behaviors. Version detection never triggers an upgrade.

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
| `gateway_model_route_contract_degraded` | The process is alive but its configured route is not exactly Sol high -> Grok 4.6. |

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
| Codex primary unavailable | Run `hermes auth add openai-codex` until health reports at least three present and selectable profiles, then prove one bounded private tool-call canary. |
| Grok fallback unavailable | Run `hermes auth add xai-oauth`, then prove a bounded private tool-call canary before treating the recovery route as ready. |

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
app_mentions:read  channels:history  channels:read  chat:write  commands
files:read  files:write  groups:history  groups:read  im:history  im:read  im:write
mpim:history  mpim:read  users:read  reactions:read  reactions:write
assistant:write
```

Then **Reinstall to Workspace**, and confirm **Event Subscriptions** has
`app_mention`, `app_context_changed`, `app_home_opened`, `message.im`,
`message.channels`, `message.groups`, `message.mpim`. If the bot token changes
on reinstall, update `SLACK_BOT_TOKEN`.
