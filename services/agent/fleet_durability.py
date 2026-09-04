"""Cleo's inexpensive fleet observations, owned by the native Hermes scheduler.

Five-minute health and daily release checks never construct an agent. A red
observation is a completed check, not an execution error that triggers backoff.
Only changed findings are returned for native Slack delivery. No deployments,
credential writes, GitHub Actions, or automatic upgrades occur here.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

AGENT_LOGS = "slack:C0BH5997USK"
JOB_PREFIX = "hlt-fleet-"
RELEASE_API = "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest"


def _home() -> Path:
    return Path(os.getenv("HERMES_HOME", "/data/hermes"))


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={
        "Accept": "application/json", "User-Agent": "HLT-fleet-readiness/1",
    })
    with urlopen(request, timeout=8) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Observation response exceeded 1 MB")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Observation response was not an object")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _previous(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Fleet receipt must be an object")
    return payload


def observation(kind: str, fetch=fetch_json) -> dict[str, Any]:
    try:
        if kind == "readiness":
            health = fetch(f"http://127.0.0.1:{int(os.getenv('PORT', '8080'))}/health")
            readiness = health.get("readiness") or {}
            checks = readiness.get("checks") or {}
            failed = sorted(key for key, passed in checks.items() if passed is not True)
            if not checks:
                failed = ["readiness_receipt_missing"]
            return {
                "status": "green" if readiness.get("ready") is True and not failed else "degraded",
                "servingReady": readiness.get("servingReady") is True,
                "redundancyReady": readiness.get("redundancyReady") is True,
                "failedChecks": failed,
                "runtimeProof": readiness.get("runtimeProof") or {},
            }
        if kind == "release":
            release = fetch(RELEASE_API)
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not tag or release.get("draft") or release.get("prerelease"):
                raise ValueError("No stable release metadata")
            # Compare immutable commit identities, not mutable tag dates.
            ref = fetch("https://api.github.com/repos/NousResearch/hermes-agent/commits/" + tag)
            candidate = str(ref.get("sha") or "")
            installed = os.getenv("HERMES_UPSTREAM_REF", "")
            if len(candidate) != 40 or len(installed) != 40:
                raise ValueError("Missing exact Hermes release/deploy SHA")
            return {
                "status": "current" if candidate == installed else "candidate",
                "tag": tag, "installedSha": installed, "candidateSha": candidate,
                "url": "https://github.com/NousResearch/hermes-agent/releases/tag/" + tag,
            }
        raise ValueError("Unknown fleet check")
    except Exception as exc:
        # Exception class is enough for an alert. Never echo provider bodies,
        # full health documents, or credential-bearing configuration.
        return {"status": "unknown", "errorType": type(exc).__name__}


def check(kind: str, fetch=fetch_json) -> str:
    path = _home() / "fleet" / f"{kind}.json"
    previous = _previous(path)
    current = observation(kind, fetch)
    signature = {
        key: value for key, value in current.items()
        if key not in {"runtimeProof", "installedSha", "url"}
    }
    # Native Hermes records failed delivery separately. Retry the same finding
    # only when its preceding delivery failed, not every five-minute tick.
    jobs_path = _home() / "cron" / "jobs.json"
    jobs = _previous(jobs_path).get("jobs", []) if jobs_path.exists() else []
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    job = next((row for row in jobs if row.get("name") == f"{JOB_PREFIX}{kind}-cleo-v1"), {})
    retry_delivery = bool(job.get("last_delivery_error"))
    changed = previous.get("signature") != signature or retry_delivery
    candidate_seen = previous.get("offeredCandidateSha")
    repeated_candidate = (
        current.get("candidateSha") == candidate_seen
        and current.get("status") == "candidate"
    )
    _write(path, {
        "version": "hlt.fleet.observation.v1", "checkedAt": datetime.now(UTC).isoformat(),
        "kind": kind, "signature": signature, **current,
        "offeredCandidateSha": current.get("candidateSha", candidate_seen),
    })
    healthy = current["status"] in {"green", "current"}
    if not changed or (not previous and healthy):
        return ""
    if kind == "release" and current["status"] == "candidate":
        if repeated_candidate and not retry_delivery:
            return "Cleo: release check recovered; the previously reported update remains available."
        return f"Cleo: Hermes {current['tag']} is available for a reviewed update. No build or upgrade was triggered. {current['url']}"
    if healthy:
        return f"Cleo: {kind} check recovered."
    detail = ", ".join(current.get("failedChecks", [])) or current.get("errorType", "unknown")
    return f"Cleo: {kind} needs attention — {detail}."


def gate() -> str:
    receipt = _previous(_home() / "fleet" / "readiness.json")
    checked = receipt.get("checkedAt")
    age = (datetime.now(UTC) - datetime.fromisoformat(checked)).total_seconds() if checked else -1
    fresh = 0 <= age < 660
    return json.dumps({"wakeAgent": fresh and receipt.get("servingReady") is True})


def install() -> dict[str, Any]:
    """Upsert only these three jobs before the gateway starts; preserve history."""
    if os.getenv("HLT_AGENT_REF", "agent:cleo") != "agent:cleo" or os.getenv("AGENT_ID", "cleo") != "cleo":
        return {"installed": [], "failed": [], "skipped": "different-agent"}
    from cron.jobs import create_job, list_jobs, update_job
    from fleet_run_budget import CANARY_BUDGET

    scripts = _home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    definitions = (
        ("readiness", "*/5 * * * *", "check('readiness')"),
        ("release", "20 15 * * *", "check('release')"),
        ("daily-canary", "45 14 * * *", "gate()"),
    )
    existing = list_jobs(include_disabled=True)
    result: dict[str, Any] = {
        "installed": [], "failed": [], "target": AGENT_LOGS,
        "canaryRoute": {
            "provider": "xai-oauth", "model": "grok-4.6",
            "verifies": "K2 and authenticated backup; ordinary Slack acceptance verifies Sol/high",
            "reason": "Codex subscription wire rejects output caps; only this scheduled check uses Grok",
        },
    }
    for kind, schedule, expression in definitions:
        name = f"{JOB_PREFIX}{kind}-cleo-v1"
        script = f"{name}.py"
        (scripts / script).write_text(
            "import sys\nsys.path.insert(0, '/app')\n"
            "from fleet_durability import check, gate\n"
            f"message = {expression}\nif message: print(message)\n", encoding="utf-8",
        )
        canary = kind == "daily-canary"
        fields: dict[str, Any] = {
            "name": name, "schedule": schedule, "script": script,
            "no_agent": not canary, "deliver": AGENT_LOGS,
            "prompt": (
                "Read your current canonical agent:cleo record through Katailyst2. "
                "Return your name, current role and version with one source link, "
                "in no more than three short lines. This is the daily read-only fleet "
                "check: use registry_get only, no delegated workers, writes or sends."
            ) if canary else "",
        }
        if canary:
            import grounding

            fields.update({
                "model": "grok-4.6", "provider": "xai-oauth",
                "reasoning_effort": "high", "enabled_toolsets": ["mcp-katailyst2"],
                "workdir": str(grounding.grounding_dir(_home())),
            })
        matches = [job for job in existing if job.get("name") == name]
        if len(matches) > 1:
            result["failed"].append(f"duplicate:{name}")
            continue
        if matches:
            # Avoid recomputing next_run_at on every deploy when the schedule
            # has not changed (a busy deployment day must not starve the job).
            from cron.jobs import parse_schedule

            changes = {key: value for key, value in fields.items() if key != "schedule" and matches[0].get(key) != value}
            if matches[0].get("schedule") != parse_schedule(schedule):
                changes["schedule"] = schedule
            job = update_job(matches[0]["id"], changes) if changes else matches[0]
        else:
            job = create_job(**fields)
        if not job:
            result["failed"].append(name)
            continue
        if canary:
            job = update_job(job["id"], {"hlt_run_budget": dict(CANARY_BUDGET)})
        result["installed"].append({"name": name, "id": job["id"], "enabled": job.get("enabled", True)})
    return result
