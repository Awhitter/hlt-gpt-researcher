#!/usr/bin/env python3
"""Seed the agent's recurring briefs at boot, idempotently.

Why this exists rather than granting her the `cronjob` tool: a scheduled run
happens with nobody watching, and the tool would let anyone who can @mention her
leave something running. Upstream is explicit that cron delivery does not need
the tool at all — "the agent's final response is automatically delivered to the
configured `deliver:` target, the agent does not send messages itself" — so the
schedule stays operator-owned and her toolset stays read-of-record.

Jobs are created through `hermes cron create` rather than by writing
`cron/jobs.json` directly: the CLI owns the record shape (ids, next-run
computation, schedule parsing), and hand-building that file is how you get a job
that looks present and never fires.

Idempotent by name — an existing job is left exactly as it is, including any
edit an operator has made to it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("brian")

# A Slack conversation id: C/D/G (channel, DM, group) then upper alphanumerics.
# The command below is an argv list with no shell, so this is not an injection
# fix — it is a typo fix. A malformed SLACK_HOME_CHANNEL would otherwise be
# baked into a job that fails to deliver, silently, every week.
DELIVER_TARGET = re.compile(r"^slack:[CDG][A-Z0-9]{6,}$")

# Each brief loads the matching skill, so the procedure lives in one place and a
# change to the skill changes the brief too.
BRIEFS: tuple[dict[str, str], ...] = (
    {
        "name": "nm-monday-brief",
        # 13:00 UTC Monday — before the US working day, after any weekend merges.
        "schedule": "0 13 * * 1",
        "skill": "weekly-brief",
        "prompt": (
            "Write this week's Nursing Mastery brief for the team. Follow the "
            "weekly-brief skill exactly: what shipped, what is in flight, what is "
            "stuck. Rank by consequence, not by what is most visible. Plain "
            "language for people who were not in the code, and cite the real "
            "identifiers. If nothing meaningful moved, say that in one line "
            "rather than padding."
        ),
    },
    {
        "name": "nm-board-health",
        "schedule": "0 14 * * 5",
        "skill": "weekly-brief",
        "prompt": (
            "Report the health of the NUR board for repo:nursing-mastery only: "
            "how many open issues have no project, no priority, and no assignee, "
            "and which of those look important enough that somebody should own "
            "them. Do not file or change anything — this is a read-only report."
        ),
    },
)


def seed(deliver: str, dry_run: bool = False) -> dict[str, list[str]]:
    """Create any brief that does not already exist. Returns what happened."""
    result: dict[str, list[str]] = {"created": [], "existing": [], "failed": []}
    if not deliver:
        return result
    if not DELIVER_TARGET.match(deliver):
        logger.error(
            "refusing to seed briefs: %r is not a Slack conversation id. Check "
            "SLACK_HOME_CHANNEL — it must be the id, optionally '<id>|#name'.",
            deliver,
        )
        result["failed"].append("bad-deliver-target")
        return result

    # Existence is checked by reading the record, NOT by shelling out to
    # `hermes cron list`: that subcommand has no --json flag, so parsing its
    # human output is guesswork and a wrong guess seeds a duplicate brief.
    jobs_file = Path(os.environ.get("HERMES_HOME") or "/data/hermes") / "cron" / "jobs.json"
    existing_names: set[str] = set()
    if jobs_file.exists():
        try:
            payload = json.loads(jobs_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # An unreadable record is not proof the jobs are absent, and a
            # duplicate brief means the team gets the same message twice.
            logger.warning("cron jobs.json unreadable (%s); not seeding", exc)
            result["failed"].append("read-jobs-file")
            return result
        jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
        if isinstance(jobs, dict):  # keyed by id in some versions
            jobs = list(jobs.values())
        existing_names = {
            str(j.get("name") or "") for j in jobs if isinstance(j, dict)
        }

    for brief in BRIEFS:
        if brief["name"] in existing_names:
            result["existing"].append(brief["name"])
            continue
        cmd = [
            "hermes", "cron", "create", brief["schedule"], brief["prompt"],
            "--name", brief["name"], "--deliver", deliver, "--skill", brief["skill"],
        ]
        if dry_run:
            result["created"].append(brief["name"])
            continue
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("cron create %s failed: %s", brief["name"], exc)
            result["failed"].append(brief["name"])
            continue
        if done.returncode == 0:
            result["created"].append(brief["name"])
        else:
            logger.warning(
                "cron create %s exited %s: %s",
                brief["name"], done.returncode, (done.stderr or done.stdout)[:300],
            )
            result["failed"].append(brief["name"])

    return result


# A brief that has never run is not a working brief — that is the failure this
# whole service keeps hitting, most recently a lane that sat dark so long its
# agent had never fired once behind a healthy stat. So the first boot that can
# deliver also schedules a ONE-SHOT proof a few minutes out.
#
# `--repeat 1` makes it a finite one-shot: the scheduler auto-deletes the job
# once the repeat limit is reached. That auto-delete is also why this needs a
# sentinel — the job vanishes from jobs.json, so the name-based idempotency
# above would happily re-create it on the next deploy and the channel would get
# a smoke message every merge.
#
# The prompt asks for real identifiers on purpose. "Did cron fire" is the easy
# half; the half that actually breaks is whether a CRON session — a standalone
# agent on the scheduler's own thread pool, outside the gateway's dispatch — has
# her MCP tools at all. An answer with a NUR number proves both.
SMOKE_NAME = "nm-brief-smoke"
SMOKE_SENTINEL = ".brief-smoke-seeded"
SMOKE_PROMPT = (
    "This is a one-off check that scheduled briefs work end to end — say so in "
    "your first line. Then, in no more than four lines, name one thing that "
    "moved in Nursing Mastery in the last week and one open NUR issue, each "
    "with its real identifier, so we can see you can reach your sources on a "
    "schedule and not just in chat. If you cannot reach a source, say which one "
    "and stop — that result is more useful than a guess."
)


def seed_smoke(deliver: str, dry_run: bool = False) -> str:
    """Schedule the one-shot proof. Returns what happened, for /health."""
    if not deliver or not DELIVER_TARGET.match(deliver):
        return "skipped-no-target"

    home = Path(os.environ.get("HERMES_HOME") or "/data/hermes")
    sentinel = home / SMOKE_SENTINEL
    if sentinel.exists():
        return "already-run"

    cmd = [
        "hermes", "cron", "create", "5m", SMOKE_PROMPT,
        "--name", SMOKE_NAME, "--deliver", deliver,
        "--skill", "weekly-brief", "--repeat", "1",
    ]
    if dry_run:
        return "would-create"
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("smoke brief failed: %s", exc)
        return "failed"
    if done.returncode != 0:
        logger.warning(
            "smoke brief exited %s: %s", done.returncode, (done.stderr or done.stdout)[:300]
        )
        return "failed"

    # Written only after a successful create, so a failed attempt retries on the
    # next boot instead of marking itself done.
    try:
        sentinel.write_text("seeded\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("smoke brief ran but the sentinel could not be written: %s", exc)
        return "created-unsentinelled"
    return "created"
