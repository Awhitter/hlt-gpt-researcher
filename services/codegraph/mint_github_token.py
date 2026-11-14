#!/usr/bin/env python3
"""Ask Katailyst for a one-hour, read-only GitHub token for the repositories reindex.sh refreshes.

Since the 8 Nov 2026 key rotation GitHub refused the long-lived GITHUB_TOKEN on this service, and every
refresh failed. Katailyst's GitHub App already covers these repositories and mints short-lived tokens
through its `github.token.mint` verb, so no long-lived GitHub key has to live here.

    python3 mint_github_token.py [--dry-run] REPO [REPO ...]

REPO is a repository name (`katailyst2`) or `Awhitter/katailyst2`. On success the token is the only thing
written to stdout, for reindex.sh to hold in memory; a one-line summary without it goes to stderr.

Exit codes: 0 minted; 3 no Katailyst credential set (KATAILYST2_MCP_TOKEN or KATAILYST_MCP_TOKEN); 1 Katailyst
did not mint (reason on stderr, never a credential). `--dry-run` prints the request it would send and calls
nothing. Standard library only, so it runs on the image's system Python.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = os.getenv("KATAILYST2_MCP_URL") or "https://katailyst.com/mcp"
PERMISSIONS = {"contents": "read", "metadata": "read"}
REASON = "hlt-codegraph daily source refresh (read-only clone and fetch)"
TIMEOUT_S = float(os.getenv("CODEGRAPH_MINT_TIMEOUT_S") or "30")
# Anything shaped like a key is cut from messages before they reach a log.
_SECRETISH = re.compile(r"(gh[a-z]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|kata_[A-Za-z0-9_-]+|Bearer\s+\S+)")


def _credential() -> str:
    return (os.getenv("KATAILYST2_MCP_TOKEN") or os.getenv("KATAILYST_MCP_TOKEN") or "").strip()


def _safe(text: object) -> str:
    return _SECRETISH.sub("[redacted]", " ".join(str(text).split()))[:240]


class _Client:
    def __init__(self, credential: str) -> None:
        self._credential = credential
        self._session: str | None = None

    def rpc(self, method: str, params: dict | None = None, request_id: int | None = 1) -> dict | None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._credential}",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            self._session = response.headers.get("Mcp-Session-Id") or self._session
            raw = response.read().decode("utf-8", "replace")
        if request_id is None or not raw.strip():
            return None
        # Streamable HTTP may answer as server-sent events; the JSON-RPC reply is the data line.
        if not raw.lstrip().startswith("{"):
            lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            raw = next((line for line in reversed(lines) if line.startswith("{")), "{}")
        return json.loads(raw)


def _minted(reply: dict | None) -> dict:
    """The verb's result ({token, expiresAt, repositories, ...}), or ValueError with Katailyst's reason."""
    if not isinstance(reply, dict):
        raise ValueError("Katailyst sent no reply")
    if "error" in reply:
        raise ValueError(f"Katailyst refused the call: {_safe((reply['error'] or {}).get('message', 'unknown error'))}")
    result = reply.get("result") or {}
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        text = next((item.get("text") for item in result.get("content") or [] if item.get("type") == "text"), "")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"message": text}
    if result.get("isError") or not isinstance(data.get("token"), str) or not data["token"]:
        message = data.get("message") or data.get("error") or data.get("reason") or "no token in the reply"
        if isinstance(message, dict):
            message = message.get("message") or json.dumps(message)
        raise ValueError(f"Katailyst did not mint a token: {_safe(message)}")
    return data


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    repositories = [arg for arg in argv if arg != "--dry-run"]
    if not repositories:
        print("[codegraph] mint: name at least one repository", file=sys.stderr)
        return 1
    request = {"repositories": repositories, "permissions": PERMISSIONS, "reason": REASON}
    credential = _credential()
    if dry_run:
        print(json.dumps({
            "url": URL,
            "katailystCredential": "set" if credential else "unset",
            "tool": "github_token_mint",
            "arguments": request,
        }, indent=2))
        return 0
    if not credential:
        print("[codegraph] no Katailyst credential set (KATAILYST2_MCP_TOKEN)", file=sys.stderr)
        return 3
    client = _Client(credential)
    try:
        client.rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "hlt-codegraph-reindex", "version": "1"},
        })
        try:
            client.rpc("notifications/initialized", request_id=None)
        except (urllib.error.URLError, OSError):
            pass
        data = _minted(client.rpc("tools/call", {"name": "github_token_mint", "arguments": request}, request_id=2))
    except urllib.error.HTTPError as error:
        print(f"[codegraph] Katailyst answered HTTP {error.code} to the token request", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"[codegraph] {_safe(error)}", file=sys.stderr)
        return 1
    covered = ", ".join(data.get("repositories") or repositories)
    print(f"[codegraph] Katailyst minted a read-only GitHub token for {covered}; expires {data.get('expiresAt', 'within the hour')}",
          file=sys.stderr)
    sys.stdout.write(data["token"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
