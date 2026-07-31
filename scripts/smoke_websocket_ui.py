#!/usr/bin/env python3
"""Smoke-test the deployed Vercel UI token route and Railway WebSocket path.

This intentionally exits after the first non-error stream event. The backend
will cancel the research task when the WebSocket closes, keeping the smoke test
cheap while still proving the browser-facing auth path and research start path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.request

import websockets


SCOPE_KEYS = {
    "codebase",
    "cms",
    "qbank",
    "metrics",
    "firecrawl",
    "media",
    "audience",
    "recruiting",
}


def team_gate_cookie(ui_url: str, team_password: str | None = None) -> str | None:
    """Log in through the shared-password team gate and return the session cookie.

    The Vercel UI middleware gates /api/ws-token behind TEAM_ACCESS_PASSWORD.
    Pass the password explicitly or export TEAM_ACCESS_PASSWORD; when neither is
    set the gate is assumed to be off (local dev) and no cookie is used.
    """
    password = team_password if team_password is not None else os.getenv("TEAM_ACCESS_PASSWORD")
    if not password:
        return None
    request = urllib.request.Request(
        f"{ui_url.rstrip('/')}/api/auth/login",
        data=json.dumps({"password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        set_cookie = response.headers.get("Set-Cookie") or ""
    cookie_pair = set_cookie.split(";", 1)[0].strip()
    if not cookie_pair:
        raise RuntimeError("team gate login succeeded but no session cookie was set")
    return cookie_pair


def fetch_ws_token(ui_url: str, team_password: str | None = None) -> str:
    headers = {}
    cookie = team_gate_cookie(ui_url, team_password)
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(
        f"{ui_url.rstrip('/')}/api/ws-token", headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        payload = json.load(response)
    token = payload.get("ws_token")
    if not token:
        raise RuntimeError(f"No ws_token in response: {payload}")
    return token


async def smoke(args: argparse.Namespace) -> None:
    token = fetch_ws_token(args.ui_url)
    ws_url = args.api_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    requested_scope = [
        key.strip()
        for key in args.scope.split(",")
        if key.strip()
    ]
    unknown_scope = sorted(set(requested_scope) - SCOPE_KEYS)
    if unknown_scope:
        raise RuntimeError(f"unknown scope key(s): {', '.join(unknown_scope)}")

    payload = {
        "task": args.query,
        "report_type": "research_report",
        "report_source": "web",
        "tone": "Objective",
        "query_domains": [],
        "mcp_enabled": False,
        "mcp_strategy": "fast",
        "mcp_configs": [],
    }
    if args.auto or not requested_scope:
        payload["hlt_research_scope"] = {
            "auto": True,
            "depth": args.depth,
        }
    else:
        payload["hlt_research_scope"] = {
            **{key: key in requested_scope for key in SCOPE_KEYS},
            "auto": False,
            "depth": args.depth,
        }

    expect_auto = bool(args.auto or not requested_scope)
    expected_active = {
        key.strip()
        for key in (args.expect_active or "").split(",")
        if key.strip()
    }

    async with websockets.connect(f"{ws_url}/ws?ws_token={token}") as websocket:
        await websocket.send("start " + json.dumps(payload))
        for attempt in range(args.max_messages):
            raw = await asyncio.wait_for(websocket.recv(), timeout=args.timeout)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")

            if "name 'os' is not defined" in raw or "JSONDecodeError" in raw:
                raise RuntimeError(f"fatal startup error: {raw[:500]}")

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                print(f"stream_text={raw[:160]}")
                return

            event_type = event.get("type")
            content = event.get("content")
            output = str(event.get("output", ""))
            print(f"stream_event_{attempt + 1}=type:{event_type} content:{content}")

            if event_type == "logs" and content == "error":
                raise RuntimeError(f"backend returned error event: {output[:500]}")
            if event_type == "logs" and content == "hlt_scope_status":
                metadata = event.get("metadata") or {}
                hlt_scope = metadata.get("hlt_research_scope") or {}
                active = hlt_scope.get("active_sources", [])
                degraded = hlt_scope.get("degraded_sources", [])
                auto_meta = hlt_scope.get("auto_scope") or {}
                print(f"hlt_scope_active={','.join(active) if active else 'none'}")
                print(f"hlt_scope_degraded={','.join(degraded) if degraded else 'none'}")
                print(f"hlt_auto_requested={auto_meta.get('requested')}")
                print(f"hlt_auto_applied={','.join(auto_meta.get('applied') or []) or 'none'}")
                if expect_auto and not auto_meta.get("requested"):
                    raise RuntimeError("expected auto scope but auto_scope.requested was false")
                if expected_active and set(active) != expected_active:
                    # Allow supersets when other ready scopes also match; require all expected.
                    if not expected_active.issubset(set(active)):
                        raise RuntimeError(
                            f"expected active scopes to include {sorted(expected_active)}, got {active}"
                        )
                if args.expect_empty_active and active:
                    raise RuntimeError(f"expected no active scopes, got {active}")
                if degraded and not args.allow_degraded_scope:
                    raise RuntimeError(f"scope degraded: {', '.join(degraded)}")
                return
            if event_type and not expect_auto and not requested_scope:
                return

    raise RuntimeError("WebSocket closed before any stream event was observed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-url", default="https://gpt-researcher-ui.vercel.app")
    parser.add_argument("--api-url", default="https://gpt-researcher-api-production.up.railway.app")
    parser.add_argument("--query", default="smoke test: GPT Researcher WebSocket startup")
    parser.add_argument(
        "--scope",
        default="",
        help="Comma-separated pinned HLT scopes. Empty (default) uses auto scope.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Force auto scope even when --scope is also provided",
    )
    parser.add_argument(
        "--expect-active",
        default="",
        help="Comma-separated scopes that must appear in active_sources",
    )
    parser.add_argument(
        "--expect-empty-active",
        action="store_true",
        help="Fail if any internal scope activates",
    )
    parser.add_argument("--depth", choices=["fast", "balanced", "deep"], default="balanced")
    parser.add_argument("--allow-degraded-scope", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-messages", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(smoke(args))


if __name__ == "__main__":
    main()
