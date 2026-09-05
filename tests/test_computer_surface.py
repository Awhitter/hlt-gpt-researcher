from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "agent"


def _load_computer_surface():
    module_name = "hlt_agent_computer_surface"
    spec = importlib.util.spec_from_file_location(
        module_name, SERVICE_DIR / "computer_surface.py"
    )
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _dashboard() -> FastAPI:
    dashboard = FastAPI()

    @dashboard.get("/chat")
    async def chat(request: Request):
        return JSONResponse(
            {
                "ok": True,
                "prefix": request.headers.get("x-forwarded-prefix"),
            }
        )

    @dashboard.get("/api/ping")
    async def ping():
        return {"ok": True}

    @dashboard.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    return dashboard


def _launch_payload(surface, **overrides):
    return {
        "version": "agent_computer_launch.v1",
        "ticket": "opaque-k2-ticket",
        "redemptionUrl": surface.DEFAULT_REDEMPTION_URL,
        "agentRef": surface.AGENT_REF,
        "agentId": surface.AGENT_ID,
        "targetKind": surface.TARGET_KIND,
        "hostRef": surface.HOST_REF,
        "targetOrigin": surface.DEFAULT_PUBLIC_ORIGIN,
        **overrides,
    }


def _identity(surface):
    return {
        "version": "agent_computer_identity.v1",
        "orgId": surface.HLT_ORG_ID,
        "agentRef": surface.AGENT_REF,
        "agentId": surface.AGENT_ID,
        "targetKind": surface.TARGET_KIND,
        "identity": {"kind": "k2", "userId": "user-alec"},
    }


def test_k2_handoff_opens_one_cookie_gated_hermes_computer():
    surface = _load_computer_surface()
    app = FastAPI()
    redemptions = []

    def redeem(url, ticket, body):
        redemptions.append((url, ticket, body))
        return _identity(surface)

    readiness = surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        redemption_client=redeem,
        dashboard_app=_dashboard(),
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)

    blocked = client.get("/computer/chat", follow_redirects=False)
    handoff = client.post(
        "/hooks/control-ui-handoff",
        headers={"authorization": "Bearer shared-runtime-hook"},
        json=_launch_payload(surface),
    )
    launch_url = handoff.json()["launchUrl"]
    parsed = urlparse(launch_url)
    bootstrap = parse_qs(parsed.fragment)["handoff"][0]
    launch_page = client.get(parsed.path)
    established = client.post(surface.SESSION_PATH, json={"token": bootstrap})
    opened = client.get("/computer/chat")
    api = client.get("/computer/api/ping")

    assert readiness == {
        "ready": True,
        "targetKind": "hermes",
        "launchPath": "/agents/cleo/computer",
        "dashboardPath": "/computer",
    }
    assert blocked.status_code == 302
    assert blocked.headers["location"] == surface.LAUNCH_PATH
    assert handoff.status_code == 200
    assert parsed.scheme == "https"
    assert parsed.netloc == "hlt-hermes.onrender.com"
    assert parsed.query == ""
    assert launch_page.status_code == 200
    assert bootstrap not in launch_page.text
    assert "Connecting your K2 team identity" in launch_page.text
    assert established.status_code == 200
    assert established.json()["redirect"] == "/computer/chat"
    assert "HttpOnly" in established.headers["set-cookie"]
    assert "Secure" in established.headers["set-cookie"]
    assert opened.json() == {"ok": True, "prefix": "/computer"}
    assert api.json() == {"ok": True}
    assert redemptions == [
        (
            surface.DEFAULT_REDEMPTION_URL,
            "opaque-k2-ticket",
            {
                "version": "agent_computer_launch.v1",
                "agentRef": "agent:cleo",
                "targetKind": "hermes",
                "hostRef": "internal_system:hlt-hermes",
                "targetOrigin": surface.DEFAULT_PUBLIC_ORIGIN,
            },
        )
    ]


def test_mounted_dashboard_lifespan_starts_and_stops_with_parent():
    surface = _load_computer_surface()
    events = []

    @asynccontextmanager
    async def parent_lifespan(parent):
        events.append(("parent_start", parent))
        yield {"parent_ready": True}
        events.append(("parent_stop", parent))

    @asynccontextmanager
    async def dashboard_lifespan(child):
        events.append(("dashboard_start", child))
        yield {"dashboard_ready": True}
        events.append(("dashboard_stop", child))

    app = FastAPI(lifespan=parent_lifespan)
    dashboard = FastAPI(lifespan=dashboard_lifespan)
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        dashboard_app=dashboard,
    )

    with TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN):
        assert events == [
            ("parent_start", app),
            ("dashboard_start", dashboard),
        ]

    assert events == [
        ("parent_start", app),
        ("dashboard_start", dashboard),
        ("dashboard_stop", dashboard),
        ("parent_stop", app),
    ]


def test_browser_bootstrap_is_single_use_and_expires():
    surface = _load_computer_surface()
    clock = {"now": 100.0}
    store = surface.ComputerSessionStore(now=lambda: clock["now"])
    token = store.issue_bootstrap(
        identity={"kind": "k2", "userId": "user-alec"},
        org_id=surface.HLT_ORG_ID,
    )

    session = store.consume_bootstrap(token)

    assert session
    assert store.consume_bootstrap(token) is None
    assert store.session_valid(session) is True
    clock["now"] += surface.SESSION_TTL_SECONDS + 1
    assert store.session_valid(session) is False


def test_runtime_rejects_wrong_binding_without_redeeming_ticket():
    surface = _load_computer_surface()
    app = FastAPI()
    calls = []
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        redemption_client=lambda *args: calls.append(args) or _identity(surface),
        dashboard_app=_dashboard(),
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)

    wrong_host = client.post(
        "/hooks/control-ui-handoff",
        headers={"authorization": "Bearer shared-runtime-hook"},
        json=_launch_payload(surface, hostRef="internal_system:someone-else"),
    )
    wrong_secret = client.post(
        "/hooks/control-ui-handoff",
        headers={"authorization": "Bearer wrong"},
        json=_launch_payload(surface),
    )

    assert wrong_host.status_code == 401
    assert wrong_secret.status_code == 401
    assert calls == []


def test_runtime_rejects_a_redeemed_identity_for_another_agent():
    surface = _load_computer_surface()
    app = FastAPI()
    identity = _identity(surface)
    identity["agentRef"] = "agent:victoria"
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        redemption_client=lambda *args: identity,
        dashboard_app=_dashboard(),
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)

    response = client.post(
        "/hooks/control-ui-handoff",
        headers={"authorization": "Bearer shared-runtime-hook"},
        json=_launch_payload(surface),
    )

    assert response.status_code == 401
    assert "launchUrl" not in response.text


def test_runtime_rejects_a_redeemed_identity_outside_hlt_org():
    surface = _load_computer_surface()
    app = FastAPI()
    identity = _identity(surface)
    identity["orgId"] = "00000000-0000-0000-0000-000000000001"
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        redemption_client=lambda *args: identity,
        dashboard_app=_dashboard(),
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)

    response = client.post(
        "/hooks/control-ui-handoff",
        headers={"authorization": "Bearer shared-runtime-hook"},
        json=_launch_payload(surface),
    )

    assert response.status_code == 401
    assert "launchUrl" not in response.text


def test_redemption_is_exact_https_k2_endpoint_and_never_redirects(monkeypatch):
    surface = _load_computer_surface()
    opened = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            assert limit == 16_385
            return json.dumps(_identity(surface)).encode()

    class Opener:
        def open(self, request, *, timeout):
            opened.append((request.full_url, timeout))
            return Response()

    handlers = []

    def build_opener(handler):
        handlers.append(handler)
        return Opener()

    monkeypatch.setattr(surface.urllib.request, "build_opener", build_opener)

    result = surface.redeem_k2_ticket(
        surface.DEFAULT_REDEMPTION_URL,
        "opaque-k2-ticket",
        {"version": "agent_computer_launch.v1"},
    )

    assert result == _identity(surface)
    assert opened == [(surface.DEFAULT_REDEMPTION_URL, 8.0)]
    assert len(handlers) == 1
    assert isinstance(handlers[0], surface._NoRedirectHandler)
    assert handlers[0].redirect_request(None, None, 302, "", None, "https://evil") is None

    with pytest.raises(ValueError, match="not trusted"):
        surface.redeem_k2_ticket(
            "https://katailyst2.vercel.app/elsewhere",
            "opaque-k2-ticket",
            {},
        )
    with pytest.raises(ValueError, match="exact K2 HTTPS endpoint"):
        surface.install_computer_surface(
            FastAPI(),
            hook_token="shared-runtime-hook",
            redemption_url="http://katailyst2.vercel.app/api/agents/computer/handoff/consume",
            dashboard_app=_dashboard(),
        )


def test_session_exchange_caps_json_and_requires_exact_token_shape():
    surface = _load_computer_surface()
    app = FastAPI()
    store = surface.ComputerSessionStore()
    bootstrap = store.issue_bootstrap(
        identity={"kind": "k2", "userId": "user-alec"},
        org_id=surface.HLT_ORG_ID,
    )
    consumed = []
    consume_bootstrap = store.consume_bootstrap

    def observe_consume(token):
        consumed.append(token)
        return consume_bootstrap(token)

    store.consume_bootstrap = observe_consume
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        dashboard_app=_dashboard(),
        sessions=store,
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)

    oversized = client.post(
        surface.SESSION_PATH,
        content=json.dumps({"token": "A" * surface.SESSION_BODY_LIMIT_BYTES}),
        headers={"content-type": "application/json"},
    )
    wrong_shape = client.post(surface.SESSION_PATH, json={"token": "A" * 42})
    extra_field = client.post(
        surface.SESSION_PATH,
        json={"token": bootstrap, "extra": True},
    )
    valid = client.post(surface.SESSION_PATH, json={"token": bootstrap})

    assert oversized.status_code == 413
    assert wrong_shape.status_code == 401
    assert extra_field.status_code == 401
    assert valid.status_code == 200
    assert consumed == ["", "", bootstrap]


def test_cookie_gate_covers_websocket_connections():
    surface = _load_computer_surface()
    app = FastAPI()
    store = surface.ComputerSessionStore()
    bootstrap = store.issue_bootstrap(
        identity={"kind": "k2", "userId": "user-alec"},
        org_id=surface.HLT_ORG_ID,
    )
    session = store.consume_bootstrap(bootstrap)
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        dashboard_app=_dashboard(),
        sessions=store,
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)

    with pytest.raises(WebSocketDisconnect) as no_session:
        with client.websocket_connect(
            "/computer/api/ws",
            headers={"origin": surface.DEFAULT_PUBLIC_ORIGIN},
        ):
            pass
    assert no_session.value.code == 4401

    client.cookies.set(surface.COOKIE_NAME, session)

    with pytest.raises(WebSocketDisconnect) as missing_origin:
        with client.websocket_connect(
            "/computer/api/ws",
            headers={"host": "hlt-hermes.onrender.com"},
        ):
            pass
    assert missing_origin.value.code == 4403

    with pytest.raises(WebSocketDisconnect) as wrong_origin:
        with client.websocket_connect(
            "/computer/api/ws",
            headers={
                "host": "hlt-hermes.onrender.com",
                "origin": "https://attacker.example",
            },
        ):
            pass
    assert wrong_origin.value.code == 4403

    with client.websocket_connect(
        "/computer/api/ws",
        headers={
            "host": "hlt-hermes.onrender.com",
            "origin": surface.DEFAULT_PUBLIC_ORIGIN,
        },
    ) as websocket:
        assert websocket.receive_text() == "ready"


def test_cookie_gate_rejects_wrong_host_and_cross_origin_http():
    surface = _load_computer_surface()
    app = FastAPI()
    store = surface.ComputerSessionStore()
    bootstrap = store.issue_bootstrap(
        identity={"kind": "k2", "userId": "user-alec"},
        org_id=surface.HLT_ORG_ID,
    )
    session = store.consume_bootstrap(bootstrap)
    surface.install_computer_surface(
        app,
        hook_token="shared-runtime-hook",
        dashboard_app=_dashboard(),
        sessions=store,
    )
    client = TestClient(app, base_url=surface.DEFAULT_PUBLIC_ORIGIN)
    client.cookies.set(surface.COOKIE_NAME, session)

    wrong_host = client.get(
        "/computer/api/ping",
        headers={"host": "attacker.example"},
    )
    wrong_origin = client.get(
        "/computer/api/ping",
        headers={"origin": "https://attacker.example"},
    )

    assert wrong_host.status_code == 403
    assert wrong_origin.status_code == 403


def test_native_dashboard_uses_public_bind_and_existing_loopback_listener(monkeypatch):
    surface = _load_computer_surface()
    native = SimpleNamespace(app=FastAPI())
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace(web_server=native))
    monkeypatch.setenv("HERMES_DASHBOARD_WS_HOST", "stale-public-host.example")

    loaded = surface._load_hermes_dashboard(
        public_origin=surface.DEFAULT_PUBLIC_ORIGIN, listen_port=10000
    )

    assert loaded is native.app
    assert native.app.state.bound_host == "hlt-hermes.onrender.com"
    assert native.app.state.bound_port == 10000
    assert native.app.state.auth_required is False
    assert native._SESSION_TOKEN == surface.DASHBOARD_MARKER_TOKEN
    assert native.app.state.trusted_public_hosts == frozenset(
        {"hlt-hermes.onrender.com", "127.0.0.1", "::1"}
    )
    # These are the two inputs used by pinned Hermes' native child URL builders.
    assert surface.os.environ["HERMES_DASHBOARD_WS_HOST"] == "127.0.0.1"


def _native_dashboard_sockets(surface):
    dashboard = FastAPI()

    async def native_socket(websocket: WebSocket):
        if websocket.query_params.get("token") != surface.DASHBOARD_MARKER_TOKEN:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_json(
            {"path": websocket.url.path, "channel": websocket.query_params.get("channel")}
        )
        await websocket.close()

    for path in ("/api/ws", "/api/pub", "/api/events", "/api/pty"):
        dashboard.add_api_websocket_route(path, native_socket)
    return dashboard


@pytest.mark.parametrize("path", ["/api/ws", "/api/pub"])
def test_native_child_urls_reach_existing_dashboard_over_direct_loopback(path):
    surface = _load_computer_surface()
    app = FastAPI()
    surface.install_computer_surface(
        app, hook_token="hook", listen_port=10000,
        dashboard_app=_native_dashboard_sockets(surface),
    )
    client = TestClient(app, base_url="http://127.0.0.1:10000", client=("127.0.0.1", 34567))

    # Exact URLs generated by pinned Hermes for the attached TUI and event publisher.
    with client.websocket_connect(
        f"ws://127.0.0.1:10000{path}?token={surface.DASHBOARD_MARKER_TOKEN}&channel=chat-proof"
    ) as websocket:
        assert websocket.receive_json() == {"path": path, "channel": "chat-proof"}
    with pytest.raises(WebSocketDisconnect) as missing_native_token:
        with client.websocket_connect(f"ws://127.0.0.1:10000{path}"):
            pass
    assert missing_native_token.value.code == 4401


@pytest.mark.parametrize("path", ["/api/ws", "/api/pub"])
@pytest.mark.parametrize(
    "peer,headers",
    [
        (("10.197.230.141", 34567), {}),
        (("10.197.230.141", 34567), {"x-forwarded-for": "127.0.0.1"}),
        (("127.0.0.1", 34567), {"x-forwarded-for": "203.0.113.8"}),
        (("127.0.0.1", 34567), {"forwarded": "for=127.0.0.1"}),
        (("127.0.0.1", 34567), {"x-real-ip": "127.0.0.1"}),
        (("127.0.0.1", 34567), {"host": "hlt-hermes.onrender.com"}),
    ],
)
def test_native_aliases_reject_remote_or_proxy_attributed_callers(path, peer, headers):
    surface = _load_computer_surface()
    app = FastAPI()
    surface.install_computer_surface(
        app, hook_token="hook", listen_port=10000,
        dashboard_app=_native_dashboard_sockets(surface),
    )
    client = TestClient(app, base_url="http://127.0.0.1:10000", client=peer)

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(
            f"ws://127.0.0.1:10000{path}?token={surface.DASHBOARD_MARKER_TOKEN}", headers=headers
        ):
            pass
    assert rejected.value.code == 4403


@pytest.mark.parametrize("path", ["/api/pty", "/api/events"])
def test_mounted_chat_sockets_accept_authenticated_render_peers_only(path):
    surface = _load_computer_surface()
    app = FastAPI()
    store = surface.ComputerSessionStore()
    bootstrap = store.issue_bootstrap(
        identity={"kind": "k2", "userId": "user-alec"}, org_id=surface.HLT_ORG_ID
    )
    session = store.consume_bootstrap(bootstrap)
    surface.install_computer_surface(
        app, hook_token="hook", sessions=store,
        dashboard_app=_native_dashboard_sockets(surface),
    )
    client = TestClient(
        app, base_url=surface.DEFAULT_PUBLIC_ORIGIN, client=("10.197.230.141", 34567)
    )
    url = (
        f"wss://hlt-hermes.onrender.com/computer{path}"
        f"?token={surface.DASHBOARD_MARKER_TOKEN}&channel=chat-proof"
    )
    headers = {"origin": surface.DEFAULT_PUBLIC_ORIGIN}
    with pytest.raises(WebSocketDisconnect) as unauthenticated:
        with client.websocket_connect(url, headers=headers):
            pass
    assert unauthenticated.value.code == 4401

    client.cookies.set(surface.COOKIE_NAME, session)
    with pytest.raises(WebSocketDisconnect) as wrong_origin:
        with client.websocket_connect(url, headers={"origin": "https://other.example"}):
            pass
    assert wrong_origin.value.code == 4403
    with client.websocket_connect(url, headers=headers) as websocket:
        assert websocket.receive_json() == {"path": f"/computer{path}", "channel": "chat-proof"}


@pytest.mark.parametrize("listen_port", [0, 65536, True, "10000"])
def test_computer_surface_rejects_invalid_listener_ports(listen_port):
    surface = _load_computer_surface()
    with pytest.raises(ValueError, match="listener port"):
        surface.install_computer_surface(
            FastAPI(), hook_token="hook", listen_port=listen_port,
            dashboard_app=_dashboard(),
        )
