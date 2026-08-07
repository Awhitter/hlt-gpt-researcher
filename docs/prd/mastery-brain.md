# Mastery Brain — Product Requirements Document

**Status:** Active  
**Owner:** Alec Whitters / HLT  
**Repo:** `hlt-gpt-researcher` (Mastery Research)  
**Last updated:** 2026-08-06

## Vision

Mastery Brain is HLT’s reusable research core. Nontechnical teammates and
product-specific agents can ask it about any part of the estate, combine
internal sources with the public web, and receive a source-checked report. It
is a provider to Nursing Mastery and other use cases; it is not itself the
Nursing Mastery product or product agent.

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
current implementation and evidence; authority and permissions; documented
direction; unknown/unavailable; how to verify or change it; sources and
freshness. Change-oriented answers may create
a source-linked Linear request after confirmation; they never edit code or
production data.

## Trust contract

- Implementation claims cite an existing file, route, symbol, schema, or
  configuration at an exact commit SHA.
- GitHub paths are validated before a report becomes `verified`. For
  Code-scoped live research, partial or unavailable model output is discarded
  before UI delivery and before Markdown/PDF/DocX generation; the user gets a
  source-check notice instead.
- Legacy reports remain readable but begin as `unverified` and cannot feed
  future research memory until revalidated.
- Repository readiness is per repo: branch, commit SHA, index timestamp,
  status, and error. One healthy repo cannot turn the whole estate green.
- Person authority is field- and workflow-specific. The HLT account API owns
  signed-in identity, career preferences, and consent state. ScraperVault owns
  capture processing, jobs, applications, recruiting operations, and receipts;
  its local person projections still serve unlinked and operational lanes.
  Nursing Mastery is one nurse-facing consumer, Katailyst2 owns reusable
  registry/orchestration and product-agent context, and EBB/PostHog provide
  measurement evidence. The report must trace the exact field and flag sync
  freshness rather than flattening this into one “People database.”
- A repository can prove code or documented direction, not that an external
  system is live. Marketo is absent from the built-in live research sources;
  without an active connector inspected in the run, its current state is
  unavailable even if code or an older campaign receipt mentions it.

## Architecture

```
Mastery Research UI (Vercel)
  → GPT Researcher API + MCP (Railway)  [synced fork + HLT overlay]
  → Code/source MCP (Render)             [5 structural indexes + 2 source-only checkouts]
Katailyst2                               [agent registry/orchestration authority]
  → product/facilitator agents call the Mastery Research provider
Agent runtime adapter (Render)           [Cleo or Brian; gateway deployment]
  → mounts GPTR MCP, code/source MCP, Katailyst2, Linear
```

The reusable core and browser are separate from the agents that facilitate a
particular product conversation. Katailyst2 is the canonical home for those
agent capabilities and routing. `services/agent` is the deployment adapter for
the Cleo (Nursing Mastery product-owner/facilitator) and Brian (general
researcher) personas; it does not make the research core Nursing-Mastery-only.

### Estate repos (code scope)

- `Awhitter/hlt-gpt-researcher` — reusable research core and integrations
- `Awhitter/MMM2` — multimedia engine  
- `Awhitter/katailyst2` — AI primitives / registry / command hub  
- `Awhitter/evidence-based-business` (ebb) — metrics  
- `Awhitter/ScraperVault` — recruiting data backend  
- `Awhitter/nursing-mastery` — nurse-facing product surface  
- `HLT-Master/hlt-web-service` — HLT account API (exact source search; no
  structural GitNexus index)

Override via `HLT_CODEBASE_REPOS`.

## Data sources

| Source | Role |
|--------|------|
| Deep web (Tavily / Firecrawl) | External research |
| Code/source MCP (`CODEGRAPH_MCP_*`) | Exact current source + structural code Q&A (preferred for Code scope) |
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

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 1 | Upstream sync + tests + Railway redeploy | shipped |
| 2 | This PRD | shipped |
| 3 | Code/source service on Render + Code scope wiring | shipped — five structural indexes plus source-only HLT API and Mastery Research checkouts |
| 4 | Cleo/Brian runtime adapter on Render with Slack + MCP mounts | built; runtime and permission readiness are reported by `/health` (see `services/agent/README.md`) |
| 5 | Team Brain UI tabs (Ask / Codebase / Vision / Changelog / Roadmap) | shipped |

## Cost envelope (indicative)

- Render: code-graph + Hermes VMs (~$7–25/mo each on starter, plus disk)
- Railway: existing API + MCP
- Vercel: existing UI
- LLM/search: usage-based (OpenRouter, Tavily, etc.)

## Non-goals

- Replacing Katailyst2 as the registry / command hub
- Public library listing of internal UI components without explicit publish
- Writing strategy docs outside the product vision store / One Place capture path
