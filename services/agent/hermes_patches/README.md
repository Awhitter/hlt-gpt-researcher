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
