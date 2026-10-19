# Cleo: working fully, current, and pleasant to use

Living plan. Started 19 Oct 2026 from Alec's ask: "get him working perfectly
and fully updated with a nice UI and everything, and flawless."
Board thread: `b73a5541` (HLT program); claims in that thread.
Update the checkboxes and the "Where things stand" block as work lands.

## What "done" means

Cleo is the Nursing Mastery product owner. Alec chose all four jobs for her
(19 Oct):

1. **Answer the team in Slack** with grounded, finished answers.
2. **Do Katailyst (K2) missions** handed to her by people or other agents.
3. **Proactive weekly work** that helps without spamming.
4. **File and groom Linear** for the Nursing Mastery board.

And all three surfaces should feel good (19 Oct): her **Katailyst page**, her
**computer** (`/computer`, the Hermes workbench), and **how she looks in Slack**.
The runtime gets **upgraded to current Hermes on a test copy first**, then
switched over, so live Cleo keeps working throughout.

Done means each job is proven on the live system with a receipt, not a green
health check: a real Slack thread answered well, a K2 mission completed with
the right output, one weekly run that stayed quiet when nothing changed, and
one Linear grooming pass Alec approved with a single yes.

## Where things stand (update this block)

| Area | State | Evidence |
|---|---|---|
| K2 missions | Working | Runs `48f7bb09`, `00abe3e8`, `105ebcea` completed 19 Oct with receipts |
| Busy handling | Working | #158: a second mission waits instead of failing (live proof 19 Oct) |
| Approval cards on missions | Working | #157: auto-refused; seen in live logs 19 Oct |
| Mission tool weight | Shipped | #159: unattended missions lose shell/code/browser/etc. Measure tokens on next proof |
| Slack answers | Unproven | Only one Cleo Slack message in 2 weeks (a failure, 16 Oct) |
| Weekly work | Off | All Hermes cron jobs retired (Aug 21, Sep 7) after spam |
| Linear grooming | Not built | `file-a-linear-story` skill only; writes not gated in code |
| K2 page | Has bugs | Dry-run default, duplicate rows, wrong "Live in" link, no health line |
| Model route | Paid fallback | OpenRouter Astra; K2 has no `modelRoute`; Alec's sign-ins needed (owned by the plan v6 session) |
| Hermes version | 6 weeks behind | Pinned `29112bef` (v2026.8.31); latest v0.22.2 `d923a5eb` |
| Cost reporting | Broken | Every Cleo K2 run shows `spentUsd: 0`, so K2 spend caps can't bind |

## Ownership and coordination

- The **plan v6 session** (`session_012ahUWTwg8XdhMbkiJjcE6p`, Board `012562c0`)
  owns Cleo's **model route** and **env vars** (incl. `GPTR_MCP_URL`). Post on
  `b73a5541` before touching either.
- K2 agent-page work: reply on Board thread `5886e33f` first (a paused
  `agent-screens` session left WIP in `wip/2026-10-15/agent-screens`).
- K2 repo rules: `katailyst2/AGENTS.md` (worktree, focused tests + `pnpm test:tier0`,
  simplify + independent review, before/after in the Logbook, 120-word first screen).
- This repo: Cleo's tests run locally (`pytest` over the suites listed in
  `.github/workflows/hlt-checks.yml`); Actions do not run on PRs, so record the
  local result and an independent review in every PR body.

---

## Phase 1: Quick wins on today's runtime

Small, independent, each proven live. These all survive the upgrade (they are
in our own code, not Hermes overlays).

- [x] Refuse approval cards nobody can answer on K2 missions (#157)
- [x] Queue a mission while Cleo is busy; accept `cleo` and `agent:cleo` (#158)
- [x] Drop unattended-unsafe toolsets from K2 missions; first-turn answer rule (#159)
- [ ] Close K2 runs Hermes reports `interrupted` (restart mid-run) as failed with
      Hermes' reason, and treat `stopping` as running (`health_gateway._provider_status`)
- [ ] **Prove #159 live**: rerun the "no tools" proof; target zero tool attempts
      and well under the 67k input tokens of run `48f7bb09`
- [ ] **Report cost to K2.** Hermes returns token usage; K2 shows `spentUsd: 0`.
      Find where K2 turns host usage into `spentUsd` (K2 `agents.run.*`,
      `lib/agents/durable-agent-run-service.ts`) and either send a cost figure
      from the wrapper receipt or price usage in K2. Needed before any schedule
      relies on `spendCapUsd`.
- [ ] **Gate Linear writes in code.** `hlt_k2_context.effect_policy_decision`
      does not classify `mcp__linear__save_issue`/create/update; the skill's
      "wait for a yes" is only prompted. Classify writes as needing approval on
      Slack (where a person can say yes) and as staged proposals on K2 runs.
- [ ] **Slack: never ask for secrets.** Add to the Slack hint and K2 doctrine:
      credentials go to Render via Alec; never request or accept a key in chat
      (seen 11 Sep).
- [ ] **Slack: mission environment hint.** `render_config.build_config`'s
      `runtime_hint` tells every run it is "a hosted Slack teammate" with
      terminal/browser/computer. Make the API-run hint say what a K2 mission
      actually has.

## Phase 2: Her Katailyst page (repo `katailyst2`, lifts every agent)

Research: shared page `app/(dashboard)/library/[ref]/page.tsx`, rail
`components/library/detail/AgentOperationsRail.tsx`, glance `AgentGlance.tsx`,
folds `AgentProfileSections.tsx`, data `lib/surface/agent-profile-loader.ts`
and `agent-profile-view.ts`. Design rules: `DESIGN.md` (one thing at a time,
120 visible words on the first screen), `docs/design/katailyst-design-system/README.md`.
Rhyme with `components/runs/RunInsideView.tsx` and the Create home ask box.

### 2a. Bug fixes first (small PR, all agents)
- [ ] "Plan first (no spend)" is pre-checked, so for hosted agents (Cleo,
      Victoria, Julius, Lila) the ask box only records a dry run
      (`AgentOperationsRail.tsx` ~367, `lib/agents/runner-dispatch.ts:365`).
      Default it off and hide budget/dry-run for agents that run elsewhere.
- [ ] `page.tsx:204-212` drops `mcpActivity`, so "N calls into Katailyst" never
      renders for any agent. Pass it through.
- [ ] "Live in Nursing Mastery ↗" links nursingmastery.com for any agent bound
      to the product (`AgentProfileSections.tsx` 366-389,
      `lib/surface/product-home-urls.ts`). Show it only for an agent-specific venue.
- [ ] Health is missing from the page. Load `replyHealthFor` /
      `readAgentReplyRows` (`lib/agents/agent-reply-health.ts`) and show one
      status line under her name ("Answering normally · last reply 12 min ago").

### 2b. Redesign: one page, two readers
Readers: the NM team ("can Cleo answer my product question?") and Alec ("is
she healthy; let me under the hood"). One primary action: **Ask Cleo**.
- [ ] Hero: portrait, name, promise, one status line. Role only in the identity
      line; venue/tier move to details.
- [ ] Ask box (`AgentAskBox.tsx`, keep `id="agent-mission"` for Julius's CRM
      link): one textarea, three starter chips from her own use-case
      `inputExample`s (no hardcoded names), one **Ask** button, a quiet
      "Or message Cleo in Slack" line. Lands on the run page.
- [ ] "What Cleo is doing": 3–5 real asks (question, state word, time). Hide
      "Reply on slack/api" ledger receipts (count them in the status line),
      merge each K2 dispatch with its Hermes receipt (`externalRef hook:k2:<run>`),
      and keep operator proof runs out of the default list by a data rule.
- [ ] "What Cleo can help with": 2–3 plain cards from her use cases, replacing
      registry-type counts.
- [ ] One closed owner fold (only `canEdit`): health and activation ladder,
      **Open Cleo's workspace** (today's Open computer), Edit soul, settings,
      owner review, and the existing technical folds.
- [ ] Remove from the default view: "Health & tags" open-by-default, the fold
      nav, the duplicate Recent work fold.
- [ ] Update the pinned design tests (list in the research report §4) and post
      before/after desktop + phone in the Logbook.

## Phase 3: Upgrade Hermes to v0.22.2 on a test copy

Target: tag `v0.22.2`, **pin commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8`**.
Full inventory: every overlay, config key, API and build change, with
file:line evidence, from the 19 Oct research (`hermes-upgrade-inventory.md`,
summarized here; re-derive if the session scratchpad is gone).

Headline facts a future session must not miss:
- **All 19 overlays fail to apply.** 5 drop (web deps, both stream-final
  fixes, Codex Astra, most of Codex terminal refresh), 1 partly defers
  (`scheduled_run_budget`: keep only the loop `admit_iteration` hook), 13 port.
- **Python 3.13 no longer works**: every core dependency is marked
  `python_version >= '3.14'`; pip on 3.13 installs Hermes with no deps.
- **Dashboard/TUI build changed**: `web/package.json build` runs
  `scripts/build/web.mjs`, which requires `--source/--out/--icons` and has no
  `--base`; we still need a `/computer/` base (lazy chunks aren't rewritten).
- **Set `HERMES_NODE`** or the chat TUI refuses to find Node (`hermes pm`,
  lazy installs off).
- **`max_iterations` on `/v1/runs` is silently ignored** without our port, and
  `/v1/runs` now pools the memory manager and never calls `agent.close()`.
- **Tool renames**: `cronjob` → `cronjob_manage`, `todo` → `todo_list`,
  `process` → `process_manage`. Our approval check keys on `cronjob`, so cron
  writes would bypass it. Fix in the plugin before switching.
- **Approval cards still appear on API runs** (a registered notifier keeps ask
  mode). Keep `_deny_unattended_approval`.
- **`state.db` schema 26 → 31.** Snapshot `/data/hermes` before first boot;
  rollback cannot assume the old image reads a migrated DB.
- Dashboard lifespan now starts a group-chat room service (a second
  `state.db` writer). Decide whether to disable it.
- New default `tools.tool_search.defer` hides computer use, image generation,
  cron and others behind the search bridge. Decide the list explicitly.
- Unchanged and safe: `/health`, `POST /v1/runs` (202, `run_<32hex>`, 429 when
  busy), `GET /v1/runs/{id}`, `/approval` deny-all, `/stop`, port 8642,
  `API_SERVER_KEY`, `max_concurrent_runs`, `hermes gateway run
  --external-supervisor`, plugin hook kwargs, cron job API, every config key we write.

### 3a. Test service
- [ ] Create a Render service `hlt-cleo-qual-<sha>-<date>` (the suspended
      `hlt-cleo-qual-9944edc-20260928` is the precedent) from a branch, with its
      own disk, **no Slack tokens** (or a test Slack app) and its own hook token,
      so it cannot answer the real workspace or collide with live Cleo.
- [ ] Copy live env except Slack and K2 identity binding; point K2 at it only
      through a test agent ref, never `agent:cleo`, until cutover.

### 3b. Port order (each step builds and boots)
1. [ ] Build skeleton: Python 3.14 (upstream `hermes pm build_env` from
       `uv.lock` preferred; pip on `python:3.14-slim-trixie` acceptable),
       extras `slack,mcp,tts-premium,fal,firecrawl,web,ddgs`, new web/TUI build
       commands with a `/computer/` base shim, `ENV HERMES_NODE`, fixed
       Dockerfile gates (`tools.mcp_tool_schema.mcp_prefixed_tool_name`, the
       `("GET", "/v1/runs/{run_id}"` grep). Drop overlays 1.1, 1.2, 1.3, 1.8, 1.9.
       Goal: `/health`, `/v1/runs` and the dashboard work.
2. [ ] Safety: plugin `cronjob_manage` effect fix; approval patterns +
       `execute_code` effect classifier (from the lifecycle patch, part e);
       explicit `approvals.unattended_mode: deny`.
3. [ ] K2 run contract: `api_runs_turn_budget` + loop hook; `api_runs_numeric_grounding`
       with a session-end fix that is safe with memory pooling. Consider sending
       `Idempotency-Key: <wrapper id>` (native now) to retire the ambiguous-dispatch class.
4. [ ] Trivial ports: platform turn budget, `always_loaded` tools (+ decide
       `defer`), K2 pack prompt refresh, quiet provider failover, bare-transfer
       context, readiness counts.
5. [ ] Spillover / `read_spillover` (progressive tool-result compaction).
6. [ ] Slack admission: pre-dispatch before typing (multi-agent correctness).
7. [ ] Managed model route + `/model` lock (lifecycle patch part a).
8. [ ] Slack streaming UX (largest): single-stream progress + stream priming +
       native Stop confirmation, with a live DM canary for ack, progress, final, Stop.
       Fold in Phase 4's progress-residue fix here rather than porting it twice.
- [ ] Rewrite the 12 `assert_*.py` checks against the new tree (retire the
      Astra and cron-budget ones while those paths are off).
- [ ] README/AGENTS: replace the "15 of 16 overlays" narrative, Python 3.13 and
      Node build descriptions.

### 3c. Cutover
- [ ] Qualify on the test service: Slack DM canary (test app), three K2
      proofs (no-tools, research, queue), dashboard Chat + `/api/pub` sidebar.
- [ ] Snapshot `/data/hermes` on live, then point `hlt-hermes` at the branch.
- [ ] Prove live; keep the old image tag for rollback with the snapshot.
- [ ] Suspend and later delete the test service.

## Phase 4: Slack presentation

Today: one native stream per turn (ack, progress, final). Problems seen:
progress paragraphs stay at the top of long answers; 2–3x too long for a
decision; approval cards with raw shell; one promised draft never arrived;
she cannot read channel history.
- [ ] Progress stays short and is not left as paragraphs above the answer
      (status line / task card, or at most two short lines). Do this inside the
      Phase 3 streaming port.
- [ ] Guarantee a final: when the turn or tool-round cap trips, force a
      no-tool synthesis turn; never seal a stream with progress only.
- [ ] Default length rule (about 150 words, offer the full version or a file).
- [ ] Remove terminal/code/computer from the default Slack toolset or gate
      them behind an explicit "use your computer" ask, so host introspection
      never produces approval-card spam.
- [ ] Resolve the table-rendering conflict between repo AGENTS and K2 doctrine.
- [ ] Mount a read-only Slack history reader so "read the channel and reply" works.

## Phase 5: Proactive weekly work (after cost reporting and model route)

History: three weekly briefs paused 21 Aug ("status noise and stale
autonomous conclusions"); three fleet jobs retired 7 Sep ("never ever spam
like this again"). So: one job, quiet by default.
- [ ] One K2 schedule (`agents.schedule.create`, staged paused): Monday product
      owner pass: what shipped, what is stuck, the funnel, one grooming
      proposal set, one high-leverage question advanced.
- [ ] Report to the Board thread; Slack DM Alec only when he must decide
      (max one message, no "Cronjob Response" wrapper); one line and no Slack
      when nothing changed.
- [ ] Change-gated against last week's post; real budget (`maxTurnsPerCycle`,
      timeout); run on the subscription route, not paid OpenRouter.
- [ ] `agents.schedule.fire` once, read the run, then `set_enabled`.

## Phase 6: Linear grooming

Board today (NUR, 100 most recent backlog): ~44 without a project, 3
assigned, NUR-723 Urgent in Backlog, ~20 Katailyst issues in NUR though KAT
exists, four projects past target.
- [ ] New skill `groom-the-nm-board` (registry + `grounding/cleo/skills/`):
      triage new issues, re-home KAT items, flag stale (>21 days), correct
      projects past target, reconcile shipped work, name the top five.
- [ ] Propose as one checklist; apply only after Alec's yes; log each write
      (before/after) on the Board. Depends on Phase 1's Linear write gate.
- [ ] Ride the Phase 5 weekly run.

## Phase 7: Model route (owned by the plan v6 session)

- [ ] Alec signs in: ChatGPT/Codex (ideally three) and Grok, through Cleo's
      workspace link; optional `XAI_API_KEY`.
- [ ] Set `policies.routing.modelRoute` on `agent:cleo`, e.g. primary
      `openai/gpt-5.6-sol`, fallbacks `xai/grok-4.6`,
      `openrouter/openai/gpt-6.1-sol` (all allowlisted in `render_config.py`).
- [ ] Restart once; prove with `/health.config.model_route_selection.source ==
      "katailyst2"` and one DM + one K2 proof. Note: the Codex wire rejects
      output caps, so budgeted scheduled runs should use Grok.

## Traps (each cost real time before)

- A green `/health` is not proof. Prove with a real run and its receipt.
- K2's agent ref may arrive as `cleo` or `agent:cleo`.
- Hermes' 429 at admission means the run never started; other 4xx are final.
- The ledger stores no mission text by design; anything that must survive a
  restart needs K2's replay.
- `platform_toolsets` is an allowlist: a connected MCP server is not granted
  until its `mcp-<server>` toolset is listed.
- Every deploy restarts Cleo and interrupts any running mission.
- `hlt-hermes` is the Render hostname only; the agent is Cleo. `agent:hermes`
  in K2 is a different agent.
