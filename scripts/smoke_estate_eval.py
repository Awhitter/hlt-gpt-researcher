#!/usr/bin/env python3
"""Estate smoke-eval: canned questions with scope AND content assertions.

Where smoke_websocket_ui.py proves the wire (token → WebSocket → first scope
event), this script proves the *answers*: each canned question runs to a
finished report and is checked two ways —

  (a) scope: the auto-scope router activated (or stayed out of) the expected
      internal scopes, asserted at the ``hlt_scope_status`` event;
  (b) content: the report matches a content-signal regex and avoids the known
      failure frames (e.g. "what is nursing mastery" must read as HLT's
      recruiting platform, never as an NCLEX-app comparison vs Kaplan/UWorld/
      ATI — the exact regression the estate glossary fixed).

Usage:
    # Against prod (the UI sits behind the shared-password gate):
    TEAM_ACCESS_PASSWORD=... python scripts/smoke_estate_eval.py

    # Against a local stack:
    python scripts/smoke_estate_eval.py \
        --base-url http://localhost:3000 --api-url http://localhost:8000

    # One case, or just look at the roster:
    python scripts/smoke_estate_eval.py --only nursing-mastery-identity
    python scripts/smoke_estate_eval.py --list

Auth passthrough: TEAM_ACCESS_PASSWORD (env) logs in through the team gate
before fetching the ws-token; unset means the gate is off (local dev).

Exit code = number of failed cases, so any miss fails CI/cron.
Runs default to depth=fast to keep a full sweep cheap (~7 short runs).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field

import websockets

from smoke_websocket_ui import fetch_ws_token


@dataclass
class EvalCase:
    id: str
    query: str
    # Scope assertions (evaluated at the hlt_scope_status event).
    expect_active: set[str] = field(default_factory=set)  # must be ⊆ active_sources
    expect_empty_active: bool = False  # no internal scope may activate
    # Content assertions (evaluated on the finished report).
    must_match: str | None = None  # case-insensitive search anywhere
    must_not_match: str | None = None  # case-insensitive search anywhere
    must_not_match_head: str | None = None  # only the first HEAD_CHARS (the frame)
    require_immutable_code_sources: bool = False
    note: str = ""


HEAD_CHARS = 2000

CASES: list[EvalCase] = [
    EvalCase(
        id="nursing-mastery-identity",
        query="what is nursing mastery",
        must_match=r"recruiting|nursingmastery\.com|job",
        must_not_match=r"\b(Kaplan|UWorld|ATI)\b",
        note="The glossary regression: must read as HLT's recruiting platform, "
        "never an NCLEX-prep-app comparison.",
    ),
    EvalCase(
        id="glossary-competitor-frame",
        query="how does nursing mastery compare to competitors",
        must_match=r"recruiting|job board|nursingmastery\.com|hiring",
        must_not_match_head=r"\b(Kaplan|UWorld|ATI)\b",
        note="Competitor frame must be recruiting platforms; test-prep brands in "
        "the opening frame means the planner re-derived identity from the web.",
    ),
    EvalCase(
        id="katailyst-registry-scope",
        query="What does the Katailyst registry hold and how do agents use it?",
        expect_active={"cms"},
        must_match=r"registry|skill|workflow|knowledge",
        note="A Katailyst question must activate the registry (cms) scope.",
    ),
    EvalCase(
        id="scopeless-pizza",
        query="best pizza in Chicago",
        expect_empty_active=True,
        must_match=r"pizza|deep.?dish|Chicago",
        must_not_match=r"nurs|\bHLT\b",
        note="Generic questions stay pure web research: no internal scope, no "
        "estate bleed-through.",
    ),
    EvalCase(
        id="codebase-handoff",
        query="How does ScraperVault hand enriched jobs to nursing-mastery?",
        expect_active={"codebase"},
        must_match=r"ScraperVault|nursing-mastery",
        note="Repo-shaped questions must activate the codebase scope.",
    ),
    EvalCase(
        id="nurse-profile-attributes",
        query="What attributes do we capture for a nurse?",
        expect_active={"codebase"},
        must_match=r"(attribute|field|profile|captur|stor)",
        require_immutable_code_sources=True,
        note="Natural profile language must route to code and cite exact implementation sources.",
    ),
    EvalCase(
        id="email-capture-timing",
        query="When do we capture email?",
        expect_active={"codebase"},
        must_match=r"email",
        require_immutable_code_sources=True,
        note="Email timing must be traced through the implementation, not inferred from web pages.",
    ),
    EvalCase(
        id="job-search-implementation",
        query="How does job search work?",
        expect_active={"codebase"},
        must_match=r"(search|filter|query|feed)",
        must_not_match=r"jobs_enrichment\.py|/api/internal/jobs/import|BullMQ",
        require_immutable_code_sources=True,
        note="Search must distinguish Nursing Mastery consumer behavior from ScraperVault authority.",
    ),
    EvalCase(
        id="onboarding-questions-change",
        query="What onboarding questions do we ask, and how can they be changed?",
        expect_active={"codebase"},
        must_match=r"(onboarding|question|field|form)",
        require_immutable_code_sources=True,
        note="Change guidance must name current source locations without implying a direct edit.",
    ),
    EvalCase(
        id="marketo-email-readback",
        query="Do we store emails in Marketo?",
        expect_active={"codebase"},
        must_match=r"Marketo",
        must_not_match=r"definitely|certainly|guaranteed",
        require_immutable_code_sources=True,
        note="Code evidence may describe a handoff, but missing Marketo readback must remain unavailable.",
    ),
    EvalCase(
        id="audience-voice",
        query="What do new grad nurses complain about most in their first year?",
        must_match=r"burnout|staffing|ratio|preceptor|orientation|pay|bullying",
        note="Audience question; scope activation depends on corpus readiness, "
        "so only the content signal is asserted.",
    ),
    EvalCase(
        id="pay-check-tool",
        query="What is Nurse Pay Check on nursingmastery.com?",
        must_match=r"pay|salary|compensation",
        must_not_match=r"\b(Kaplan|UWorld|ATI)\b",
        note="Estate product question answered from our own surface, not "
        "test-prep chatter.",
    ),
]


class CaseFailure(Exception):
    pass


async def run_case(case: EvalCase, args: argparse.Namespace, token: str) -> None:
    ws_url = args.api_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    payload = {
        "task": case.query,
        "report_type": "research_report",
        "report_source": "web",
        "tone": "Objective",
        "query_domains": [],
        "mcp_enabled": False,
        "mcp_strategy": "fast",
        "mcp_configs": [],
        "hlt_research_scope": {"auto": True, "depth": args.depth},
    }

    scope_checked = False
    report_chunks: list[str] = []
    final_report: str | None = None
    deadline = time.monotonic() + args.case_timeout

    async with websockets.connect(f"{ws_url}/ws?ws_token={token}") as websocket:
        await websocket.send("start " + json.dumps(payload))
        while time.monotonic() < deadline:
            remaining = max(1.0, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(
                    websocket.recv(), timeout=min(args.timeout, remaining)
                )
            except asyncio.TimeoutError as error:
                raise CaseFailure(
                    f"no stream event within {args.timeout}s "
                    f"(scope_checked={scope_checked}, report_chars={len(''.join(report_chunks))})"
                ) from error
            except websockets.ConnectionClosed:
                break

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            content = event.get("content")

            if event_type == "logs" and content == "error":
                raise CaseFailure(f"backend error event: {str(event.get('output'))[:400]}")

            if event_type == "logs" and content == "hlt_scope_status":
                metadata = event.get("metadata") or {}
                hlt_scope = metadata.get("hlt_research_scope") or {}
                active = set(hlt_scope.get("active_sources") or [])
                print(f"  scope: active={sorted(active) or 'none'}")
                if case.expect_empty_active and active:
                    raise CaseFailure(f"expected no active scopes, got {sorted(active)}")
                if case.expect_active and not case.expect_active.issubset(active):
                    raise CaseFailure(
                        f"expected active scopes to include {sorted(case.expect_active)}, "
                        f"got {sorted(active)}"
                    )
                scope_checked = True

            if event_type == "report":
                report_chunks.append(str(event.get("output") or ""))
            if event_type == "report_complete":
                final_report = str(event.get("output") or "")
                break
            if event_type == "path":
                # End of run; report_complete usually precedes this.
                break

    report = final_report if final_report is not None else "".join(report_chunks)
    if not scope_checked and (case.expect_active or case.expect_empty_active):
        raise CaseFailure("run finished without an hlt_scope_status event")
    if not report.strip():
        raise CaseFailure("run finished without report content")

    print(f"  report: {len(report)} chars")
    if case.must_match and not re.search(case.must_match, report, re.IGNORECASE):
        raise CaseFailure(f"report missing content signal /{case.must_match}/i")
    if case.must_not_match and (hit := re.search(case.must_not_match, report, re.IGNORECASE)):
        raise CaseFailure(f"report matched forbidden /{case.must_not_match}/i: {hit.group(0)!r}")
    if case.must_not_match_head and (
        hit := re.search(case.must_not_match_head, report[:HEAD_CHARS], re.IGNORECASE)
    ):
        raise CaseFailure(
            f"report frame (first {HEAD_CHARS} chars) matched forbidden "
            f"/{case.must_not_match_head}/i: {hit.group(0)!r}"
        )
    if case.require_immutable_code_sources:
        refs = re.findall(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/[0-9a-fA-F]{40}/[^\s)#?]+",
            report,
        )
        if not refs:
            raise CaseFailure("report has no immutable GitHub source at an exact commit")


async def run_all(args: argparse.Namespace) -> int:
    cases = [c for c in CASES if not args.only or c.id in args.only]
    if not cases:
        print(f"no cases match --only {args.only}", file=sys.stderr)
        return 1

    failures = 0
    for case in cases:
        print(f"[{case.id}] {case.query!r}")
        try:
            # A fresh short-lived token per case keeps long sweeps valid.
            token = fetch_ws_token(args.base_url)
            await run_case(case, args, token)
            print("  PASS")
        except CaseFailure as error:
            failures += 1
            print(f"  FAIL: {error}")
        except Exception as error:  # noqa: BLE001 — a broken wire is a failed case
            failures += 1
            print(f"  FAIL (transport): {type(error).__name__}: {error}")

    print(f"\n{len(cases) - failures}/{len(cases)} cases passed")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base-url",
        default="https://gpt-researcher-ui.vercel.app",
        help="UI base URL (issues ws tokens; sits behind the team gate in prod)",
    )
    parser.add_argument(
        "--api-url",
        default="https://gpt-researcher-api-production.up.railway.app",
        help="Backend API base URL (hosts the research WebSocket)",
    )
    parser.add_argument("--depth", choices=["fast", "balanced", "deep"], default="fast")
    parser.add_argument("--only", nargs="*", default=None, help="Run only these case ids")
    parser.add_argument("--list", action="store_true", help="List cases and exit")
    parser.add_argument("--timeout", type=int, default=120, help="Per-event timeout (s)")
    parser.add_argument("--case-timeout", type=int, default=600, help="Per-case wall clock (s)")
    args = parser.parse_args()

    if args.list:
        for case in CASES:
            scope = (
                "no-scope" if case.expect_empty_active
                else ",".join(sorted(case.expect_active)) or "any"
            )
            print(f"{case.id:28s} scope={scope:12s} {case.query}")
        return

    sys.exit(asyncio.run(run_all(args)))


if __name__ == "__main__":
    main()
