#!/usr/bin/env python3
"""Ask Katailyst for one-hour, read-only GitHub tokens for the repositories reindex.sh refreshes.

Since the 8 Nov 2026 key rotation GitHub refused the long-lived GITHUB_TOKEN on this service, and every
refresh failed. Katailyst's GitHub App already covers these repositories and mints short-lived tokens
through its `github.token.mint` verb, so no long-lived GitHub key has to live here.

    python3 mint_github_token.py [--dry-run] REPO [REPO ...]

REPO is a repository name (`katailyst2`) or `Awhitter/katailyst2`.

One request names every repository (at most eight, Katailyst's limit; more go in further requests). Katailyst
refuses a whole request when one name is off its App's list or not ticked on the installation, so a refused
request is asked again one repository at a time: each covered repository still gets a token and only the
uncovered one falls back.

stdout is for reindex.sh, which holds it in memory. One line per repository, tab-separated:

    minted    REPO  TOKEN
    fallback  REPO  WHY

WHY is `not-covered` (Katailyst refused this repository: not covered by the GitHub App), `scope` (the key lacks
the github.token scope), `key-refused` (Katailyst refused the key), `no-answer` (unreachable, slow past the
deadline, or an unusable reply) or `refused` (any other refusal). stderr carries one line per event and never a
credential: server text is cut and anything key-shaped is masked.

Exit codes: 0 every repository has a token; 2 some have; 3 no Katailyst credential set (KATAILYST2_MCP_TOKEN or
KATAILYST_MCP_TOKEN); 1 none minted. `--dry-run` prints the request it would send and calls nothing.

One mint (for the first, with the handshake before it) has CODEGRAPH_MINT_TIMEOUT_S seconds of wall-clock time,
45 by default, however often the server sends a keepalive. The address must be https (plain http only for
localhost, 127.0.0.1 and [::1]) and a redirect is refused, so the key is sent to that one origin only.
Standard library only, so it runs on the image's system Python.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

URL = os.getenv("KATAILYST2_MCP_URL") or "https://katailyst.com/mcp"
PERMISSIONS = {"contents": "read", "metadata": "read"}
REASON = "hlt-codegraph daily source refresh (read-only clone and fetch)"
DEFAULT_DEADLINE_S = 45.0
# Katailyst refuses a request naming more repositories than its App's list holds (eight).
MAX_PER_REQUEST = 8
MAX_REPLY_BYTES = 1 << 20
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REPOSITORY = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?")
# Anything shaped like a key is cut from messages before they reach a log.
_SECRETISH = re.compile(r"(gh[a-z]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|kata_[A-Za-z0-9_-]+|Bearer\s+\S+)")
# Katailyst's words when a repository is off the App's list, not ticked on the installation, or one too many
# (lib/providers/github-app-policy.ts and github-app.ts): asking per repository can still succeed.
_ABOUT_REPOSITORIES = re.compile(
    r"repository list|is not an? \S+ repository|not installed on|At most \d+ repositories", re.IGNORECASE)
# Its words when the key itself may not get tokens, whatever the repository.
_ABOUT_THE_KEY = re.compile(r"cannot get GitHub tokens|minted for HLT only|can get a GitHub token", re.IGNORECASE)


def _credential() -> str:
    return (os.getenv("KATAILYST2_MCP_TOKEN") or os.getenv("KATAILYST_MCP_TOKEN") or "").strip()


def _safe(text: object) -> str:
    flat = " ".join(str(text).split())
    credential = _credential()
    if credential:
        flat = flat.replace(credential, "[redacted]")
    return _SECRETISH.sub("[redacted]", flat)[:240]


def _say(line: str) -> None:
    print(f"[codegraph] {line}", file=sys.stderr)


class _NoMint(Exception):
    """Katailyst did not mint. `why` is the word reindex.sh records; `stop` means asking again cannot help."""

    def __init__(self, why: str, reason: str, stop: bool = False) -> None:
        super().__init__(reason)
        self.why, self.reason, self.stop = why, reason, stop


def _deadline_seconds() -> float:
    raw = (os.getenv("CODEGRAPH_MINT_TIMEOUT_S") or "").strip()
    if not raw:
        return DEFAULT_DEADLINE_S
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("CODEGRAPH_MINT_TIMEOUT_S must be a number of seconds above zero")
    return seconds


def _address_problem() -> str | None:
    """Why the Katailyst key must not be sent to URL, or None. Names the setting, never its value."""
    try:
        parts = urllib.parse.urlsplit(URL)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError on a port that is not a number
    except ValueError:
        return "KATAILYST2_MCP_URL is not a valid address"
    if host and (parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS)):
        return None
    return "KATAILYST2_MCP_URL must be an https address (plain http only for localhost, 127.0.0.1 and [::1])"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would follow a 302 and send `Authorization: Bearer <key>` again, to whatever origin it names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        fp.close()
        raise _NoMint("no-answer", _redirect_reason(code), stop=True)


def _redirect_reason(code: int) -> str:
    return f"Katailyst answered with a redirect (HTTP {code}); it was not followed, so the key went nowhere else"


def _within(deadline: float, seconds: float, work):  # noqa: ANN001, ANN202
    """Run `work` on a thread and stop waiting at `deadline`: a socket timeout restarts with every keepalive."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = work()
        except BaseException as error:  # noqa: BLE001 - handed to the waiting thread
            box["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    if deadline > time.monotonic():
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive() or not box:
        raise _NoMint("no-answer", f"Katailyst did not answer within {seconds:g} s", stop=True)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _read_reply(response, request_id: int) -> object:  # noqa: ANN001
    """The JSON-RPC reply to `request_id`: a JSON body, or its `data:` line in a server-sent event stream.

    Read line by line and returned as soon as the reply arrives, so a stream the server keeps open with
    keepalives is never mistaken for a reply still on its way.
    """
    size, body = 0, []
    for raw in response:
        size += len(raw)
        if size > MAX_REPLY_BYTES:
            raise _NoMint("no-answer", "Katailyst's reply was too large to read", stop=True)
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            try:
                message = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        elif line and not line.startswith((":", "event:", "id:", "retry:")):
            body.append(line)
    if not body:
        return None
    try:
        return json.loads("\n".join(body))
    except ValueError:
        raise _NoMint("no-answer", "Katailyst's reply was not JSON", stop=True) from None


class _Client:
    def __init__(self, credential: str, seconds: float) -> None:
        self._credential = credential
        self._seconds = seconds
        self._session: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirects)
        self._next_id = 0

    def _exchange(self, payload: dict, request_id: int | None, timeout: float) -> object:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._credential}",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with self._opener.open(request, timeout=timeout) as response:
            self._session = response.headers.get("Mcp-Session-Id") or self._session
            return None if request_id is None else _read_reply(response, request_id)

    def rpc(self, method: str, params: dict | None, deadline: float, notification: bool = False) -> object:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        request_id = None
        if not notification:
            self._next_id += 1
            request_id = payload["id"] = self._next_id
        if params is not None:
            payload["params"] = params
        timeout = max(0.1, deadline - time.monotonic())
        try:
            return _within(deadline, self._seconds, lambda: self._exchange(payload, request_id, timeout))
        except _NoMint:
            raise
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise _NoMint("key-refused", f"Katailyst refused the key in KATAILYST2_MCP_TOKEN (HTTP {error.code})",
                              stop=True) from None
            if 300 <= error.code < 400:
                raise _NoMint("no-answer", _redirect_reason(error.code), stop=True) from None
            raise _NoMint("no-answer", f"Katailyst answered HTTP {error.code} to the token request", stop=True) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
            # socket.timeout is TimeoutError only from Python 3.10; urllib wraps one raised while connecting.
            if isinstance(getattr(error, "reason", error), (TimeoutError, socket.timeout)):
                raise _NoMint("no-answer", f"Katailyst did not answer within {self._seconds:g} s", stop=True) from None
            raise _NoMint("no-answer", f"Katailyst could not be reached: {_safe(error)}", stop=True) from None

    def handshake(self, deadline: float) -> None:
        self.rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "hlt-codegraph-reindex", "version": "2"},
        }, deadline)
        try:
            self.rpc("notifications/initialized", None, deadline, notification=True)
        except _NoMint:
            pass

    def mint(self, repositories: list[str], deadline: float) -> dict:
        arguments = {"repositories": repositories, "permissions": PERMISSIONS, "reason": REASON}
        return _minted(self.rpc("tools/call", {"name": "github_token_mint", "arguments": arguments}, deadline))


def _refusal(data: dict) -> _NoMint:
    """Sort a tool reply that carries no token. The shapes are Katailyst's own (lib/mcp/handler.ts)."""
    meta = data.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    words = next((value for value in (data.get("error"), data.get("message"), data.get("reason"))
                  if isinstance(value, str) and value.strip()), "no token in the reply")
    # The MCP door, before the verb runs: {"error": "scope_denied", "neededScope": "github.token", ...}.
    if words == "scope_denied" or data.get("neededScope"):
        needed = data.get("neededScope")
        scope = _safe(needed) if isinstance(needed, str) and needed else "github.token"
        return _NoMint("scope", f"the key in KATAILYST2_MCP_TOKEN lacks the {scope} scope", stop=True)
    if meta.get("notInstalledOn") or meta.get("notInstalled") or _ABOUT_REPOSITORIES.search(words):
        return _NoMint("not-covered", _safe(words))
    if _ABOUT_THE_KEY.search(words):
        return _NoMint("key-refused", _safe(words), stop=True)
    # Twenty mints an hour per caller, or an App nobody registered: every further request gets the same answer.
    return _NoMint("refused", _safe(words), stop=bool(meta.get("rateLimited") or meta.get("notConfigured")))


def _minted(reply: object) -> dict:
    """The verb's result ({token, expiresAt, repositories, ...}), or _NoMint with Katailyst's reason."""
    if not isinstance(reply, dict):
        raise _NoMint("no-answer", "Katailyst sent no usable reply", stop=True)
    if "error" in reply:
        error = reply["error"]
        words = error.get("message") if isinstance(error, dict) else error
        raise _NoMint("refused", f"it refused the call: {_safe(words if isinstance(words, str) else 'unknown error')}")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise _NoMint("no-answer", "Katailyst's reply had no result", stop=True)
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        content = result.get("content")
        text = next((item["text"] for item in (content if isinstance(content, list) else [])
                     if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)), "")
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            data = {"message": text}
        if not isinstance(data, dict):
            raise _NoMint("no-answer", "Katailyst's reply was not an object", stop=True)
    token = data.get("token")
    if result.get("isError") or not isinstance(token, str) or not token:
        raise _refusal(data)
    if re.search(r"\s", token):
        raise _NoMint("no-answer", "Katailyst's reply held an unusable token", stop=True)
    return data


def _mint_all(client: _Client, repositories: list[str], seconds: float) -> dict:
    """{repository: ("minted", token) | ("fallback", why)} for every repository, in as few requests as it takes."""
    outcomes: dict = {}
    # Set by a refusal that asking again cannot change (no answer, the key itself): the rest fall back unasked.
    stopped: _NoMint | None = None

    def ask(names: list[str], deadline: float, retry_each: bool = False) -> bool:
        """One mint request for `names`. True when the caller should ask again, one repository at a time."""
        nonlocal stopped
        refusal = stopped
        if not refusal:
            try:
                data = client.mint(names, deadline)
            except _NoMint as error:
                refusal = error
            else:
                # Katailyst never trims a request: a token covers every repository named, or none is minted.
                outcomes.update((name, ("minted", data["token"])) for name in names)
                _say(f"Katailyst minted a read-only GitHub token for {', '.join(names)}; "
                     f"expires {_safe(data.get('expiresAt') or 'within the hour')}")
                return False
        if retry_each and not refusal.stop:
            _say(f"Katailyst refused one token for {', '.join(names)}: {refusal.reason}; asking per repository")
            return True
        if not stopped:
            label = " (not covered by the GitHub App)" if refusal.why == "not-covered" else ""
            _say(f"Katailyst did not mint a token for {', '.join(names)}{label}: {refusal.reason}")
            stopped = refusal if refusal.stop else None
        for name in names:
            outcomes[name] = ("fallback", refusal.why)
        return False

    # The first mint shares the handshake's clock; each later one gets its own.
    deadline = time.monotonic() + seconds
    try:
        client.handshake(deadline)
    except _NoMint as refusal:
        _say(refusal.reason)
        stopped = refusal
    for start in range(0, len(repositories), MAX_PER_REQUEST):
        batch = repositories[start:start + MAX_PER_REQUEST]
        if ask(batch, deadline if start == 0 else time.monotonic() + seconds, retry_each=len(batch) > 1):
            for name in batch:
                ask([name], time.monotonic() + seconds)
    return outcomes


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    repositories: list[str] = []
    for name in (arg for arg in argv if arg != "--dry-run"):
        if not REPOSITORY.fullmatch(name):
            _say("mint: a repository name is not valid (use katailyst2 or Awhitter/katailyst2)")
            return 1
        if name.lower() not in {seen.lower() for seen in repositories}:
            repositories.append(name)
    if not repositories:
        _say("mint: name at least one repository")
        return 1
    try:
        seconds = _deadline_seconds()
    except ValueError as error:
        _say(str(error))
        return 1
    credential = _credential()
    problem = _address_problem()
    if dry_run:
        if problem:
            _say(problem)
            return 1
        requests = [{"repositories": repositories[start:start + MAX_PER_REQUEST], "permissions": PERMISSIONS, "reason": REASON}
                    for start in range(0, len(repositories), MAX_PER_REQUEST)]
        print(json.dumps({
            "url": URL,
            "katailystCredential": "set" if credential else "unset",
            "tool": "github_token_mint",
            "arguments": requests[0],
            **({"furtherRequests": requests[1:]} if len(requests) > 1 else {}),
            "deadlineSeconds": seconds,
        }, indent=2))
        return 0
    if not credential:
        _say("no Katailyst credential set (KATAILYST2_MCP_TOKEN)")
        return 3
    if re.search(r"[^\x21-\x7e]", credential):
        _say("KATAILYST2_MCP_TOKEN is not a usable key: it holds a space, a control character or a non-ASCII one")
        return 1
    if problem:
        _say(problem)
        return 1
    outcomes = _mint_all(_Client(credential, seconds), repositories, seconds)
    for name in repositories:
        kind, value = outcomes.get(name, ("fallback", "refused"))
        sys.stdout.write(f"{kind}\t{name}\t{value}\n")
    minted = sum(1 for kind, _ in outcomes.values() if kind == "minted")
    return 0 if minted == len(repositories) else 2 if minted else 1


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as error:  # noqa: BLE001 - one line, never a traceback or a reply
        _say(f"mint stopped on an unexpected error ({type(error).__name__})")
        code = 1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass
    # A request abandoned at its deadline is still blocked on its socket; it must not hold the exit.
    os._exit(code)
