"""Deterministic current-run grounding for Hermes/K2 final answers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def _load():
    spec = importlib.util.spec_from_file_location(
        "hlt_numeric_grounding", SERVICE_DIR / "hlt_numeric_grounding.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


grounding = _load()


def test_exact_cleo_funnel_regression_fails_closed():
    ledger = grounding.NumericGroundingLedger("Review the maintained Nursing Mastery funnel.")
    ledger.observe_tool_result(
        "mcp__posthog__exec",
        {
            "insight": "RN7CyKkR",
            "steps": [
                {"name": "search", "count": 975},
                {"name": "detail", "count": 37},
                {"name": "apply", "count": 6},
                {"name": "received", "count": 2},
            ],
        },
    )

    verdict = ledger.validate(
        """## Maintained funnel
| Step | People |
| Search | 168 |
| Job detail | 159 |
| Apply started | 18 |
| Application received | 2 |
"""
    )

    assert verdict.ok is False
    assert [claim.value for claim in verdict.unsupported] == ["168", "159", "18"]
    assert verdict.as_dict()["status"] == "failed"
    assert "975" not in verdict.failure_message()


def test_corrected_funnel_and_grounded_percentage_pass():
    ledger = grounding.NumericGroundingLedger("Use the last 30 days.")
    ledger.observe_tool_result(
        "mcp__posthog__exec",
        "search count: 975\ndetail count: 37\napply count: 6\nreceived count: 2",
    )

    verdict = ledger.validate(
        """## Funnel performance
Search: 975 → detail: 37 → apply: 6 → received: 2.
Search-to-detail conversion: 37 / 975 = 3.79%.
"""
    )

    assert verdict.ok is True
    assert verdict.checked_claims == 7
    assert verdict.grounded_claims == 6
    assert verdict.derived_claims == 1


def test_explicit_future_targets_are_not_presented_as_observed_facts():
    ledger = grounding.NumericGroundingLedger("Current received applications: 2")
    verdict = ledger.validate(
        "Current received applications: 2.\nFuture target: 20 received applications per month."
    )

    assert verdict.ok is True
    assert verdict.grounded_claims == 1
    assert verdict.future_claims == 1


def test_user_request_is_evidence_but_prior_assistant_reasoning_is_not():
    from_user = grounding.NumericGroundingLedger("We spent $450 and received 9 applications.")
    assert from_user.validate("Spend was $450 for 9 applications.").ok is True

    no_user_fact = grounding.NumericGroundingLedger("Summarize the funnel.")
    verdict = no_user_fact.validate("Assistant reasoning estimated 9 applications.")
    assert verdict.ok is False


def test_failed_tool_results_do_not_ground_claims():
    ledger = grounding.NumericGroundingLedger("Read the application count.")
    ledger.observe_tool_result(
        "mcp__ebb__exec",
        "Error: stale fallback says applications=44",
        is_error=True,
    )

    verdict = ledger.validate("Application count: 44")
    assert verdict.ok is False
    assert verdict.successful_tool_results == 0


def test_technical_numbers_and_markdown_ordinals_are_outside_metric_gate():
    ledger = grounding.NumericGroundingLedger("Explain the repair.")
    verdict = ledger.validate(
        """1. Deploy commit 0fd5c5d9 on 2026-08-24.
2. The endpoint returned HTTP 404 at 10:52:38.
Version v1.2 is still pinned at https://example.com/build/1275.
"""
    )
    assert verdict.ok is True
    assert verdict.checked_claims == 0


def test_unverified_numbers_may_be_named_only_when_explicitly_labelled():
    ledger = grounding.NumericGroundingLedger("Explain why the old answer failed.")
    verdict = ledger.validate(
        "The previous draft's unsupported funnel was 168 → 159 → 18 → 2."
    )
    assert verdict.ok is True
    assert verdict.labelled_nonfacts == 4


def test_future_exemption_is_scoped_to_an_explicit_label_before_each_claim():
    ledger = grounding.NumericGroundingLedger("Current conversion is 12%.")

    broad_modal = ledger.validate("Conversion was 168% and should improve.")
    assert broad_modal.ok is False

    scoped = ledger.validate(
        "Current conversion: 12%.\nObserved conversion was 168%; target: 20%."
    )
    assert scoped.ok is False
    assert [claim.value for claim in scoped.unsupported] == ["168%"]
    assert scoped.future_claims == 1


def test_nonfact_exemption_cannot_be_added_after_an_unsupported_claim():
    ledger = grounding.NumericGroundingLedger("Explain the bad metric.")

    after_the_fact = ledger.validate("Application count was 168, which is unsupported.")
    assert after_the_fact.ok is False

    explicit_label = ledger.validate("Unsupported claim: application count 168.")
    assert explicit_label.ok is True
    assert explicit_label.labelled_nonfacts == 1

    mixed = ledger.validate(
        "Unsupported claim: application count 168, actual application count 975."
    )
    assert mixed.ok is False
    assert [claim.value for claim in mixed.unsupported] == ["975"]


def test_grounded_sum_and_difference_are_allowed_when_operands_are_shown():
    ledger = grounding.NumericGroundingLedger("Compare the counts 37 and 6.")
    verdict = ledger.validate(
        "Application funnel difference: 37 - 6 = 31.\nApplication funnel sum: 37 + 6 = 43."
    )
    assert verdict.ok is True
    assert verdict.derived_claims == 2


def test_word_based_grounded_ratio_is_recognized():
    ledger = grounding.NumericGroundingLedger("Search count 975; detail count 37.")
    verdict = ledger.validate(
        "Search-to-detail conversion: detail 37 of search 975 = 3.79%."
    )
    assert verdict.ok is True
    assert verdict.derived_claims == 1


def test_metric_table_context_carries_across_rows():
    ledger = grounding.NumericGroundingLedger("Find the results.")
    ledger.observe_tool_result("analytics", "{\"people\": 12, \"received\": 3}")
    verdict = ledger.validate(
        """| Step | People |
| --- | ---: |
| Search | 12 |
| Received | 4 |
"""
    )
    assert verdict.ok is False
    assert [claim.value for claim in verdict.unsupported] == ["4"]


def test_equal_value_from_unrelated_metric_does_not_ground_funnel_claim():
    ledger = grounding.NumericGroundingLedger("Read the maintained funnel.")
    ledger.observe_tool_result(
        "analytics",
        {"metric": "clicks", "value": 168},
    )

    wrong_label = ledger.validate("Search count: 168")
    right_label = ledger.validate("Click count: 168")

    assert wrong_label.ok is False
    assert right_label.ok is True


def test_code_examples_are_not_mistaken_for_business_claims():
    ledger = grounding.NumericGroundingLedger("Show a SQL example.")
    verdict = ledger.validate(
        """```sql
select count(*) as application_count from applications limit 100;
```"""
    )
    assert verdict.ok is True
    assert verdict.checked_claims == 0


def test_callback_adapter_observes_only_successful_completions():
    ledger = grounding.NumericGroundingLedger("Read metrics.")
    ledger.observe_tool_event("tool.started", "analytics", result="users=11")
    ledger.observe_tool_event(
        "tool.completed", "analytics", result="users=11", is_error=False
    )
    ledger.observe_tool_event(
        "tool.completed", "analytics", result="users=12", is_error=True
    )

    assert ledger.validate("Users: 11").ok is True
    assert ledger.validate("Users: 12").ok is False


def test_verdict_payload_is_bounded_and_does_not_echo_answer_text():
    ledger = grounding.NumericGroundingLedger("Read metrics.")
    verdict = ledger.validate("Application count: 999")
    payload = verdict.as_dict()

    assert payload["contract"] == "hlt.current_run_numeric_grounding.v1"
    assert payload["unsupported"] == [{"value": "999", "line": 1}]
    assert "Application count" not in str(payload)
