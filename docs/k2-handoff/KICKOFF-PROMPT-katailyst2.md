# Kickoff prompt — paste this into an agent running on `Awhitter/katailyst2`

> Why this file exists: the cloud agent that authored the registration package
> runs with credentials scoped to `hlt-gpt-researcher` only, so it cannot push
> to katailyst2. Launch a Cursor agent (cloud or local) on the katailyst2 repo
> and paste everything below the line as its task.
>
> **Shortest possible paste** (instead of the full text below):
>
> ```
> Fetch https://raw.githubusercontent.com/Awhitter/hlt-gpt-researcher/cursor/cleo-k2-org-map-fd55/docs/k2-handoff/KICKOFF-PROMPT-katailyst2.md and execute everything below its horizontal rule as your task.
> ```

---

Register Cleo — the Nursing Mastery product-owner facilitator that runs on the
Hermes Slack runtime (Render `hlt-hermes`) — as a first-class agent brain in
this registry. Her body already boots with `agent_ref: agent:cleo@v1` and her
grounding tells her to load a K2 packet, but no `cleo` row exists here, so
that call returns nothing.

The complete registration package (census row, seed field content, doctrine,
skill body, links, proof steps, and deliberate scope exclusions) is here —
fetch it first and treat it as the content source of truth:

https://raw.githubusercontent.com/Awhitter/hlt-gpt-researcher/cursor/cleo-k2-org-map-fd55/docs/k2-handoff/agent-cleo-registration.md

(If that branch is gone, the file lives at `docs/k2-handoff/agent-cleo-registration.md`
on `main` of `Awhitter/hlt-gpt-researcher` after PR #90 merges.)

Apply it using THIS repo's own current conventions — the package supplies
content, not exact code; where its field names and the current seed helpers
disagree, the repo's types win:

1. Add the `cleo` census row to `lib/agents/agent-census.ts` (kind
   `human_persona`, disposition `retain`). Validate the host-profile list
   against the current `supportedHostProfiles` enum; the profile covering the
   Hermes/Paperclip Slack lane must be present because that is the only live
   body. Do not add `openclaw`.
2. Create the agent seed (pattern: `lib/registry/seeds/agent-archimedes.ts` /
   the gold-fleet rows): agent + doctrine, seeded **staged**, `isOnline: false`,
   always-confirm posture for outward actions.
3. Seed `skill:nursing-mastery-facilitate-product-work` with the method body
   from the package — the Hermes-side shim already loads exactly that ref.
4. Wire the links: `uses_tool` → `tool:mastery-research` and
   `tool:deep-research-gather`; the collaboration links to `agent:lila`,
   `agent:victoria`, `agent:julius`, `agent:archimedes`,
   `agent:deep-researcher`, `agent:content`; doctrine `governed_by`.
5. Verify `agent:cleo@v1` resolves for the versioned ref the body pins.
6. Run this repo's own gates (`pnpm verify` and the registry/seed tests) and
   fix what they surface.
7. Prove it: `agents.runtime_pack`, `agents.shell_config`, and the compat
   `registry.agent_context` for `agent:cleo` must each return a real packet,
   not a miss. Record the receipts in the PR description.

Hard boundaries: do not flip `is_online` (fleet states: this lands at
*authored*; owner tuning comes later). Do not touch the v1 registry. Do not
create or modify any Slack binding — the Slack app stays owned by the Hermes
deployment. Open the work as a PR on this repo; do not merge it yourself.
