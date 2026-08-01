# Mastery Research — start here

This fork hosts **Mastery Research** (also called Mastery Brain): HLT's
team research tool over web + our estate. The upstream README below this
folder is Assaf Elovic's open-source GPT Researcher — keep that intact for
upstream sync. Everything HLT-specific lives in the surfaces and modules
below.

## What it is (one sentence)

Ask a question in plain English; Auto decides whether the answer needs our
code/registry/metrics/media or just the public web, then researches and
cites sources.

## Three doors (pick one)

| Who | Door | URL / mount |
| --- | --- | --- |
| Humans (Kim, Bruce, marketing) | Browser UI | https://gpt-researcher-ui.vercel.app |
| Agents (Claude Code, Cursor, Katailyst) | MCP | `https://gpt-researcher-mcp-production.up.railway.app/mcp` · Bearer `$GPTR_MCP_TOKEN` |
| Scripts / Sidecar | REST API | `https://gpt-researcher-api-production.up.railway.app` · `X-API-Key: $API_AUTH_KEY` |

- UI + MCP default to **Auto scope** (estate context when the question needs
  it; pure web otherwise).
- REST `POST /api/quick_search` is deliberately **web-only** — use `/report/`
  or MCP when a script needs estate awareness.

Health: `curl -fsS …/health` on the API and MCP hosts above.

## Signing in (the UI has a team password)

The browser UI sits behind a shared-password gate: opening any page bounces
you to `/login`. Enter the team password once — the session cookie lasts
30 days per browser.

- **Where the password lives:** Vercel project `gpt-researcher-ui` →
  `TEAM_ACCESS_PASSWORD` (Production), mirrored in Doppler
  `hlt-agent-tokens/dev` as `GPTR_TEAM_ACCESS_PASSWORD`. Ask Alec if you
  don't have it. Vercel env values pull back redacted on this team, so the
  Doppler mirror is the recoverable copy.
- **Rotating it:** `printf '<new>' | vercel env add TEAM_ACCESS_PASSWORD
  production --force` from `frontend/nextjs/`, then redeploy (below), then
  update the Doppler mirror. `TEAM_ACCESS_COOKIE_SECRET` is separate, so a
  rotation does **not** log anyone out.
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

Seven canned questions with scope + content assertions; exit code = number
of failures. Two cases are known-flaky at `fast` depth (`katailyst-registry-scope`
occasionally finishes without report content; `scopeless-pizza` occasionally
name-drops HLT) — a single flaky miss on those two is noise, repeated misses
or any failure on the two `nursing-mastery-*` glossary cases is real.

## How to use it in 30 seconds

**Human:** open the UI → leave **Auto** on → ask. Pin Code / Registry /
Metrics only when you want to force a lane. Watch the phase rail: it will
say *Auto-detected: Code (mentions ScraperVault)* when Auto fires.

**Agent:** call `deep_research` (or `quick_search`) with the default
`scope="auto"`. Pin `["codebase","cms"]` or pass `"none"` to override.
Estate questions on `quick_search` escalate to a short scoped research pass
(`mode: "scoped_research"`) — expect seconds, not milliseconds. Pass
`scope="none"` for a guaranteed cheap web scan.

**Canonical estate repos Auto knows about:** nursing-mastery, ScraperVault,
katailyst2, MMM2, EBB (override with `HLT_CODEBASE_REPOS`).

## Where the HLT code lives

```
backend/server/
  hlt_extensions.py      ← router: auth, readiness, MCP presets, prepare_research_request
  hlt_scope_inference.py ← Auto: heuristics → optional FAST_LLM tiebreak
  hlt_brain.py           ← /api/brain/* (repos, corpora, library, Linear)
  hlt_media.py           ← Cloudinary for the media scope
  hlt_text.py            ← shared tokenizer/stopwords
mcp_server/tools.py      ← MCP tools; both default scope="auto"
frontend/nextjs/         ← Mastery Research UI (Vercel)
docs/usage/              ← this folder — operator docs
docs/prd/mastery-brain.md
```

All three research doors go through `prepare_research_request` except REST
`/api/quick_search` (documented exception).

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
