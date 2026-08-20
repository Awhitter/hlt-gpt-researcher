#!/usr/bin/env python3
"""Retire the old recurring briefs safely; retain legacy seed helpers.

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

Boot no longer calls ``seed``. It inventories and pauses the three legacy jobs
through ``retire_stale_briefs`` after writing a durable recovery export. The
older seed helpers remain only for explicit operator recovery and their pinned
regression tests; an existing job is still left exactly as it is.
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
LEGACY_EXPORT_NAME = "nm-legacy-briefs-before-retirement.json"


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


def retire_stale_briefs(dry_run: bool = False) -> dict[str, Any]:
    """Export and pause the three superseded briefs without deleting history.

    The pre-retirement records are written once on the durable disk. We then
    ask Hermes to pause each live job by its exact id, preserving its run
    history and making recovery a simple ``hermes cron resume <id>``. If the
    record cannot be read or exported, nothing is paused: reversible means the
    backup exists before the state change.
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
        result["not_found"] = sorted(LEGACY_BRIEF_NAMES)
        return result
    try:
        payload = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cron jobs.json unreadable (%s); legacy briefs left untouched", exc)
        result["failed"] = ["read-jobs-file"]
        return result

    legacy = [
        job for job in _jobs_from_payload(payload)
        if str(job.get("name") or "") in LEGACY_BRIEF_NAMES
    ]
    result["inventory"] = [
        {
            "id": str(job.get("id") or job.get("job_id") or ""),
            "name": str(job.get("name") or ""),
            "enabled": bool(job.get("enabled", True)),
            "state": str(job.get("state") or ""),
        }
        for job in legacy
    ]
    found_names = {str(job.get("name") or "") for job in legacy}
    result["not_found"] = sorted(LEGACY_BRIEF_NAMES - found_names)
    if not legacy:
        return result

    export_dir = home / "cron" / "retired"
    export_path = export_dir / LEGACY_EXPORT_NAME
    result["export_path"] = str(export_path)
    if not dry_run and export_path.exists():
        try:
            previous_export = json.loads(export_path.read_text(encoding="utf-8"))
            exported_ids = {
                str(job.get("id") or job.get("job_id") or "")
                for job in _jobs_from_payload(previous_export)
            }
            current_ids = {
                str(job.get("id") or job.get("job_id") or "") for job in legacy
            }
            if (
                not isinstance(previous_export, dict)
                or previous_export.get("version") != "hlt.legacy_cron_export.v1"
                or not current_ids.issubset(exported_ids)
            ):
                raise ValueError("existing export does not cover the current jobs")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "legacy brief export is not a valid recovery point (%s); jobs left untouched",
                exc,
            )
            result["failed"] = ["read-export"]
            return result
    elif not dry_run:
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            temp_path = export_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(
                    {
                        "version": "hlt.legacy_cron_export.v1",
                        "exported_at": datetime.now(UTC).isoformat(),
                        "source": str(jobs_file),
                        "jobs": legacy,
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
            logger.warning("legacy brief export failed (%s); jobs left untouched", exc)
            result["failed"] = ["write-export"]
            return result

    for job in legacy:
        name = str(job.get("name") or "")
        job_id = str(job.get("id") or job.get("job_id") or "")
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
