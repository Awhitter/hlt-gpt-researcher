"""Deterministic current-run grounding for Hermes/K2 final answers."""

from __future__ import annotations

import importlib.util
import json
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


def test_hosted_posthog_wrapper_preserves_table_column_labels():
    ledger = grounding.NumericGroundingLedger("Read the maintained Nursing Mastery funnel.")
    ledger.observe_tool_result(
        "mcp__posthog__exec",
        """<untrusted_tool_result source="mcp__posthog__exec">
External source data follows.

{"result": "Date range: 2026-07-25 00:00:00 to 2026-08-24 23:59:59 (UTC)\\n\\nMetric|funnel_search_performed|funnel_job_viewed|funnel_profile_milestone_reached|funnel_application_submitted\\nTotal person count|975|37|6|2\\nConversion rate|100%|3.79%|0.62%|0.21%\\nDropoff rate|0%|96.21%|99.38%|99.79%"}
</untrusted_tool_result>""",
    )

    verdict = ledger.validate(
        """## Funnel performance
job search (funnel_search_performed): 975 people, 100% conversion, 0% dropoff
job detail (funnel_job_viewed): 37 people, 3.79% conversion, 96.21% dropoff
apply start (funnel_profile_milestone_reached): 6 people, 0.62% conversion, 99.38% dropoff
application received (funnel_application_submitted): 2 people, 0.21% conversion, 99.79% dropoff
"""
    )

    assert verdict.ok is True
    assert verdict.checked_claims == 12
    assert verdict.grounded_claims == 12

    exact_live_rendering = ledger.validate(
        """RN7CyKkR, 2026-07-25 00:00:00 to 2026-08-24 23:59:59 UTC

funnel_search_performed — 975 people, conversion 100%, drop-off 0%
funnel_job_viewed — 37 people, conversion 3.79%, drop-off 96.21%
funnel_profile_milestone_reached — 6 people, conversion 0.62%, drop-off 99.38%
funnel_application_submitted — 2 people, conversion 0.21%, drop-off 99.79%
"""
    )
    assert exact_live_rendering.ok is True
    assert exact_live_rendering.checked_claims == 12
    assert exact_live_rendering.grounded_claims == 12

    shorthand = ledger.validate(
        "Funnel: search 975 → detail 37 → apply 6 → received 2."
    )
    assert shorthand.ok is True
    assert shorthand.grounded_claims == 4


def test_equal_value_from_unrelated_text_table_column_stays_rejected():
    ledger = grounding.NumericGroundingLedger("Read the maintained funnel.")
    ledger.observe_tool_result(
        "analytics",
        "Metric|clicks|revenue\nTotal count|168|200",
    )

    assert ledger.validate("Search count: 168").ok is False
    assert ledger.validate("Click count: 168").ok is True


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


def _nm_readout_ledger():
    """Relevant source fields from saved run df06b0b7 (2026-09-06).

    No live provider call: preserve the real human questions and machine keys,
    nested K2 result encoding, measured zeroes, and unreadable 28-day values.
    Daily series/transport metadata are intentionally not numeric evidence here.
    """
    ledger = grounding.NumericGroundingLedger("Read the current NM analytics readout.")
    for days, people, previous, walks, emails, excluded in (
        (7, 342, 109, 27, 6, 466),
        (28, 626, 170, None, None, 11188),
    ):
        rows = [
            {"key": "humans", "question": "How many nurses were on the site?",
             "value": people, "previous": previous, "excluded": excluded},
            {"key": "walk_started", "question": "How many answered an opening question?",
             "value": walks, "previous": 13 if days == 7 else None},
            {"key": "email_given", "question": "How many gave us an email?",
             "value": emails, "previous": 4 if days == 7 else None},
            {"key": "applications", "question": "How many nurse applications did we receive?",
             "value": 0, "previous": 0, "control": {"days": 90, "value": 0}},
        ]
        for row in rows:
            row["window"] = {"days": days}
            row["state"] = "measured" if row["value"] is not None else "unreadable"
        payload = {"result": json.dumps({"output": {"readouts": rows}})}
        ledger.observe_tool_result(
            "mcp__katailyst2__tool_execute",
            '<untrusted_tool_result source="mcp__katailyst2__tool_execute">\n'
            + json.dumps(payload) + "\n</untrusted_tool_result>",
        )
    return ledger


def test_real_nm_answer_reconciles_human_labels_and_backward_reference():
    ledger = _nm_readout_ledger()
    verdict = ledger.validate(
        """Alec — current NM readout (generated 2026-09-06). These are four separate counts, not a conversion funnel.

Metric | 7d | 28d
Site people (browser) | 342 | 626
Opening-question respondents (server) | 27 | unreadable
Email identities (server) | 6 | unreadable
Recorded applications (Vault) | 0 | 0

Windows: 7d 2026-08-30→09-06; 28d 2026-08-09→09-06. Site people previous: 109 / 170. Walk previous 13; email previous 4. Applications previous 0; 90-day control also 0.

Do not divide these. Browser people, server identities, and Vault application IDs barely overlap. 7d also excluded 466 one-page/analytics-declined browsers (28d excluded 11,188). Walks measured since 2026-08-18.

Bottleneck: Vault recorded 0 applications in 7d, 28d, and 90d. Traffic and walks moved; applications did not. 28d walks/emails are an evidence gap (PostHog 504 timeout), not a substitute number.

Next: inspect the Applying view for Apply presses vs Vault QA exclusion — the 0 is the product fact, not a readout miss.

Receipts: tool:nm-analytics-readout audits 884286fb-091d-4f34-8277-4ed121476fa2 (7d), 6b377efc-3977-4d32-b9c0-b451d0d6dbf9 (28d).
"""
    )
    assert verdict.ok, verdict.as_dict()
    assert verdict.referenced_claims == 1
    assert verdict.as_dict()["referenced_claims"] == 1


def test_human_question_is_a_source_label_not_a_product_alias():
    ledger = grounding.NumericGroundingLedger("Read the metrics.")
    ledger.observe_tool_result(
        "analytics", {"key": "m4", "question": "How many renewals completed?", "value": 23}
    )
    assert ledger.validate("Renewals count: 23").ok
    assert not ledger.validate("Application count: 23").ok


def test_second_table_value_keeps_its_row_metric_label():
    ledger = grounding.NumericGroundingLedger("Read metrics.")
    ledger.observe_tool_result("analytics", [{"metric": "search", "value": 12},
                                             {"metric": "clicks", "value": 168}])
    for row in ("| Search count | 12 | 168 |", "Search count | 12 | 168"):
        verdict = ledger.validate(row)
        assert not verdict.ok
        assert [claim.value for claim in verdict.unsupported] == ["168"]


def test_backward_reference_requires_an_earlier_reconciled_claim():
    ledger = grounding.NumericGroundingLedger("Read the metrics.")
    ledger.observe_tool_result("analytics", {"applications": 0})
    for prefix in ("", "Future target: 0 applications.\n",
                   "Unsupported claim: 0 applications.\n"):
        verdict = ledger.validate(prefix + "Inspect the Applying view — the 0 is the product fact.")
        assert not verdict.ok
        assert verdict.referenced_claims == 0
    assert ledger.validate("Applications: 0.\nInspect the Applying view — the 0 is the product fact.").ok


def test_reference_cannot_rename_a_metric_or_invent_a_value():
    ledger = grounding.NumericGroundingLedger("Read metrics.")
    ledger.observe_tool_result("analytics", {"clicks": 0})
    for ending in ("the 0 applications are recorded.",
                   "the 0 is our application count.",
                   "the 99 is the product fact."):
        verdict = ledger.validate("Click count: 0.\nInspect the Applying view — " + ending)
        assert not verdict.ok
        assert verdict.referenced_claims == 0


def test_nm_apply_presses_are_not_applications_and_invented_counts_still_fail():
    ledger = _nm_readout_ledger()
    for answer in ("Apply presses count: 0", "Site people count: 343"):
        assert not ledger.validate(answer).ok


def test_failure_explains_value_or_label_mismatch_without_echoing_prose():
    ledger = grounding.NumericGroundingLedger("Read metrics.")
    ledger.observe_tool_result("analytics", {"clicks": 168})
    verdict = ledger.validate("Search count: 168")
    assert "values and labels" in verdict.failure_message()
    assert "Search count" not in verdict.failure_message()


def test_http_status_in_metric_paragraph_is_not_business_evidence():
    ledger = grounding.NumericGroundingLedger("Read metrics.")
    verdict = ledger.validate("Application count unreadable (HTTP 504; PostHog 504 timeout).")
    assert verdict.ok
    assert verdict.checked_claims == 0
    assert not ledger.validate("Application count: 504").ok
