# Hermes 0.21.5 HLT contracts

Upstream: `v2026.9.24`, commit `f97608f178d1ffeca59860195ab7da295f7c8e5f`.
The runtime image applies `web_dependency_security.patch` and then
`hlt_runtime_contract.patch`; the web builder applies the same security patch.
Both patches must apply cleanly to that exact commit. Do not replay the previous
August overlay series against the new runtime.

| Retained behavior | Current native owner and validation |
| --- | --- |
| Account catalog, Astra reasoning, context provenance | Already upstream in `agent/model_metadata.py`, `agent/reasoning_effort.py`, `hermes_cli/codex_models.py`, `agent/codex_headers.py`; `assert_codex_astra.py` exercises actual catalog and wire methods with synthetic accounts. |
| Manual Codex grants and terminal rejection | `agent/credential_pool.py` preserves each manual grant's exact pool row, adopts a peer's completed rotation, marks only a rejected grant dead, and leaves quota cooldowns intact; `assert_codex_terminal_refresh.py`. |
| Subscription first, funded fallback, reset recovery | Native provider fallback and recovery remain the owners. Managed gateway routing uses Codex Astra/high then OpenRouter Astra and ignores stale session overrides; `assert_provider_failover_request_contract.py` and native Slack tests. |
| Human-only admission and shared lead | Native pre-dispatch hook runs before typing/model work. HLT plugin 1.7 claims the canonical K2 decision using the raw human message, injects the shared role, and fails closed without burning replay admission. Bot messages never claim. Wrapper coordination tests and `assert_pre_gateway_dispatch.py`. |
| One Slack stream, early acknowledgement, Stop | Current gateway turn/runner and stream transport/fallback owners retain a single public stream, final-message dedup and worker-drained cancellation; `assert_slack_single_stream_progress.py` plus native Slack tests. |
| K2 pack refresh | Current system-prompt and conversation owners retain shared pack-read locking and version-based prompt invalidation without discarding history; `assert_k2_runtime_pack_prompt_refresh.py`. |
| API grounding and durable close | `gateway/platforms/api_server_runs.py` captures raw current-run tool results, validates before completion, and closes the agent in its worker finally block. Resolved numbered source citations are not numeric metrics. `assert_api_runs_numeric_grounding.py` and wrapper numeric regressions. |
| Progressive results and scoped eager tools | Current tool executor/result store scopes spillover to the session; tool discovery retains full input schemas while bounding descriptions and schema batches. `always_loaded` only promotes tools already within the session's scope. `assert_progressive_tool_result_compaction.py`, `assert_always_loaded_tools.py`. |
| Native run budgets | API admission carries its explicit turn ceiling into the real agent. Slack has its separate ceiling. Opt-in scheduled budgets cover the native loop, actual request owner and Codex stream retries. `assert_hosted_turn_budget.py`, `assert_platform_turn_budget.py`, `assert_scheduled_run_budget.py`. |
| Broad autonomous read/stage with real send boundaries | Managed `execute_code` classifies effects before execution; native approvals retain outward sends/publish/delete boundaries. Curl, wget and GitHub mutations are classified in the current detection owner. Native Slack regression cases exercise read/stage and outward effects. |
| Web/TUI capability and dependencies | Current native web and TUI builds, mounted `/computer/` assets, pinned Node/Python/SQLite, and required media/research extras remain image gates. |

Offline checks and image build success establish candidate compatibility, not
live account entitlement or operational completion. Rollout still needs the
observed deployed SHA, provider/model, useful saved artifact, durable terminal
receipt, and controlled Slack acknowledgement/Stop behavior.

### Managed provider continuation

The pinned native `/v1/runs` owner accepts `provider_recovery: true` only for
an authenticated, durable, default-profile managed route with an explicit
active execution budget. Admission supplies its normal idempotency key once.
An exhausted model request may checkpoint the current turn at a settled tool
boundary. The same native run, task, turn, message markers, guardrails, numeric
evidence and cumulative budgets survive a wait and restart. SQLite generation
claims serialize resumption; claimed-but-interrupted work is never replayed.
Unknown effects, detached child/process work, changed transcripts and unsupported
checkpoint state require reconciliation. No new request is admitted on retry.

`POST /hooks/agent/runs/{wrapper_run_id}/cancel` forwards Stop to the originally
bound native ID. The admission advertises `hermes_hook_v1` only after native Stop
capability confirmation. Pending Stop is `running` plus
`cancellationRequested: true`; cancellation is reported only from settled native
status. Provider waiting pauses the execution clock without resetting its
consumed budget or iteration ceiling. The private checkpoint remains in the
native permission-restricted SQLite store and is cleared on completion or confirmed
Stop; failed continuations retain it for reconciliation under the existing retention
policy. Exact source, active governance-pack and credential-free primary/fallback/tool-policy fingerprints
block incompatible resumption. Credential refreshes do not change those fingerprints.
An older runtime may report an interrupted run after rollback; re-upgrading never
resumes such a row, even if its unfamiliar checkpoint columns still say waiting.
If rollback leaves a parked row untouched, only the exact compatible runtime and
policy may claim it again. Transcript edits also block automatic resumption.

`assert_provider_wait.py` exercises native admission, exhaustion, adapter restart,
CAS resumption, transcript/tool/result custody, budget retention, Stop tombstones
and ambiguous claimed leases using synthetic providers and no model calls.

Ordinary managed Slack requests use the same native turn checkpoint through a
separate private SQLite owner in `hlt_slack_provider_wait.py`. The original
adapter task and session reservation survive waiting; its model worker and
execution-capacity lease do not occupy capacity while parked. Restart restores
the admitted event, native turn, tool-round budget and acknowledged stream ID
without passing through inbound admission or electing a lead again. The current
K2 coordination decision is read before resumption and finalization; a changed
lead or authorization requires reconciliation. Human follow-ups wait in a
durable FIFO. Its rows retain custody until native admission acknowledges the
original human message. Startup dispatches definitely unadmitted queued work
only after its parent completes or is cancelled; dead claims and failed parents
require reconciliation. Bot messages remain excluded by existing admission and the queue.

Native Slack Stop and typed Stop/reset persist a tombstone before releasing
the original generation. Unknown stream acknowledgements, incompatible state
and crashed claimed leases retain private evidence without automatic replay.
The generic restart-message path excludes owned checkpoints. Disconnect does
not seal a parked stream, and restored consumers use the same Slack message;
only acknowledged final delivery or confirmed Stop clears a recovered checkpoint.
A failed SQLite park also writes a private reconciliation journal; this journal
is never an execution source. Reconciliation retains the original stream locator
and honest thread status, and an exact Slack Stop can close it without touching
a newer request. A failed startup dispatch releases only its own generation and
guard, leaving the private checkpoint blocked for reconciliation.

`assert_slack_provider_wait.py` covers native SQLite ownership, Stop, queue
deduplication/admission acknowledgement, terminal-parent restart recovery,
capacity reacquisition, failed dispatch cleanup, stream restoration, failed
serialization/SQLite parking, uncertain delivery and lead handoff. The real
native-loop fixture also invokes `TurnRunner.run_sync` on Slack, proving a
completed tool followed by dual-provider exhaustion skips stream finalization
and resumes the same native turn. These are authored qualification checks;
hosted Slack behavior still requires a deployed runtime receipt.

Artifact saves from K2-managed API runs receive `agentRunId` at the native
pre-tool hook. The binding comes from the exact authenticated wrapper admission
and native idempotency reservation, held in a worker-local ContextVar and reset
in the native worker's `finally`. Direct `artifact.save` and `tool.execute`
forms preserve the original payload; conflicting model IDs are blocked. Slack
and ordinary API sessions receive no inferred run association. The actual-loop
proof exercises this through the native API worker and a separate native tool
worker, including a mismatched native admission that must persist an explicit
block without invoking artifact transport; focused helper cases cover foreign
bindings and conflicts. Tool checkpoints also reject native unknown/nonterminal
effect dispositions before interpreting result text, including timed-out file
writes whose receipts are plain text.
