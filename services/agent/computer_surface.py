"""One-click, identity-bound browser entry to Cleo's native Hermes computer.

Katailyst authenticates the teammate and sends one opaque, single-use ticket
server-to-server. This adapter redeems that ticket, returns a short-lived
fragment bootstrap, and exchanges it for an HttpOnly browser session. The
mounted Hermes dashboard never receives public traffic without that session;
its otherwise process-global browser token is reduced to a non-secret marker.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, AsyncIterator, Callable, Mapping

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

AGENT_REF = "agent:cleo"
AGENT_ID = "cleo"
HOST_REF = "internal_system:hlt-hermes"
TARGET_KIND = "hermes"
HLT_ORG_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_PUBLIC_ORIGIN = "https://hlt-hermes.onrender.com"
DEFAULT_REDEMPTION_URL = (
    "https://katailyst2.vercel.app/api/agents/computer/handoff/consume"
)
LAUNCH_PATH = "/agents/cleo/computer"
SESSION_PATH = f"{LAUNCH_PATH}/session"
DASHBOARD_PATH = "/computer"
DASHBOARD_MARKER_TOKEN = "k2-browser-session"
COOKIE_NAME = "__Host-cleo-computer"
BOOTSTRAP_TTL_SECONDS = 2 * 60
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_BODY_LIMIT_BYTES = 4 * 1024
BOOTSTRAP_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the K2 ticket on the one exact redemption request."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _token_digest(kind: str, token: str) -> str:
    return hashlib.sha256(
        f"hlt-cleo-computer:{kind}:v1\0{token}".encode()
    ).hexdigest()


def _cookie_value(headers: list[tuple[bytes, bytes]], name: str) -> str:
    raw = next(
        (
            value.decode("latin-1")
            for key, value in headers
            if key.lower() == b"cookie"
        ),
        "",
    )
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except Exception:
        return ""


def _header_value(headers: list[tuple[bytes, bytes]], name: str) -> str:
    expected = name.lower().encode("ascii")
    return next(
        (
            value.decode("latin-1").strip()
            for key, value in headers
            if key.lower() == expected
        ),
        "",
    )


def _validated_https_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if any(
        (
            parsed.scheme != "https",
            not parsed.hostname,
            parsed.username is not None,
            parsed.password is not None,
            parsed.path not in {"", "/"},
            bool(parsed.query),
            bool(parsed.fragment),
        )
    ):
        raise ValueError("computer public origin must be an exact HTTPS origin")
    return value.rstrip("/")


async def _read_small_session_json(request: Request) -> Any:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return None
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > SESSION_BODY_LIMIT_BYTES:
                raise OverflowError
        except ValueError:
            return None

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > SESSION_BODY_LIMIT_BYTES:
            raise OverflowError
        body.extend(chunk)
    try:
        return json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class _BootstrapRecord:
    expires_at: float
    identity: Mapping[str, str]
    org_id: str


@dataclass(frozen=True)
class _SessionRecord:
    expires_at: float
    identity: Mapping[str, str]
    org_id: str


class ComputerSessionStore:
    """In-process one-use bootstrap and browser sessions.

    A deploy intentionally expires browser sessions; the K2 button immediately
    mints a new one. Only digests are retained, and expired records are pruned
    on ordinary traffic rather than by another background process.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._bootstraps: dict[str, _BootstrapRecord] = {}
        self._sessions: dict[str, _SessionRecord] = {}

    def _prune(self, now: float) -> None:
        self._bootstraps = {
            digest: record
            for digest, record in self._bootstraps.items()
            if record.expires_at > now
        }
        self._sessions = {
            digest: record
            for digest, record in self._sessions.items()
            if record.expires_at > now
        }

    def issue_bootstrap(
        self, *, identity: Mapping[str, str], org_id: str
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = self._now()
        with self._lock:
            self._prune(now)
            self._bootstraps[_token_digest("bootstrap", token)] = _BootstrapRecord(
                expires_at=now + BOOTSTRAP_TTL_SECONDS,
                identity=dict(identity),
                org_id=org_id,
            )
        return token

    def consume_bootstrap(self, token: str) -> str | None:
        if not token or len(token) > 256:
            return None
        now = self._now()
        with self._lock:
            self._prune(now)
            record = self._bootstraps.pop(
                _token_digest("bootstrap", token), None
            )
            if not record or record.expires_at <= now:
                return None
            session = secrets.token_urlsafe(32)
            self._sessions[_token_digest("session", session)] = _SessionRecord(
                expires_at=now + SESSION_TTL_SECONDS,
                identity=record.identity,
                org_id=record.org_id,
            )
            return session

    def session_valid(self, token: str) -> bool:
        if not token or len(token) > 256:
            return False
        now = self._now()
        with self._lock:
            self._prune(now)
            record = self._sessions.get(_token_digest("session", token))
            return bool(record and record.expires_at > now)


RedemptionClient = Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]]


def redeem_k2_ticket(
    redemption_url: str,
    ticket: str,
    body: Mapping[str, Any],
) -> Mapping[str, Any]:
    if redemption_url != DEFAULT_REDEMPTION_URL:
        raise ValueError("computer handoff redemption URL is not trusted")
    request = urllib.request.Request(
        redemption_url,
        data=json.dumps(body, separators=(",", ":")).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {ticket}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=8.0) as response:
        if response.status != 200:
            raise RuntimeError("K2 refused the computer handoff")
        raw = response.read(16_385)
    if len(raw) > 16_384:
        raise RuntimeError("K2 computer handoff response was too large")
    parsed = json.loads(raw)
    if not isinstance(parsed, Mapping):
        raise RuntimeError("K2 computer handoff response was invalid")
    return parsed


def _valid_identity(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("kind") == "k2":
        user_id = value.get("userId")
        if isinstance(user_id, str) and user_id.strip():
            return {"kind": "k2", "userId": user_id.strip()}
    if value.get("kind") == "slack":
        team_id = value.get("teamId")
        user_id = value.get("userId")
        if all(isinstance(item, str) and item.strip() for item in (team_id, user_id)):
            return {
                "kind": "slack",
                "teamId": team_id.strip(),
                "userId": user_id.strip(),
            }
    return None


def _launch_body(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    exact_keys = {
        "version",
        "ticket",
        "redemptionUrl",
        "agentRef",
        "agentId",
        "targetKind",
        "hostRef",
        "targetOrigin",
    }
    if set(value) != exact_keys:
        return None
    if value.get("version") != "agent_computer_launch.v1":
        return None
    if any(not isinstance(value.get(key), str) for key in exact_keys):
        return None
    return {key: str(value[key]) for key in exact_keys}


def _bootstrap_html(k2_agent_url: str) -> str:
    # The fragment never reaches this server. Inline JS immediately removes it
    # from browser history, then exchanges it for an HttpOnly cookie.
    safe_k2_url = json.dumps(k2_agent_url)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opening Cleo</title><style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#0b0f13;color:#f5f7f8}}
body{{min-height:100vh;margin:0;display:grid;place-items:center;padding:24px}}main{{width:min(460px,100%);background:#131920;border:1px solid #27313b;border-radius:20px;padding:32px;box-sizing:border-box;box-shadow:0 24px 80px #0008}}
.mark{{width:48px;height:48px;border-radius:14px;background:#14b8a6;display:grid;place-items:center;font-size:26px;margin-bottom:24px}}h1{{font-size:25px;margin:0 0 10px}}p{{color:#aeb8c2;line-height:1.55;margin:0}}a{{color:#5eead4}}.pulse{{display:inline-block;width:8px;height:8px;border-radius:99px;background:#2dd4bf;margin-right:9px;animation:p 1.2s infinite}}@keyframes p{{50%{{opacity:.35}}}}
</style></head><body><main><div class="mark">C</div><h1>Opening Cleo’s computer</h1><p id="status"><span class="pulse"></span>Connecting your K2 team identity…</p></main>
<script>
const status=document.getElementById('status');
const k2Url={safe_k2_url};
const params=new URLSearchParams(location.hash.slice(1));
const token=params.get('handoff');history.replaceState(null,'',location.pathname);
if(!token){{status.innerHTML=`Open Cleo from <a href="${{k2Url}}">her K2 profile</a> to start a team session.`;}}
else fetch({json.dumps(SESSION_PATH)},{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{token}}),cache:'no-store',credentials:'same-origin'}})
.then(async r=>{{if(!r.ok)throw new Error();return r.json();}}).then(body=>location.replace(body.redirect))
.catch(()=>{{status.innerHTML=`This one-use link expired. <a href="${{k2Url}}">Open a fresh computer session from K2.</a>`;}});
</script></body></html>"""


class HermesComputerGate:
    """Cookie gate around the mounted Hermes ASGI dashboard, including WS."""

    def __init__(
        self,
        application: Any,
        sessions: ComputerSessionStore,
        *,
        public_origin: str,
    ) -> None:
        self.application = application
        self.sessions = sessions
        self.public_origin = public_origin
        self.expected_host = urllib.parse.urlsplit(public_origin).netloc.lower()

    def _same_origin(self, scope: Mapping[str, Any]) -> bool:
        headers = list(scope.get("headers") or [])
        host = _header_value(headers, "host").lower()
        origin = _header_value(headers, "origin")
        if host != self.expected_host:
            return False
        if scope.get("type") == "websocket":
            return origin == self.public_origin
        if origin and origin != self.public_origin:
            return False
        method = str(scope.get("method") or "GET").upper()
        return method not in {"POST", "PUT", "PATCH", "DELETE"} or bool(origin)

    async def _deny_origin(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        response = JSONResponse(
            {"error": "computer_origin_required"},
            status_code=403,
            headers=NO_STORE_HEADERS,
        )
        await response(scope, receive, send)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.application(scope, receive, send)
            return
        token = _cookie_value(scope.get("headers") or [], COOKIE_NAME)
        if not self.sessions.session_valid(token):
            if scope.get("type") == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            path = str(scope.get("path") or "")
            response = (
                JSONResponse(
                    {"error": "computer_session_required"},
                    status_code=401,
                    headers=NO_STORE_HEADERS,
                )
                if "/api/" in path
                else RedirectResponse(LAUNCH_PATH, status_code=302, headers=NO_STORE_HEADERS)
            )
            await response(scope, receive, send)
            return
        if not self._same_origin(scope):
            await self._deny_origin(scope, receive, send)
            return

        # Hermes rewrites all browser URLs from this prefix and validates its
        # ordinary marker token. The marker is intentionally non-secret: this
        # outer K2 session gate is authoritative for HTTP and WebSocket traffic.
        headers = [
            (key, value)
            for key, value in (scope.get("headers") or [])
            if key.lower() != b"x-forwarded-prefix"
        ]
        headers.append((b"x-forwarded-prefix", DASHBOARD_PATH.encode()))
        scoped = dict(scope)
        scoped["headers"] = headers
        await self.application(scoped, receive, send)


def _load_hermes_dashboard(*, public_origin: str, listen_port: int) -> Any:
    from hermes_cli import web_server

    # The dashboard shares the existing health listener. Browser traffic is
    # authenticated by HermesComputerGate; native TUI traffic uses the two
    # direct-loopback aliases below, retaining Hermes' own gateway/event protocol.
    web_server._SESSION_TOKEN = DASHBOARD_MARKER_TOKEN
    web_server.app.state.auth_required = False
    hostname = urllib.parse.urlsplit(public_origin).hostname
    web_server.app.state.bound_host = hostname
    web_server.app.state.bound_port = listen_port
    web_server.app.state.trusted_public_hosts = frozenset({hostname, "127.0.0.1", "::1"})
    # A missing bind makes upstream reject remote browser peers as loopback-only.
    # Its separate native dial setting keeps PTY children inside this process's
    # listener instead of dialing the public Render hostname without TLS.
    os.environ["HERMES_DASHBOARD_WS_HOST"] = "127.0.0.1"
    return web_server.app


def _install_native_loopback_sockets(
    application: FastAPI, dashboard: Any, *, listen_port: int
) -> None:
    """Expose only Hermes' existing child RPC and event publisher locally.

    Upstream builds root /api/ws and /api/pub URLs for its TUI child, not
    browser mount URLs. No second listener, transport, or credential is needed.
    Never accept proxy attribution as evidence that a remote caller is local.
    """
    async def native_socket(websocket: WebSocket) -> None:
        scope = websocket.scope
        headers = list(scope.get("headers") or [])
        peer = scope.get("client")
        try:
            direct_loopback = bool(peer) and ipaddress.ip_address(peer[0]).is_loopback
        except (ValueError, TypeError):
            direct_loopback = False
        forwarded = any(
            key.lower() in {b"forwarded", b"x-real-ip"}
            or key.lower().startswith(b"x-forwarded-")
            for key, _ in headers
        )
        local_host = _header_value(headers, "host").lower() in {
            f"127.0.0.1:{listen_port}", f"[::1]:{listen_port}"
        }
        if not direct_loopback or forwarded or not local_host:
            await websocket.close(code=4403)
            return
        # The native routes still enforce their ordinary session token. Only
        # already-local native traffic reaches them outside the browser gate.
        await dashboard(scope, websocket.receive, websocket.send)

    for path in ("/api/ws", "/api/pub"):
        application.add_api_websocket_route(path, native_socket)


def _compose_dashboard_lifespan(application: FastAPI, dashboard: Any) -> None:
    """Run a mounted Starlette/FastAPI dashboard's otherwise-skipped lifespan."""
    nested_context = getattr(getattr(dashboard, "router", None), "lifespan_context", None)
    if not callable(nested_context):
        return
    original_context = application.router.lifespan_context

    @asynccontextmanager
    async def merged_lifespan(parent: FastAPI) -> AsyncIterator[Mapping[str, Any] | None]:
        async with original_context(parent) as original_state:
            # Starlette does not dispatch parent lifespan events to mounted apps.
            # Invoke the child with its own app so its startup/shutdown state is
            # identical to running the native Hermes dashboard standalone.
            async with nested_context(dashboard) as nested_state:
                if nested_state is None and original_state is None:
                    yield None
                else:
                    yield {**(nested_state or {}), **(original_state or {})}

    application.router.lifespan_context = merged_lifespan


def install_computer_surface(
    application: FastAPI,
    *,
    hook_token: str,
    public_origin: str = DEFAULT_PUBLIC_ORIGIN,
    listen_port: int = 8080,
    redemption_url: str = DEFAULT_REDEMPTION_URL,
    redemption_client: RedemptionClient = redeem_k2_ticket,
    dashboard_app: Any | None = None,
    sessions: ComputerSessionStore | None = None,
) -> dict[str, Any]:
    """Install Cleo's K2 handoff and mounted native Hermes dashboard."""
    origin = _validated_https_origin(public_origin)
    if isinstance(listen_port, bool) or not isinstance(listen_port, int) or not 1 <= listen_port <= 65535:
        raise ValueError("computer listener port must be between 1 and 65535")
    if redemption_url != DEFAULT_REDEMPTION_URL:
        raise ValueError("computer redemption must use the exact K2 HTTPS endpoint")
    expected_redemption = DEFAULT_REDEMPTION_URL
    store = sessions or ComputerSessionStore()
    k2_agent_url = "https://katailyst2.vercel.app/library/agent%3Acleo"

    @application.post("/hooks/control-ui-handoff")
    async def control_ui_handoff(request: Request) -> JSONResponse:
        presented = request.headers.get("authorization", "")
        expected = f"Bearer {hook_token}"
        if not hook_token or not hmac.compare_digest(presented, expected):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers=NO_STORE_HEADERS
            )
        # Request.json has no default/catch method in Python; keep malformed
        # bodies on the same non-enumerating 401 path as mismatched bindings.
        try:
            raw_body = await request.json()
        except Exception:
            raw_body = None
        parsed = _launch_body(raw_body)
        if not parsed or any(
            (
                parsed["redemptionUrl"] != expected_redemption,
                parsed["agentRef"] != AGENT_REF,
                parsed["agentId"] != AGENT_ID,
                parsed["targetKind"] != TARGET_KIND,
                parsed["hostRef"] != HOST_REF,
                parsed["targetOrigin"] != origin,
            )
        ):
            return JSONResponse(
                {"error": "invalid_handoff"}, status_code=401, headers=NO_STORE_HEADERS
            )
        redemption_body = {
            "version": parsed["version"],
            "agentRef": AGENT_REF,
            "targetKind": TARGET_KIND,
            "hostRef": HOST_REF,
            "targetOrigin": origin,
        }
        try:
            identity = await asyncio.to_thread(
                redemption_client,
                expected_redemption,
                parsed["ticket"],
                redemption_body,
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ):
            return JSONResponse(
                {"error": "handoff_unavailable"},
                status_code=503,
                headers=NO_STORE_HEADERS,
            )
        entry_identity = _valid_identity(identity.get("identity"))
        if (
            identity.get("version") != "agent_computer_identity.v1"
            or identity.get("agentRef") != AGENT_REF
            or identity.get("agentId") != AGENT_ID
            or identity.get("targetKind") != TARGET_KIND
            or identity.get("orgId") != HLT_ORG_ID
            or not entry_identity
        ):
            return JSONResponse(
                {"error": "invalid_handoff"}, status_code=401, headers=NO_STORE_HEADERS
            )
        bootstrap = store.issue_bootstrap(
            identity=entry_identity, org_id=str(identity["orgId"])
        )
        return JSONResponse(
            {
                "version": "agent_computer_launch_response.v1",
                "launchUrl": f"{origin}{LAUNCH_PATH}#handoff={bootstrap}",
            },
            headers=NO_STORE_HEADERS,
        )

    @application.get(LAUNCH_PATH)
    async def computer_bootstrap() -> HTMLResponse:
        return HTMLResponse(
            _bootstrap_html(k2_agent_url),
            headers={
                **NO_STORE_HEADERS,
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; connect-src 'self'; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                ),
            },
        )

    @application.post(SESSION_PATH)
    async def establish_computer_session(request: Request) -> JSONResponse:
        try:
            body = await _read_small_session_json(request)
        except OverflowError:
            return JSONResponse(
                {"error": "request_too_large"},
                status_code=413,
                headers=NO_STORE_HEADERS,
            )
        except Exception:
            body = None
        token = (
            body.get("token")
            if isinstance(body, Mapping) and set(body) == {"token"}
            else None
        )
        strict_token = (
            token
            if isinstance(token, str) and BOOTSTRAP_TOKEN_RE.fullmatch(token)
            else ""
        )
        session = store.consume_bootstrap(strict_token)
        if not session:
            return JSONResponse(
                {"error": "invalid_or_expired_handoff"},
                status_code=401,
                headers=NO_STORE_HEADERS,
            )
        response = JSONResponse(
            {"ok": True, "redirect": f"{DASHBOARD_PATH}/chat"},
            headers=NO_STORE_HEADERS,
        )
        response.set_cookie(
            COOKIE_NAME,
            session,
            max_age=SESSION_TTL_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    try:
        mounted = dashboard_app or _load_hermes_dashboard(
            public_origin=origin, listen_port=listen_port
        )
        _install_native_loopback_sockets(application, mounted, listen_port=listen_port)
        application.mount(
            DASHBOARD_PATH,
            HermesComputerGate(mounted, store, public_origin=origin),
        )
        _compose_dashboard_lifespan(application, mounted)
    except Exception as exc:
        return {
            "ready": False,
            "targetKind": TARGET_KIND,
            "error": f"{type(exc).__name__}: dashboard unavailable",
        }
    return {
        "ready": bool(hook_token),
        "targetKind": TARGET_KIND,
        "launchPath": LAUNCH_PATH,
        "dashboardPath": DASHBOARD_PATH,
    }
