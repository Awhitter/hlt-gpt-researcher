#!/usr/bin/env python3
"""Retire scheduled jobs that talk too much; retain legacy seed helpers.

Retirement is not a reason to remove scheduling capability. Cleo retains the
practical cron workbench; the execution-time effect policy governs actual
unattended work. Native delivery owns scheduled final messages.

Jobs are created through `hermes cron create` rather than by writing
`cron/jobs.json` directly: the CLI owns the record shape (ids, next-run
computation, schedule parsing), and hand-building that file is how you get a job
that looks present and never fires.

Boot no longer calls ``seed``. It inventories and pauses every job named in
``RETIRED_JOB_NAMES`` through ``retire_stale_briefs`` after banking a durable
recovery export. The older seed helpers remain only for explicit operator
recovery and their pinned regression tests; an existing job is still left
exactly as it is.

All three fleet checks installed by ``fleet_durability.py`` are retired here
rather than deleted there. That module still owns their definitions and still
upserts them on every boot, but it never writes ``enabled``, so a job paused
here stays paused across deploys and comes back with a single
``hermes cron resume <job-id>``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("hlt-agent")

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
    {
        "name": "nm-product-owner-work",
        # A single midweek work block adds initiative without turning the home
        # channel into a stream of daily status noise.
        "schedule": "0 14 * * 3",
        "skill": "facilitate-product-work",
        "prompt": (
            "Do one useful Nursing Mastery product-owner work block, not a status "
            "update. Load your current Cleo context from K2, inspect current product "
            "truth and open work, choose one bounded high-leverage question you can "
            "advance safely, and actually advance it. Return the decision, comparison, "
            "draft, visual, or hosted artifact the team can use, with concise sources. "
            "Do not publish, send a campaign, file issues, or change production. If "
            "there is no honest useful move, say that briefly instead of padding."
        ),
    },
)
LEGACY_BRIEF_NAMES = frozenset(brief["name"] for brief in BRIEFS)

# All three `fleet_durability.py` checks deliver straight into #agent-logs
# (`slack:C0BH5997USK`) on a clock rather than on an event, and the owner named
# that noise on 2026-09-07: a condition should speak once when it starts, once
# when it ends, and at most once a day while it persists. "i want the agents to
# work but never ever spam like this again."
#
#   hlt-fleet-daily-canary-cleo-v1 — wakes a real agent every day and delivers
#     the entire model reply, wrapped in Hermes' own "Cronjob Response: … To
#     stop or manage this job" template. It reports the same unchanged
#     agent:cleo record daily. That is a bot narrating its own schedule.
#   hlt-fleet-readiness-cleo-v1 — runs every five minutes. It is quiet while
#     the signature holds, but a flapping check or a pending delivery retry
#     re-posts on the five-minute tick, with no daily ceiling.
#   hlt-fleet-release-cleo-v1 — posts the same wrapped "Cronjob Response: … To
#     stop or manage this job" block into #agent-logs on its daily 15:20 UTC
#     tick. What it watches (the upstream Hermes stable release) is already
#     watched by openclaw-hq's own upstream release watch, which now speaks at
#     most once a week. The fleet does not need a second, daily one saying the
#     same thing.
RETIRED_FLEET_JOB_NAMES = frozenset(
    {
        "hlt-fleet-daily-canary-cleo-v1",
        "hlt-fleet-readiness-cleo-v1",
        "hlt-fleet-release-cleo-v1",
    }
)
RETIRED_JOB_NAMES = LEGACY_BRIEF_NAMES | RETIRED_FLEET_JOB_NAMES

# Filename kept as-is: this file already exists on the live disk holding the
# three product briefs, and renaming it would strand that recovery point. It
# banks every retired job now, not only the briefs.
LEGACY_EXPORT_NAME = "nm-legacy-briefs-before-retirement.json"
EXPORT_VERSION = "hlt.legacy_cron_export.v1"


def _jobs_from_payload(payload: object) -> list[dict[str, object]]:
    """Normalize the cron store shapes supported by pinned Hermes."""
    raw = payload if isinstance(payload, list) else (
        payload.get("jobs", []) if isinstance(payload, dict) else []
    )
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return []
    return [job for job in raw if isinstance(job, dict)]


def _job_id(job: dict[str, object]) -> str:
    return str(job.get("id") or job.get("job_id") or "")


def retire_stale_briefs(dry_run: bool = False) -> dict[str, Any]:
    """Export and pause every ``RETIRED_JOB_NAMES`` job without deleting history.

    The pre-retirement records are banked on the durable disk. We then ask
    Hermes to pause each live job by its exact id, preserving its run history
    and making recovery a simple ``hermes cron resume <id>``. If the record
    cannot be read or exported, nothing is paused: reversible means the backup
    exists before the state change.

    The bank grows with the retired set. An earlier export covering only the
    three product briefs is a valid recovery point for those briefs and must
    not block retiring anything added since — that is how this whole function
    becomes a silent no-op the day the set changes. Previously banked records
    are never rewritten (they are the pre-retirement snapshot); newly retired
    jobs are appended. Only a file we cannot read, or one that is not ours,
    still blocks: we do not overwrite a recovery point we cannot verify.
    """
    result: dict[str, Any] = {
        "policy": "retired",
        "inventory": [],
        "paused": [],
        "already_paused": [],
        "not_found": [],
        "failed": [],
        "export_path": "",
    }
    home = Path(os.environ.get("HERMES_HOME") or "/data/hermes")
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.exists():
        result["not_found"] = sorted(RETIRED_JOB_NAMES)
        return result
    try:
        payload = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cron jobs.json unreadable (%s); retired jobs left untouched", exc)
        result["failed"] = ["read-jobs-file"]
        return result

    retired = [
        job for job in _jobs_from_payload(payload)
        if str(job.get("name") or "") in RETIRED_JOB_NAMES
    ]
    result["inventory"] = [
        {
            "id": _job_id(job),
            "name": str(job.get("name") or ""),
            "enabled": bool(job.get("enabled", True)),
            "state": str(job.get("state") or ""),
        }
        for job in retired
    ]
    found_names = {str(job.get("name") or "") for job in retired}
    result["not_found"] = sorted(RETIRED_JOB_NAMES - found_names)
    if not retired:
        return result

    export_dir = home / "cron" / "retired"
    export_path = export_dir / LEGACY_EXPORT_NAME
    result["export_path"] = str(export_path)
    if not dry_run:
        banked: list[dict[str, object]] = []
        if export_path.exists():
            try:
                previous_export = json.loads(export_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(previous_export, dict)
                    or previous_export.get("version") != EXPORT_VERSION
                ):
                    raise ValueError("not an HLT retired-cron export")
                banked = _jobs_from_payload(previous_export)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "retired cron export is not a valid recovery point (%s); "
                    "jobs left untouched",
                    exc,
                )
                result["failed"] = ["read-export"]
                return result

        banked_ids = {_job_id(job) for job in banked}
        newly_retired = [job for job in retired if _job_id(job) not in banked_ids]
        if newly_retired:
            try:
                export_dir.mkdir(parents=True, exist_ok=True)
                temp_path = export_path.with_suffix(".tmp")
                temp_path.write_text(
                    json.dumps(
                        {
                            "version": EXPORT_VERSION,
                            "exported_at": datetime.now(UTC).isoformat(),
                            "source": str(jobs_file),
                            "jobs": banked + newly_retired,
                            "restore": "hermes cron resume <job-id>",
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temp_path, export_path)
            except OSError as exc:
                logger.warning("retired cron export failed (%s); jobs left untouched", exc)
                result["failed"] = ["write-export"]
                return result

    for job in retired:
        name = str(job.get("name") or "")
        job_id = _job_id(job)
        state = str(job.get("state") or "").lower()
        if not bool(job.get("enabled", True)) or state in {"paused", "disabled"}:
            result["already_paused"].append(name)
            continue
        if not job_id:
            result["failed"].append(f"missing-id:{name}")
            continue
        if dry_run:
            result["paused"].append(name)
            continue
        cmd = ["hermes", "cron", "pause", job_id]
        try:
            done = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("cron pause %s failed: %s", name, exc)
            result["failed"].append(name)
            continue
        if done.returncode == 0:
            result["paused"].append(name)
        else:
            logger.warning(
                "cron pause %s exited %s: %s",
                name,
                done.returncode,
                (done.stderr or done.stdout)[:300],
            )
            result["failed"].append(name)
    return result


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
