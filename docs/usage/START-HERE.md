# Mastery Research — start here

This fork hosts **Mastery Research** (also called Mastery Brain): HLT's
reusable team research core over web + our estate. It serves multiple products
and agents; it is not the Nursing Mastery tool. The upstream README below this
folder is Assaf Elovic's open-source GPT Researcher — keep that intact for
upstream sync. Everything HLT-specific lives in the surfaces and modules
below.

## What it is (one sentence)

Ask a question in plain English; Auto decides whether the answer needs our
code/registry/metrics/media or just the public web, then researches and
cites sources.

## Doors (pick one)

| Who | Door | URL / mount | Status |
| --- | --- | --- | --- |
| Humans (Kim, Bruce, marketing) | Browser UI | https://gpt-researcher-ui.vercel.app | live |
| Agents (Claude Code, Cursor, Katailyst2) | MCP | `https://gpt-researcher-mcp-production.up.railway.app/mcp` · Bearer `$GPTR_MCP_TOKEN` | live |
| Make.com, n8n, simple automations | Nonblocking strict HTTP jobs | `POST …/automation/research/jobs/v1/start` → `GET …/{request_id}/status` → `GET …/{request_id}/result` · Bearer `$GPTR_MCP_TOKEN` | deploy with the MCP service |
| Existing blocking callers | Strict HTTP compatibility route | `POST https://gpt-researcher-mcp-production.up.railway.app/automation/research/v1` · Bearer `$GPTR_MCP_TOKEN` | preserved |
| Scripts / Sidecar | REST API | `https://gpt-researcher-api-production.up.railway.app` · `X-API-Key: $API_AUTH_KEY` | live |
| Humans, in Slack | **Cleo**, Nursing Mastery product-owner facilitator | `hlt-hermes` on Render → https://hlt-hermes.onrender.com/health | trust the current `/health` seam readback |

The research core is the provider layer. Katailyst2 is the canonical
registry/orchestration layer for product-specific facilitator agents. The
shared runtime adapter in [`services/agent`](../../services/agent/README.md)
can boot Cleo, the Nursing Mastery product-owner/facilitator, or Brian, the
general Mastery Researcher. Cleo using this provider is one use case; it does
not change the core's scope.

("Hermes" is only the upstream runtime the selected agent runs on. The Render hostname still
says `hlt-hermes` because Render cannot rename a service in place.)

- UI + MCP default to **Auto scope** (estate context when the question needs
  it; pure web otherwise).
- REST `POST /api/quick_search` is deliberately **web-only** — use `/report/`
  or MCP when a script needs estate awareness.
- Automation jobs use the same strict-source facade over the MCP engine. Start
  persists one exact idempotent operation and returns immediately; status and
  result are pure reads. The older `/automation/research/v1` call remains
  blocking for compatibility. Neither path publishes, messages, or writes
  Airtable.

### Nonblocking automation contract

Send the existing `research_automation_request.v1` body to:

```text
POST /automation/research/jobs/v1/start
GET  /automation/research/jobs/v1/{request_id}/status
GET  /automation/research/jobs/v1/{request_id}/result
```

The three provider operations are `mastery_research_start` (idempotent
effect), `mastery_research_status` (read), and `mastery_research_result`
(read). Repeating start with the same `request_id` and canonical payload never
creates another operation; changing the payload under that ID returns 409.
Result returns the accepted report, source evidence, independent quality
receipt, cost, provenance, and a provider-neutral K2
`knowledge.refine.preview` handoff. The handoff is `ready` only when the full
accepted report fits both the 128,000 Unicode-codepoint and 128,000 UTF-8-byte
inline limits. A larger report is `withheld` with the typed reason
`k2_inline_content_limit_exceeded`, durable research locator, report path, and
source snapshot identity so a caller can fetch or chunk it without pretending
that truncated content is complete. The handoff records this run with
`upstream_execution_receipt.v1`; it does not call K2 on its own.

The durable operation survives process restarts even though an in-memory worker
cannot. Work is admitted to a configurable shared pool (8 running and 256
durably queued by default); queued starts return 202 and `Retry-After`, while a
full durable queue returns the typed 429
`automation_admission_saturated`. Exact idempotent replays bypass admission and
return the existing receipt. A background drainer starts with the service,
promotes queued work in FIFO order, and can reclaim abandoned work after the
stale-lease boundary. Polling status/result remains a pure read and never
performs that mutation.

Each attempt persists its lease-scoped core run identity, generation, deadline,
and phase budgets. Defaults are two hours overall, 90 minutes for deep research,
and 30 minutes for report generation; operators can tune them with the
`AUTOMATION_RESEARCH_*` variables documented in the hosted MCP guide. Terminal
timeouts are typed, and a canceled or stale attempt cannot overwrite the newer
winner.

Health: `curl -fsS …/health` on the API and MCP hosts above.

## Signing in (the UI has a team password)

The browser UI sits behind a shared-password gate: opening any page bounces
you to `/login`. Enter the team password once — the session cookie lasts
30 days per browser. **Sign out** clears it immediately. Production fails
closed if either auth variable is absent; secret-free bypass exists only in
local development.

- **Where the password lives:** Vercel project `gpt-researcher-ui` →
  `TEAM_ACCESS_PASSWORD` (Production), mirrored in Doppler
  `hlt-agent-tokens/dev` as `GPTR_TEAM_ACCESS_PASSWORD`. Ask Alec if you
  don't have it. Vercel env values pull back redacted on this team, so the
  Doppler mirror is the recoverable copy.
- **Rotating it:** update `TEAM_ACCESS_PASSWORD` and a newly generated
  `TEAM_ACCESS_COOKIE_SECRET` together, then redeploy and update the Doppler
  mirror. Rotating the cookie secret intentionally logs out every old session.
- Agents and scripts are unaffected — MCP and REST auth are separate keys.

## Deploying

| Surface | Command | Notes |
| --- | --- | --- |
| Backend API (Railway) | `./deploy-to-railway.sh` | reads local `.env`; healthcheck `GET /health` |
| MCP server (Railway) | `./deploy-to-railway-mcp.sh` | same pattern; redeploy BOTH when `backend/server/*` changes — the MCP routes through the same request prep |
| Team UI (Vercel) | `cd frontend/nextjs && vercel --prod --scope alecs-projects-e88e78a8` | project `gpt-researcher-ui`; the CLI's default scope is the HLT team, so pass the scope explicitly |

## Is it working? (60 seconds)

```
TEAM_ACCESS_PASSWORD=<the password> .venv/bin/python scripts/smoke_estate_eval.py --depth fast
```

The suite includes grounded product questions that require internal routing
and immutable commit-specific GitHub sources; exit code = number of failures.
Two web/model cases remain known-flaky at `fast` depth (`katailyst-registry-scope`
occasionally finishes without report content; `scopeless-pizza` occasionally
name-drops HLT) — a single flaky miss on those two is noise, repeated misses
or any failure on the two `nursing-mastery-*` glossary cases is real.

For Code-scoped questions, “done” means more than model output: source
retrieval runs comprehensively even when the human selects Fast, every cited
repository path is validated at a full 40-character commit, and that check
happens before the live answer or its Markdown/PDF/DocX copies are created. If
the check cannot prove the sources, the UI shows a plain-language source-check
notice and the unsupported draft is discarded. Public-web source cards are
also hidden from Code reports so generic search results cannot masquerade as
implementation evidence.

## How to use it in 30 seconds

**Human:** open the UI → leave **Auto** on → ask. The compact scope toggles
remain visible; pin Code / Registry / Metrics only when you want to force a
lane. History and secondary research/admin surfaces sit behind their own
links rather than competing with the main prompt.

**Agent:** call `deep_research` (or `quick_search`) with the default
`scope="auto"`. Pin `["codebase","cms"]` or pass `"none"` to override.
Estate questions on `quick_search` escalate to a short scoped research pass
(`mode: "scoped_research"`) — expect seconds, not milliseconds. Pass
`scope="none"` for a guaranteed cheap web scan. Fast, Balanced, and Deep use
progressively wider 5/8/12-result pools per query. Research returns attributed
source images when pages expose them; agents can set
`include_generated_images=true` for optional non-likeness illustrations and
retrieve either kind with `get_research_images`.

**Canonical estate repos Auto knows about:** Mastery Research itself,
nursing-mastery, ScraperVault, HLT Web Service, katailyst2, MMM2, and EBB (override with
`HLT_CODEBASE_REPOS`). Mastery Research and HLT Web Service support exact
current source reads without memory-heavy structural indexes.

## Where the HLT code lives

```
backend/server/
  hlt_extensions.py      ← router: auth, readiness, MCP presets, prepare_research_request
  hlt_scope_inference.py ← Auto: heuristics → optional FAST_LLM tiebreak
  hlt_brain.py           ← /api/brain/* (repos, corpora, library, Linear)
  hlt_grounding.py       ← source validation, live/artifact delivery gate, memory quarantine
  hlt_media.py           ← Cloudinary for the media scope
  hlt_text.py            ← shared tokenizer/stopwords
mcp_server/tools.py      ← MCP tools; both default scope="auto"
mcp_server/automation_research.py ← automation executor/routes/readback; blocking compatibility + async jobs
mcp_server/automation_research_contracts.py ← versioned contracts, identity, capacity/deadline config
mcp_server/automation_research_store.py ← durable SQLite admission, queue, lease/fencing receipts
frontend/nextjs/         ← Mastery Research UI (Vercel)
docs/usage/              ← this folder — operator docs
docs/prd/mastery-brain.md
```

The strict automation route deliberately bypasses internal scope/memory
injection and uses only its typed source policy. REST `/api/quick_search` is
the other documented routing exception.

## Kill switches & deeper docs

| Want | Flag / doc |
| --- | --- |
| Turn Auto off entirely | `HLT_SCOPE_INFERENCE=0` |
| Heuristics only (no LLM tiebreak) | `HLT_SCOPE_INFERENCE_LLM=0` |
| Operator manual (agents, Sidecar, smoke) | [owners-manual.md](./owners-manual.md) |
| Scope chips + env table | [branding-and-catalyst-options.md](./branding-and-catalyst-options.md) |
| MCP curl / auth details | [hosted-mcp.md](./hosted-mcp.md) |
| Product vision / tabs | [../prd/mastery-brain.md](../prd/mastery-brain.md) |
| Agent rules for this repo | [../../AGENTS.md](../../AGENTS.md) |

## Smoke (prove Auto is alive)

```bash
# plain web — must stay empty
python scripts/smoke_websocket_ui.py --auto \
  --query 'NCLEX pass rates 2026 by state' --expect-empty-active

# estate — must pull codebase
python scripts/smoke_websocket_ui.py --auto \
  --query 'How does ScraperVault hand applications to nursing-mastery?' \
  --expect-active codebase
```
