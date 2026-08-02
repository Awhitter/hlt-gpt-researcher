# Mastery Brain — Product Requirements Document

**Status:** Active  
**Owner:** Alec Whitters / HLT  
**Repo:** `hlt-gpt-researcher` (Mastery Research)  
**Last updated:** 2026-08-02

## Vision

Mastery Brain is the personal research OS for HLT’s product estate. Nontechnical teammates can talk to it, see what each codebase can do, ask “can we do X?”, store vision, and watch an interactive changelog of what shipped — powered by a maxed-out GPT Researcher, a code-graph MCP over the estate repos, and a persistent Hermes agent that learns across sessions.

It leans toward **2027**: frontier models via OpenRouter (swappable), subagents for parallel research, MCP-native integrations (Katailyst2, Linear, code graph, media), and highly interactive visuals — not a static FAQ wiki.

## Personas

| Persona | Needs |
|---------|--------|
| **Nontechnical teammate** (marketing, recruiting, ops) | Plain-English answers, visuals, “can we do this?” without reading code |
| **Alec** | Store vision, steer product, deep research across repos + web + registry |
| **Agent consumers** (Katailyst2, Hermes, other MCP clients) | Reliable `deep_research` / `quick_search` / `/gather` with scope presets |

## Human experience

The primary browser surface is one plain-language prompt, **Ask about HLT**,
with the existing compact scope controls visible. The UI does not hardcode
example questions: examples belong in acceptance tests, not permanent copy.
Codebase, Audience, Library, Vision, Changelog, Roadmap, and technical
preferences remain available through progressive disclosure.

Implementation answers use a stable plain-English order: direct answer;
what happens and when; data captured and storage; where behavior lives; how
to change it; sources/freshness/unknowns. Change-oriented answers may create
a source-linked Linear request after confirmation; they never edit code or
production data.

## Trust contract

- Implementation claims cite an existing file, route, symbol, schema, or
  configuration at an exact commit SHA.
- GitHub paths are validated before a report becomes `verified`; partial or
  unavailable validation stays visible.
- Legacy reports remain readable but begin as `unverified` and cannot feed
  future research memory until revalidated.
- Repository readiness is per repo: branch, commit SHA, index timestamp,
  status, and error. One healthy repo cannot turn the whole estate green.
- ScraperVault owns recruiting/profile/application truth; Nursing Mastery is
  the nurse-facing consumer; Katailyst owns capabilities; EBB/PostHog provide
  measurement evidence. Missing live-system access is unavailable, not a guess.

## Architecture

```
Mastery Research UI (Vercel)
  → GPT Researcher API + MCP (Railway)  [synced fork + HLT overlay]
  → Code-graph MCP (Render)             [GitNexus indexes 5 repos]
Hermes agent (Render VM)
  → mounts GPTR MCP, code graph, Katailyst2, Linear
  → Slack/Telegram gateway for the team
```

### Estate repos (code scope)

- `Awhitter/MMM2` — multimedia engine  
- `Awhitter/katailyst2` — AI primitives / registry / command hub  
- `Awhitter/evidence-based-business` (ebb) — metrics  
- `Awhitter/ScraperVault` — recruiting data backend  
- `Awhitter/nursing-mastery` — nurse-facing product surface  

Override via `HLT_CODEBASE_REPOS`.

## Data sources

| Source | Role |
|--------|------|
| Deep web (Tavily / Firecrawl) | External research |
| Code-graph MCP (`CODEGRAPH_MCP_*`) | Structural code Q&A (preferred for Code scope) |
| GitHub MCP | Fallback code/search |
| Katailyst2 MCP | Registry, skills, playbooks |
| Cloudinary | Media library |
| Metabase / Katailyst metrics | Metrics scope |
| Linear MCP | Roadmap + releases |
| Productboard | Roadmap (when keyed) |
| Vision docs (`my-docs/vision/`) | Hybrid research context |

## Model strategy

- Default frontier stack via OpenRouter (SMART / STRATEGIC / FAST LLMs configurable).
- Subagents for parallel code + web + registry retrieval.
- Hermes persistent memory + skill loop for cross-session learning.
- Swap models without code changes (env / OpenRouter).

## Success criteria

1. A marketer gets a correct, visual answer to “can MMM2 do X?” in under 2 minutes.
2. Code scope uses the code-graph MCP when configured; GitHub MCP is fallback only.
3. Vision docs appear in hybrid research citations when relevant.
4. Changelog shows at least the last 30 days of estate activity in plain English.
5. Roadmap tab reflects live Linear milestones for Nursing Mastery workspace.
6. `/gather` and hosted MCP tools remain green for Katailyst2 consumers.
7. Upstream sync remains re-applicable: HLT logic stays in overlay modules.

## Rollout phases

| Phase | Deliverable |
|-------|-------------|
| 1 | Upstream sync + tests + Railway redeploy |
| 2 | This PRD |
| 3 | Code-graph service on Render + Code scope wiring |
| 4 | Hermes on Render with Slack + MCP mounts |
| 5 | Team Brain UI tabs (Ask / Codebase / Vision / Changelog / Roadmap) |

## Cost envelope (indicative)

- Render: code-graph + Hermes VMs (~$7–25/mo each on starter, plus disk)
- Railway: existing API + MCP
- Vercel: existing UI
- LLM/search: usage-based (OpenRouter, Tavily, etc.)

## Non-goals

- Replacing Katailyst2 as the registry / command hub
- Public library listing of internal UI components without explicit publish
- Writing strategy docs outside the product vision store / One Place capture path
