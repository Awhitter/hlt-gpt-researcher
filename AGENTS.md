# GPT Researcher — AGENTS.md

> Agent orientation for this repo. **Read this BEFORE making changes.** This is the
> AGENTS.md spec (agents.md, formalized 2025) — the de-facto agent README adopted
> by Cursor, Claude Code, OpenAI, Sourcegraph, Factory.

## What this repo is

Research orchestration lane. As of 2026-04-22, this fork is 24 commits behind
upstream `main` and carries HLT-specific commits on top; run
`git rev-list --left-right --count upstream/main...HEAD` before sync-sensitive
work. Used for deep customer/competitive research that feeds article writing.

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

1. **`katailyst2`** (HTTP, hosted) — the registry and AI operating layer, at
   `https://katailyst2.vercel.app/api/mcp`. **Katailyst v1 (`www.katailyst.com`)
   is reference-only prior art and is no longer mounted here.** Tools:
   `discover`, `traverse`,
   `get_entity`, `registry_capabilities`, `registry_health`, `registry_agent_context`,
   `katailyst_orchestrate`, `tool_describe`/`tool_execute`, `memory_query`/`memory_write`.
   First call to make in any new task: `discover` with a 2-sentence intent.
2. **`multimediaMastery`** (HTTP, hosted) — image / video / TTS generation,
   served by **mmm2** at `mmm2-three.vercel.app/api/media/v1/*` (the older
   `multimedia-mastery` deployment is retired). Default image model:
   FAL nano-banana-2. Cloudinary upload server-side.
3. **`github`** (HTTP, hosted) — GitHub's remote MCP server at
   `https://api.githubcopilot.com/mcp/`, used by the `codebase` research scope.
   Auth: `Authorization: Bearer ${GITHUB_MCP_TOKEN}`.
4. **`gpt-researcher`** (HTTP, hosted) — HLT-hosted GPT Researcher MCP server.
   URL: `https://gpt-researcher-mcp-production.up.railway.app/mcp`.
   Auth: `Authorization: Bearer ${GPTR_MCP_TOKEN}`. Tools:
   `deep_research`, `quick_search`, `write_report`, `get_research_sources`,
   `get_research_context`; resource: `research://{topic}`. Owner's manual:
   `docs/usage/owners-manual.md`.

Human UI: `https://gpt-researcher-ui.vercel.app` is branded as **Mastery
Research**. The launch screen includes compact HLT scope toggles for Deep web,
QBank, Media, Code, Registry, and Metrics. Those toggles are browser-safe
metadata; server-side preset expansion and Cloudinary media lookup live in
`backend/server/hlt_extensions.py`.

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
  `https://katailyst2.vercel.app/api/mcp` (needs Bearer auth). Katailyst2 is the
  only registry endpoint mounted in `.mcp.json`; v1 was removed so agents cannot
  silently research the wrong system.
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
