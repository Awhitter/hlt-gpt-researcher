# GPT Researcher — AGENTS.md

> Agent orientation for this repo. **Read this BEFORE making changes.** This is the
> AGENTS.md spec (agents.md, formalized 2025) — the de-facto agent README adopted
> by Cursor, Claude Code, OpenAI, Sourcegraph, Factory.

## What this repo is

Research orchestration lane + **Mastery Brain / Mastery Research** team
surface. Synced with upstream `assafelovic/gpt-researcher` (check with
`git rev-list --left-right --count upstream/master...HEAD`). Used for deep
customer/competitive research and for nontechnical teammates to ask
capability questions across the estate.

**Start here (operators + agents):** [`docs/usage/START-HERE.md`](docs/usage/START-HERE.md)
— one screen: what it is, three doors, Auto scope, module map, smoke.

PRD: `docs/prd/mastery-brain.md`. UI tabs: Ask / Audience / Codebase /
Library / Vision / Changelog / Roadmap. Code scope prefers `CODEGRAPH_MCP_*`
(GitNexus on Render) with GitHub MCP as fallback. Sidecars:
`services/codegraph/`, `services/brian/` (`render.yaml`). Tracked corpora
under `my-docs/`: `vision/`, `audience/` (voice-of-nurse quote bank + briefs),
`recruiting/` (generated nursingmastery.com content inventory —
`scripts/build_recruiting_inventory.py`), `design/` (Refero notes). All load
as hybrid-research context via `DOC_PATH`.

### HLT module map (do not dump new logic into one file)

```
backend/server/hlt_extensions.py      auth · readiness · MCP presets · prepare_research_request · install()
backend/server/hlt_scope_inference.py Auto scope (heuristics → optional FAST_LLM tiebreak)
backend/server/hlt_brain.py           /api/brain/* estate context, library, Linear
backend/server/hlt_media.py           Cloudinary for the media scope
backend/server/hlt_text.py            shared tokenizer / stopwords
mcp_server/tools.py                   MCP tools (default scope="auto")
```

Leaves never import `hlt_extensions`. New Brain/tab work goes in
`hlt_brain.py`; new Auto signals go in `hlt_scope_inference.py`; the router
stays the thin compose point.

## Where it sits in the HLT ecosystem

This repo is one of **14 active sibling repos under `~/hlt/`** that share the
**Katailyst registry** as their capability brain (1,663 entities, 11,151
graph links, 30+ MCP tools). The full ecosystem map lives at:

- **Master:** `~/hlt/katailyst/docs/ecosystem-map/05-llms-ecosystem-master.md`
- **Atlas:** `~/hlt/katailyst/docs/ecosystem-map/01-ecosystem-atlas-master.md`
- **Repo runtime ledger:** `~/hlt/katailyst/docs/ecosystem-map/03-repo-runtime-ledger.md`

Sibling repos: `katailyst, sidecar, mastery-publishing, multimedia-mastery,
engage, jobs, forum-template, agent-canvas, brand-design-lab,
evidence-based-business, gpt-researcher, mastra, paperclip,
research-team`.

## Tools available in this repo (auto-discovered via `.mcp.json`)

Drop into this repo with Claude Code / Cursor / any MCP client and the
following servers auto-mount from `.mcp.json`:

1. **`katailyst`** (HTTP, hosted) — the registry. Tools: `discover`, `traverse`,
   `get_entity`, `registry_capabilities`, `registry_health`, `registry_agent_context`,
   `katailyst_orchestrate`, `tool_describe`/`tool_execute`, `memory_query`/`memory_write`.
   First call to make in any new task: `discover` with a 2-sentence intent.
2. **`multimediaMastery`** (HTTP, hosted) — image / video / TTS generation.
   Live at `multimediamastery.vercel.app/api/media/v1/*`. Default image model:
   FAL nano-banana-2. Cloudinary upload server-side.
3. **`gpt-researcher`** (HTTP, hosted) — HLT-hosted GPT Researcher MCP server.
   URL: `https://gpt-researcher-mcp-production.up.railway.app/mcp`.
   Auth: `Authorization: Bearer ${GPTR_MCP_TOKEN}`. Tools:
   `deep_research`, `quick_search`, `write_report`, `get_research_sources`,
   `get_research_context`; resource: `research://{topic}`. Owner's manual:
   `docs/usage/owners-manual.md`.

Human UI: `https://gpt-researcher-ui.vercel.app` is branded as **Mastery
Research**. Ask is the always-on surface; the other tabs (Nurse voice,
Codebase, Library, Vision, Changelog, Roadmap) live behind "More research
tools" and render from `lib/brainTabs.ts` — that array is the single tab
registry, so do not add a second hardcoded list beside it.

Under the ask box sits a **suggestion strip**: three example questions with a
control to cycle to the next three. The bank comes from
`GET /api/brain/suggestions` (`backend/server/hlt_suggestions.py`), built by
filling curated templates with live nouns — Linear shipped issues, the weekly
content-inventory sweep, ranked nurse pain points, the code graph's repo list —
so it cannot go stale the way the old hardcoded chips did. Every entry pins the
scope it needs, which is what makes its label honest: explicit scope beats Auto
in `prepare_research_request`. The same endpoint feeds Brian's Slack suggested
prompts, so there is one bank, not two.

Ask keeps **Auto** visible as the only always-on control; the eight scope
toggles (Deep web, Nurse voice, Recruiting, QBank, Media, Code, Registry,
Metrics), the Fast/Balanced/Deep depth picker and the
Standard/Top 1% mode picker (top1 injects the cross-industry
winners → mechanisms → rhymes → audience-verification doctrine) all fold behind
one `Advanced` disclosure whose summary reports what is in force. Auto is on by
default and pins nothing: `backend/server/hlt_scope_inference.py`
infers the scopes a query needs (heuristics, then one FAST_LLM tiebreak for
weak signals, never `qbank`/`firecrawl`, never an unready integration), and the
phase rail shows what auto-fired and why. Pinning any toggle leaves auto mode.
MCP `deep_research`/`quick_search` default to the same `scope="auto"`; REST
`/api/quick_search` stays web-only by design. Toggles are browser-safe
metadata; `hlt_extensions.prepare_research_request` expands presets
(codegraph/GitHub/Katailyst/Apify/QBank), `hlt_media` handles Cloudinary,
and `hlt_brain` owns `/api/brain/*`. Live runs render a Plan/Search/
Read/Write phase rail (`components/brain/ResearchProgress.tsx`); raw agent
logs are collapsed by default.

Research memory: finished reports persist to `REPORT_STORE_PATH`
(`/data/reports.json` on the Railway volume) and are searchable at
`/api/brain/library` (Library tab). Run completion persists server-side
(`server_utils.handle_start_command`) and sends a `report_complete`
websocket message — pipelines like deep research don't stream `report`
chunks, so this is what delivers the final answer to the UI; the browser
then upserts the same research_id with its richer orderedData. `prepare_research_request` injects the
top related prior reports into new runs (disable per-request with
`hlt_research_scope.memory: false`).

Scraping stack: `SCRAPER=firecrawl` (Firecrawl API) is the production scraper
and `RETRIEVER=firecrawl,mcp` runs web search on the same Firecrawl plan via
the HLT-added `firecrawl` retriever (`gpt_researcher/retrievers/firecrawl/`);
Tavily remains supported but is off in prod (plan limit exhausted). Deep web
and Audience scopes also mount Apify's hosted MCP (`https://mcp.apify.com`)
when `APIFY_TOKEN` is set; Audience/Recruiting scopes force Firecrawl
scraping when configured. QBank scope prefers a dedicated `QBANK_MCP_URL`
partner-API MCP and falls back to the Katailyst tool path. Roadmap and
Changelog tabs pull live Linear data (projects + recently completed issues,
5-minute cache) when `LINEAR_API_KEY` is set, falling back to seed entries.

Automation: `.github/workflows/audience-sweep.yml` (Mondays) runs a deep
audience-scoped report against the hosted API, refreshes the recruiting
content inventory, and opens a PR into `my-docs/audience/`; it needs the
`API_AUTH_KEY` and `FIRECRAWL_API_KEY` repo secrets.
`.github/workflows/upstream-sync.yml` (Mondays) merges
`assafelovic/gpt-researcher` master into a `sync/upstream-*` branch, runs the
HLT test suite, and opens an AI-reviewable PR (with conflict markers
committed when the merge conflicts).

Katailyst registry: this service is registered as the tier-1 capability
`tool:http-api-https-gpt-researcher-api-production-up-railway-app-post-report`
(name "Mastery Research (GPT Researcher)", staged, org-shared). Re-register or
amend with `scripts/katailyst_mcp.py` + `scripts/katailyst_capability.md`.

Render sidecars: `hlt-codegraph` runs on the standard plan (gitnexus analyze
OOMs on 512MB) with a 10GB disk at `/data`; boot reindex runs in the
background so the port binds immediately. The second sidecar runs **Brian, the
Mastery Researcher** (`services/brian/`, 5GB disk) and stays in
readiness-gateway mode until Slack tokens exist — see
`services/brian/README.md` for the enable flow.

**Naming:** the agent is **Brian**. "Hermes" is only the upstream runtime we
embed (NousResearch hermes-agent) — never the agent's name. `agent:hermes` in
Katailyst2 is the fleet orchestrator persona, a different thing entirely, and
conflating the two is how the estate ends up with two Hermeses. The Render
service keeps the hostname `hlt-hermes` because Render cannot rename in place;
that hostname is infrastructure, not identity.

Brian's internals: `health_gateway.py` is the main process because the Hermes
gateway reaches Slack over Socket Mode and never binds a port — it would fail
Render's health check as PID 1. That module installs grounding
(`grounding.py`), renders `$HERMES_HOME/config.yaml` from env
(`render_config.py`), supervises `hermes gateway` as a child, and serves
`/health`. Do not add config keys by hand: `render_config.py` is the only
writer and `tests/test_brian_boot.py` pins the schema. `/health` reports
observed state (`gateway.running`, `config.mcp_mounted`), so treat
`status: degraded` / `mode: gateway_down` as a real outage even though the HTTP
code stays 200.

Three rules that exist because each was once a silent no-op:

1. **`platform_toolsets.slack` is a security control, not a preference.**
   Upstream's default Slack toolset is "full access for workspace use" —
   terminal, execute_code, cronjob, computer_use. Brian is reachable by the
   whole workspace and reads untrusted web pages. Never widen that list without
   reading `tests/test_brian_boot.py::test_slack_toolset_excludes_host_access`.
2. **Company facts go in `services/brian/grounding/AGENTS.md`**, loaded from
   `terminal.cwd`. Not `MEMORY.md` — that is ~2200 chars, frozen per session and
   agent-writable.
3. **Hermes reads memory from `$HERMES_HOME/memories/MEMORY.md`** (plural dir,
   fixed filenames). An earlier seeder wrote `$HERMES_HOME/memory/*.md` and
   nothing ever read it.

AI observability: HLT-hosted GPT Researcher emits Langfuse observations when
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are configured. `/health`
reports redacted readiness under `observability.langfuse`; prompt/output capture
stays off unless `LANGFUSE_RECORD_IO=true`.

## Rules of engagement

1. **Katailyst first** — for any task that decomposes into multiple facets,
   call `discover` against the registry before assuming what's available.
   Many things you might want to build already exist as skills, KBs, prompts,
   playbooks, rubrics, schemas.
2. **Upstream-compatible changes only** — this is an external community repo,
   not an HLT-owned product codebase. Prefer configuration, deployment wrappers,
   docs, hosted MCP registration, and isolated HLT modules over broad edits to
   upstream files. If a change belongs upstream, keep it small enough to PR
   cleanly or document it as an overlay.
3. **No stale docs** — if you change behavior that this AGENTS.md describes,
   update this file in the same commit.
4. **Registry counts rule** — when surfacing a count from the registry,
   use `registry_capabilities` full-scope (System + Org merged). Don't quote a
   single-slice number as "the" count.
5. **Status lifecycle** — registry entities use `staged → curated → published →
   deprecated → archived`. There is **no `draft` status**. Run-step statuses
   are `pending, running, completed, failed, skipped` (no `blocked`).

## Read first (in order)

1. `llms.txt` — repo orientation auto-generated nightly, link cross-repo.
   Root `llm*.txt` dumps are gitignored in this fork; if absent, regenerate
   them from the nightly hygiene workflow or use the ecosystem maps above.
2. `README.md` — human-facing overview if present
3. `package.json` (or `pyproject.toml` / `Cargo.toml`) — stack + scripts
4. The "Inspect first in this repo" list in `llms.txt`

## Inspecting the live system

- **Cron status:** `~/hlt/katailyst/.github/workflows/repo-hygiene-nightly.yml`
- **Registry health:** call `registry_health` MCP tool, or hit
  `https://www.katailyst.com/mcp` (needs Bearer auth)
  Katailyst2 is also available at `https://katailyst2.vercel.app/api/mcp`; repo-local agents can
  choose either because `.mcp.json` exposes both endpoints explicitly.
- **Drift report:** `bash ~/.openclaw/workspace/system/check-llms-drift.sh`

## Honest scope of this stub

This file is a **stub auto-generated 2026-04-17** by the observability +
discoverability perfection arc (see
`~/hlt/katailyst/docs/planning/active/2026-04-17-observability-discoverability-perfection.md`).
It is the same shape across all 15 hlt repos so an agent landing cold has
a consistent first read. Per-repo specifics belong in `llms.txt` (which IS
maintained nightly from the canonical Obsidian system maps).

If you make a meaningful behavior change in this repo, edit this file by
hand to capture the new agent-relevant constraints. Don't let the
auto-generated stub become the only word on the repo's actual rules.
