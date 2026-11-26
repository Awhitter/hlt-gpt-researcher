#!/usr/bin/env python3
"""Zero-dependency Sentry Cron Monitor check-in emitter for hlt-codegraph.

Emits lightweight HTTPS Sentry envelope check-ins (`in_progress` -> `ok` / `error`)
for the `hlt-codegraph-reindex` monitor whenever `SENTRY_DSN` is configured.
Matches HLT's zero-dep Sentry envelope contract (`katailyst2/lib/operations/worker-sentry.ts`):
- Accepts only `https://<publicKey>@<host>.sentry.io/<numericProjectId>` DSNs.
- Refuses HTTP redirects so the Sentry public key never leaves Sentry's ingest host.
- Never prints or logs the DSN or key, and never fails the caller on network or HTTP errors.
- Standard library only so it runs on the image's system Python with zero added packages.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_MONITOR_SLUG = "hlt-codegraph-reindex"
DEFAULT_SCHEDULE_HOURS = 24
DEFAULT_CHECKIN_MARGIN_MINUTES = 120
DEFAULT_MAX_RUNTIME_MINUTES = 60
DEFAULT_TIMEOUT_S = 5.0
ALLOWED_STATUSES = frozenset({"in_progress", "ok", "error"})

_SENTRY_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*sentry\.io$", re.IGNORECASE)
_PROJECT_ID_RE = re.compile(r"^\d+$")
_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse 3xx redirects so the Sentry key is never sent to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        fp.close()
        # Pass a redacted URL placeholder so sentry_key never rides in the exception object.
        raise urllib.error.HTTPError("https://sentry.io/envelope", code, "redirect refused", headers, None)


def sentry_target(dsn: str | None) -> dict[str, str] | None:
    """Parse a Sentry DSN into `{origin, project_id, public_key}`, or None if invalid."""
    raw = (dsn or "").strip()
    if not raw:
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port  # raises ValueError on invalid port
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower()
    project_id = parts.path.strip("/")
    if (
        parts.scheme != "https"
        or not parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not hostname
        or not _SENTRY_HOST_RE.fullmatch(hostname)
        or not _PROJECT_ID_RE.fullmatch(project_id)
    ):
        return None
    if re.search(r"[^\x21-\x7e]", parts.username):
        return None
    origin = f"https://{hostname}" + (f":{port}" if port is not None else "")
    return {"origin": origin, "project_id": project_id, "public_key": parts.username}


def _schedule_hours(env: Mapping[str, str]) -> int:
    raw = (env.get("CODEGRAPH_REINDEX_HOURS") or "").strip()
    if raw.isdigit():
        hours = int(raw)
        if hours > 0:
            return hours
    return DEFAULT_SCHEDULE_HOURS


def _monitor_slug(env: Mapping[str, str]) -> str:
    raw = (env.get("CODEGRAPH_SENTRY_MONITOR_SLUG") or "").strip()
    if raw and _SLUG_RE.fullmatch(raw):
        return raw
    return DEFAULT_MONITOR_SLUG


def _release(env: Mapping[str, str]) -> str | None:
    sha = (env.get("RENDER_GIT_COMMIT") or "").strip()
    return sha.lower() if _SHA_RE.fullmatch(sha) else None


def build_checkin_payload(
    status: str,
    *,
    check_in_id: str | None = None,
    duration: float | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Construct the Sentry Cron Monitor `check_in` payload."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Unsupported check-in status: {status!r}")
    source_env = env if env is not None else os.environ
    cid = (check_in_id or "").strip().lower()
    if not _HEX_32_RE.fullmatch(cid):
        cid = uuid.uuid4().hex
    environment = (source_env.get("SENTRY_ENVIRONMENT") or "production").strip() or "production"
    payload: dict[str, Any] = {
        "check_in_id": cid,
        "monitor_slug": _monitor_slug(source_env),
        "status": status,
        "environment": environment,
        "monitor_config": {
            "schedule": {
                "type": "interval",
                "value": _schedule_hours(source_env),
                "unit": "hour",
            },
            "checkin_margin": DEFAULT_CHECKIN_MARGIN_MINUTES,
            "max_runtime": DEFAULT_MAX_RUNTIME_MINUTES,
            "failure_issue_threshold": 1,
            "recovery_threshold": 1,
            "timezone": "UTC",
        },
    }
    rel = _release(source_env)
    if rel:
        payload["release"] = rel
    if duration is not None and status in {"ok", "error"}:
        dur = float(duration)
        if math.isfinite(dur):
            payload["duration"] = round(max(0.0, dur), 3)
    return payload


def build_envelope(payload: Mapping[str, Any], *, now: datetime | None = None) -> bytes:
    """Serialize one `check_in` item into a Sentry envelope."""
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    header = json.dumps({"sent_at": ts}, separators=(",", ":"))
    item_header = json.dumps({"type": "check_in"}, separators=(",", ":"))
    body = json.dumps(payload, separators=(",", ":"))
    return f"{header}\n{item_header}\n{body}\n".encode("utf-8")


def envelope_url(target: Mapping[str, str]) -> str:
    key = urllib.parse.quote(target["public_key"], safe="")
    return f"{target['origin']}/api/{target['project_id']}/envelope/?sentry_key={key}&sentry_version=7"


def send_checkin(
    status: str,
    *,
    check_in_id: str | None = None,
    duration: float | None = None,
    env: Mapping[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bool:
    """Send a check-in to Sentry when `SENTRY_DSN` is set. Best-effort; never raises or logs secrets."""
    source_env = env if env is not None else os.environ
    raw_dsn = (source_env.get("SENTRY_DSN") or "").strip()
    if not raw_dsn:
        return False
    target = sentry_target(raw_dsn)
    if target is None:
        print("[codegraph] sentry check-in skipped (SENTRY_DSN is not a valid https sentry.io DSN)", file=sys.stderr)
        return False
    try:
        payload = build_checkin_payload(status, check_in_id=check_in_id, duration=duration, env=source_env)
        data = build_envelope(payload)
        req = urllib.request.Request(
            envelope_url(target),
            data=data,
            headers={
                "Content-Type": "application/x-sentry-envelope",
                "User-Agent": "hlt-codegraph-reindex/1",
            },
            method="POST",
        )
        client = opener if opener is not None else urllib.request.build_opener(_NoRedirects)
        with client.open(req, timeout=timeout) as response:
            code = getattr(response, "status", None) or response.getcode()
            return 200 <= int(code) < 300
    except urllib.error.HTTPError as error:
        print(f"[codegraph] sentry check-in warning (HTTP {error.code}); continuing", file=sys.stderr)
        return False
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        # Never print exception text: urllib errors can include the full request URL with sentry_key.
        print("[codegraph] sentry check-in warning (delivery failed); continuing", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit a Sentry Cron Monitor check-in for hlt-codegraph.")
    parser.add_argument("status", choices=sorted(ALLOWED_STATUSES))
    parser.add_argument("--check-in-id", default=None)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.dry_run:
        target = sentry_target(os.getenv("SENTRY_DSN"))
        payload = build_checkin_payload(args.status, check_in_id=args.check_in_id, duration=args.duration)
        print(
            json.dumps(
                {
                    "configured": target is not None,
                    "origin": target["origin"] if target else None,
                    "projectId": target["project_id"] if target else None,
                    "payload": payload,
                },
                indent=2,
            )
        )
        return 0

    send_checkin(args.status, check_in_id=args.check_in_id, duration=args.duration)
    # Best-effort telemetry must never fail a codegraph reindex.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - never traceback or fail the caller
        print("[codegraph] sentry check-in warning (unexpected error); continuing", file=sys.stderr)
        raise SystemExit(0)
