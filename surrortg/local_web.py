"""Authenticated, runtime-independent local Controller web interface."""

import argparse
import asyncio
import base64
import hashlib
import hmac
import html
import importlib.metadata
import json
import os
import platform
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .controller_config import (
    SCHEMA_VERSION,
    ChangeEffect,
    ConfigurationConflict,
    ConfigurationError,
    ControllerConfig,
    ControllerConfigurationStore,
)
from .management_ipc import default_socket_path, management_request


DEFAULT_ADMIN_HASH_PATH = Path("/etc/srtg/local-web-admin.hash")
SESSION_COOKIE = "srtg_local_session"
PASSWORD_ITERATIONS = 310_000
MAX_FORM_BYTES = 16 * 1024

try:
    SOFTWARE_VERSION = importlib.metadata.version("surrortg")
except importlib.metadata.PackageNotFoundError:
    SOFTWARE_VERSION = "development"


def hash_password(password, salt=None):
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("administrative password must be at least 12 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.strip().split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), decode(salt), int(iterations)
        )
        return hmac.compare_digest(actual, decode(expected))
    except (AttributeError, TypeError, ValueError):
        return False


class AdminPasswordStore:
    def __init__(self, path=DEFAULT_ADMIN_HASH_PATH):
        self.path = Path(path)

    def provision(self, development_password=None):
        if self.path.exists():
            return None
        password = development_password or secrets.token_urlsafe(18)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(hash_password(password) + "\n")
        return password

    def verify(self, password):
        try:
            encoded = self.path.read_text(encoding="utf-8")
        except OSError:
            return False
        return verify_password(password, encoded)


@dataclass
class Session:
    csrf: str
    expires_at: float


class Authentication:
    def __init__(self, password_store, ttl=3600, attempts=5, window=60, clock=time.time):
        self.password_store = password_store
        self.ttl = ttl
        self.attempts = attempts
        self.window = window
        self.clock = clock
        self.sessions = {}
        self.failures = {}

    def login(self, password, peer):
        now = self.clock()
        failures = [stamp for stamp in self.failures.get(peer, []) if stamp > now - self.window]
        if len(failures) >= self.attempts:
            self.failures[peer] = failures
            return None, "rate_limited"
        if not self.password_store.verify(password):
            failures.append(now)
            self.failures[peer] = failures
            return None, "invalid_credentials"
        self.failures.pop(peer, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(secrets.token_urlsafe(24), now + self.ttl)
        return token, None

    def session(self, token):
        session = self.sessions.get(token)
        if session and session.expires_at > self.clock():
            return session
        if token:
            self.sessions.pop(token, None)
        return None

    def logout(self, token):
        self.sessions.pop(token, None)


def _safe_text(value, limit=200):
    if value is None:
        return None
    return str(value)[:limit]


def sanitize_runtime_status(status):
    """Allow-list the IPC status again at the HTTP trust boundary."""
    if not isinstance(status, dict):
        return {"runtime": {"reachable": False, "state": "unavailable"}, "robots": []}
    result = {}
    for section, keys in {
        "runtime": ("reachable", "state", "uptime_seconds", "version"),
        "transport": ("connected", "state"),
        "admission": ("state", "game_id", "controller_id", "connection_epoch", "category"),
        "applied_configuration": ("revision", "digest"),
    }.items():
        source = status.get(section, {})
        if isinstance(source, dict):
            result[section] = {key: source[key] for key in keys if key in source}
    result.setdefault("runtime", {"reachable": False, "state": "unavailable"})
    result["robots"] = []
    for robot in status.get("robots", []) if isinstance(status.get("robots"), list) else []:
        if not isinstance(robot, dict):
            continue
        clean = {key: robot[key] for key in (
            "robot_id", "seat", "implementation_kind", "backend_reachable", "ready"
        ) if key in robot}
        clean["faults"] = []
        for fault in robot.get("faults", []) if isinstance(robot.get("faults"), list) else []:
            if isinstance(fault, dict):
                clean["faults"].append({key: _safe_text(fault[key]) for key in (
                    "fault_id", "code", "severity", "observed_at"
                ) if key in fault})
        result["robots"].append(clean)
    return result


class LocalControllerService:
    def __init__(self, store, socket_path, ipc=management_request):
        self.store = store
        self.socket_path = Path(socket_path)
        self.ipc = ipc

    def configuration(self):
        try:
            config = self.store.load()
            configuration = config.sanitized()
            error = None
        except ConfigurationError as exc:
            config = None
            configuration = None
            error = _safe_text(exc)
        try:
            self.store.load_secret()
            credential_configured = True
        except ConfigurationError:
            credential_configured = False
        return config, configuration, credential_configured, error

    async def overview(self):
        config, configuration, credential, error = self.configuration()
        response = await self.ipc(self.socket_path, "status")
        runtime = sanitize_runtime_status(response.get("status", {}))
        state = self._overall_state(config, credential, runtime)
        return {
            "controller": {
                "device_id": config.device_id if config else None,
                "hostname": socket.gethostname(),
                "software_version": SOFTWARE_VERSION,
            },
            "configuration": configuration,
            "configuration_error": error,
            "credential_configured": credential,
            "runtime": runtime,
            "overall": state,
        }

    @staticmethod
    def _overall_state(config, credential, runtime):
        if not config or not credential:
            return ("not-configured", "Not configured")
        if runtime.get("runtime", {}).get("reachable") is not True:
            return ("runtime-unavailable", "Runtime unavailable")
        transport = runtime.get("transport", {})
        admission = runtime.get("admission", {})
        if transport.get("connected") is not True:
            return ("connecting", "Connecting")
        if admission.get("state") != "admitted":
            return ("not-admitted", "Connected, not admitted")
        applied = runtime.get("applied_configuration", {}).get("revision")
        if not applied:
            return ("pending", "Configuration pending")
        robots = runtime.get("robots", [])
        if any(robot.get("backend_reachable") is not True or robot.get("ready") is not True or robot.get("faults") for robot in robots):
            return ("degraded", "Degraded")
        return ("operational", "Operational")

    def update(self, values):
        current, _, _, _ = self.configuration()
        config = ControllerConfig(
            SCHEMA_VERSION,
            values.get("device_id", "").strip(),
            values.get("signaling_endpoint", "").strip(),
            values.get("game_id", "").strip(),
            values.get("runtime_module", "").strip() or None,
        )
        result = self.store.update(config, values.get("revision") or None)
        effect = ChangeEffect(result["effect"])
        credential = values.get("credential", "")
        if credential:
            self.store.replace_secret(credential)
            if effect == ChangeEffect.NONE:
                effect = ChangeEffect.RECONNECT
        result["effect"] = effect.value
        return result

    async def reconnect(self):
        return await self.ipc(self.socket_path, "reconnect")

    async def diagnostics(self):
        overview = await self.overview()
        return {
            "schema_version": overview["configuration"].get("schema_version") if overview["configuration"] else None,
            "configuration_valid": overview["configuration"] is not None,
            "configuration_error": overview["configuration_error"],
            "controller": overview["controller"],
            "credential_configured": overview["credential_configured"],
            "overall_state": overview["overall"][0],
            "runtime": overview["runtime"],
            "process": {"web_service_pid": os.getpid(), "python_version": platform.python_version()},
        }


SERVICE_KEY = web.AppKey("service", LocalControllerService)
AUTH_KEY = web.AppKey("auth", Authentication)
ALLOWED_HOSTS_KEY = web.AppKey("allowed_hosts", set)
SECURE_COOKIE_KEY = web.AppKey("secure_cookie", bool)


def _escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def _layout(title, body, session=None, overall=None):
    csrf = f'<input type="hidden" name="csrf" value="{_escape(session.csrf)}">' if session else ""
    nav = ""
    if session:
        nav = f'''<nav><a href="/">Overview</a><a href="/configuration">Configuration</a><a href="/diagnostics">Diagnostics</a><form method="post" action="/logout">{csrf}<button>Logout</button></form></nav>'''
    banner = f'<div class="state {_escape(overall[0])}"><strong>{_escape(overall[1])}</strong></div>' if overall else ""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_escape(title)} · Controller</title><style>
:root{{--bg:#f5f6f7;--card:#fff;--ink:#182026;--muted:#66717a;--line:#d8dde1;--accent:#a90066;--good:#18723c;--warn:#8a5800;--bad:#a32020}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}}header,main{{max-width:960px;margin:auto;padding:1rem}}header{{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}}h1{{font-size:1.45rem;margin:0}}nav{{display:flex;gap:.8rem;align-items:center;margin-left:auto}}nav form{{margin:0}}a{{color:var(--accent)}}button,.button{{background:var(--accent);color:#fff;border:0;border-radius:.3rem;padding:.6rem .85rem;font-weight:650;cursor:pointer}}.secondary{{background:#4d5962}}.card{{background:var(--card);border:1px solid var(--line);border-radius:.55rem;padding:1rem;margin:0 0 1rem}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1rem}}dl{{display:grid;grid-template-columns:minmax(8rem,1fr) 2fr;gap:.35rem .8rem}}dt{{color:var(--muted)}}dd{{margin:0;overflow-wrap:anywhere}}.state{{max-width:960px;margin:0 auto 1rem;padding:.75rem 1rem;border-left:5px solid var(--warn);background:#fff7df}}.state.operational{{border-color:var(--good);background:#eaf7ee}}.state.degraded,.state.runtime-unavailable,.error{{border-color:var(--bad);background:#fff0f0}}label{{display:block;font-weight:650;margin:.7rem 0 .2rem}}input{{width:100%;padding:.6rem;border:1px solid #adb5bd;border-radius:.3rem}}.hint,small{{color:var(--muted)}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:.55rem;border-bottom:1px solid var(--line)}}.ok{{color:var(--good)}}.bad{{color:var(--bad)}}code,pre{{font-family:ui-monospace,monospace}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}@media(max-width:600px){{header{{display:block}}nav{{margin-top:.7rem;overflow-x:auto}}dl{{grid-template-columns:1fr}}dd{{margin-bottom:.5rem}}}}
</style></head><body><header><h1>Local Controller</h1>{nav}</header>{banner}<main>{body}</main></body></html>'''


def _login_page(error=None):
    message = f'<p class="card error">{_escape(error)}</p>' if error else ""
    return _layout("Login", f'''{message}<section class="card"><h2>Administrator login</h2><form method="post" action="/login"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><p><button>Log in</button></p></form></section>''')


def _overview_page(data, session):
    config = data["configuration"] or {}
    runtime = data["runtime"]
    controller = data["controller"]
    admission = runtime.get("admission", {})
    transport = runtime.get("transport", {})
    applied = runtime.get("applied_configuration", {})
    robots = runtime.get("robots", [])
    robot_rows = "".join(f'''<tr><td>{_escape(r.get("robot_id"))}</td><td>{_escape(r.get("seat", "—"))}</td><td>{_escape(r.get("implementation_kind", "—"))}</td><td>{"Yes" if r.get("backend_reachable") else "No"}</td><td>{"Yes" if r.get("ready") else "No"}</td><td>{_escape(", ".join(f.get("code", "fault") for f in r.get("faults", [])) or "—")}</td></tr>''' for r in robots)
    if not robot_rows:
        robot_rows = '<tr><td colspan="6" class="hint">No Robots reported by the runtime.</td></tr>'
    runtime_info = runtime.get("runtime", {})
    body = f'''<div class="grid"><section class="card"><h2>Controller</h2><dl><dt>Device ID</dt><dd>{_escape(controller["device_id"] or "Not configured")}</dd><dt>Hostname</dt><dd>{_escape(controller["hostname"])}</dd><dt>Software</dt><dd>{_escape(controller["software_version"])}</dd><dt>Runtime</dt><dd>{_escape(runtime_info.get("state", "unavailable"))}</dd><dt>Uptime</dt><dd>{_escape(runtime_info.get("uptime_seconds", "Unavailable"))}</dd></dl></section><section class="card"><h2>Groundbreaking connection</h2><dl><dt>Endpoint</dt><dd>{_escape(config.get("signaling_endpoint", "Not configured"))}</dd><dt>Game ID</dt><dd>{_escape(config.get("game_id", "Not configured"))}</dd><dt>Credential</dt><dd>{"Configured" if data["credential_configured"] else "Not configured"}</dd><dt>Transport</dt><dd>{_escape(transport.get("state", "unavailable"))}</dd><dt>Admission</dt><dd>{_escape(admission.get("state", "unavailable"))}</dd><dt>Admitted identity</dt><dd>{_escape(admission.get("controller_id") or "—")} / game {_escape(admission.get("game_id") or "—")}</dd><dt>Applied revision</dt><dd>{_escape(applied.get("revision") or "Not applied")}</dd><dt>Connection epoch</dt><dd>{_escape(admission.get("connection_epoch") or "—")}</dd></dl></section></div><section class="card"><h2>Robots</h2><div style="overflow-x:auto"><table><thead><tr><th>Robot</th><th>Seat</th><th>Backend</th><th>Reachable</th><th>Ready</th><th>Faults</th></tr></thead><tbody>{robot_rows}</tbody></table></div></section>'''
    return _layout("Overview", body, session, data["overall"])


def _configuration_page(data, session, notice=None, error=None):
    config = data["configuration"] or {}
    message = f'<p class="card error">{_escape(error)}</p>' if error else (f'<p class="card">{_escape(notice)}</p>' if notice else "")
    body = f'''{message}<section class="card"><h2>Bootstrap configuration</h2><p class="hint">Only Controller-local connection settings are editable here.</p><form method="post" action="/configuration"><input type="hidden" name="csrf" value="{_escape(session.csrf)}"><input type="hidden" name="revision" value="{_escape(config.get("revision", ""))}"><label>Device ID</label><input name="device_id" value="{_escape(config.get("device_id", ""))}" required><label>Signaling endpoint</label><input name="signaling_endpoint" value="{_escape(config.get("signaling_endpoint", ""))}" placeholder="https://…" required><label>Game ID</label><input name="game_id" value="{_escape(config.get("game_id", ""))}" required><label>Runtime module</label><input name="runtime_module" value="{_escape(config.get("runtime_module", ""))}"><label>Replace Controller credential</label><input name="credential" type="password" autocomplete="new-password"><small>Leave empty to keep the stored credential unchanged. The current credential can never be viewed here.</small><p><button>Save configuration</button></p></form></section>'''
    return _layout("Configuration", body, session, data["overall"])


def _saved_page(data, session, effect):
    messages = {
        ChangeEffect.NONE.value: "Saved. No runtime action is needed.",
        ChangeEffect.RECONNECT.value: "Saved. Reconnect is required to apply this change.",
        ChangeEffect.RESTART.value: "Saved. A Controller runtime restart is required to apply this change.",
    }
    action = ""
    if effect == ChangeEffect.RECONNECT.value:
        action = f'''<form method="post" action="/reconnect"><input type="hidden" name="csrf" value="{_escape(session.csrf)}"><button>Apply and reconnect</button></form>'''
    return _layout("Configuration saved", f'''<section class="card"><h2>Configuration saved</h2><p>{messages.get(effect, "Saved.")}</p>{action}<p><a href="/configuration">Back to configuration</a></p></section>''', session, data["overall"])


@web.middleware
async def security_middleware(request, handler):
    allowed = request.app[ALLOWED_HOSTS_KEY]
    host = urlsplit("//" + request.host).hostname
    host = host.lower() if host else ""
    if host not in allowed:
        raise web.HTTPBadRequest(text="Unexpected Host header")
    if request.headers.get("Origin"):
        origin = urlsplit(request.headers["Origin"])
        if origin.hostname and origin.hostname.lower() not in allowed:
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed")
    if request.content_length and request.content_length > MAX_FORM_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_FORM_BYTES, actual_size=request.content_length)
    response = await handler(request)
    response.headers.update({
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'",
    })
    return response


@web.middleware
async def authentication_middleware(request, handler):
    token = request.cookies.get(SESSION_COOKIE)
    request["session_token"] = token
    request["session"] = request.app[AUTH_KEY].session(token)
    if request.path not in ("/login", "/health") and not request["session"]:
        if request.method == "GET":
            raise web.HTTPFound("/login")
        raise web.HTTPUnauthorized(text="Authentication required")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.path != "/login":
        form = await request.post()
        supplied = form.get("csrf", "")
        if not hmac.compare_digest(str(supplied), request["session"].csrf):
            raise web.HTTPForbidden(text="CSRF validation failed")
        request["form"] = form
    return await handler(request)


def create_app(service, password_store, *, allowed_hosts=None, secure_cookie=False, session_ttl=3600, clock=time.time):
    app = web.Application(middlewares=[security_middleware, authentication_middleware], client_max_size=MAX_FORM_BYTES)
    app[SERVICE_KEY] = service
    app[AUTH_KEY] = Authentication(password_store, ttl=session_ttl, clock=clock)
    app[ALLOWED_HOSTS_KEY] = {host.lower() for host in (allowed_hosts or ("localhost", "127.0.0.1", "::1"))}
    app[SECURE_COOKIE_KEY] = secure_cookie

    async def health(request):
        return web.Response(text="local-controller-web: ok\n")

    async def login_page(request):
        if request["session"]:
            raise web.HTTPFound("/")
        return web.Response(text=_login_page(), content_type="text/html")

    async def login(request):
        form = await request.post()
        peer = request.remote or "unknown"
        token, error = request.app[AUTH_KEY].login(form.get("password", ""), peer)
        if error:
            status = 429 if error == "rate_limited" else 401
            label = "Too many login attempts. Try again later." if status == 429 else "Invalid password."
            return web.Response(text=_login_page(label), status=status, content_type="text/html")
        response = web.HTTPSeeOther("/")
        response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=request.app[SECURE_COOKIE_KEY], samesite="Strict", max_age=request.app[AUTH_KEY].ttl, path="/")
        raise response

    async def logout(request):
        request.app[AUTH_KEY].logout(request["session_token"])
        response = web.HTTPSeeOther("/login")
        response.del_cookie(SESSION_COOKIE, path="/")
        raise response

    async def overview(request):
        data = await request.app[SERVICE_KEY].overview()
        return web.Response(text=_overview_page(data, request["session"]), content_type="text/html")

    async def configuration(request):
        data = await request.app[SERVICE_KEY].overview()
        notices = {
            "requested": "Reconnect requested.",
            "already": "The runtime is already reconnecting.",
            "unavailable": "Configuration is saved, but the runtime is unavailable; reconnect could not be requested.",
        }
        return web.Response(text=_configuration_page(data, request["session"], notice=notices.get(request.query.get("reconnect"))), content_type="text/html")

    async def save_configuration(request):
        try:
            result = request.app[SERVICE_KEY].update(request["form"])
        except (ConfigurationError, ConfigurationConflict) as exc:
            data = await request.app[SERVICE_KEY].overview()
            return web.Response(text=_configuration_page(data, request["session"], error=str(exc)), status=409 if isinstance(exc, ConfigurationConflict) else 400, content_type="text/html")
        data = await request.app[SERVICE_KEY].overview()
        return web.Response(text=_saved_page(data, request["session"], result["effect"]), content_type="text/html")

    async def reconnect(request):
        result = await request.app[SERVICE_KEY].reconnect()
        if not result.get("ok"):
            outcome = "unavailable"
        else:
            reconnect_result = result.get("reconnect", {})
            outcome = "requested" if reconnect_result.get("requested") else "already"
        raise web.HTTPSeeOther(f"/configuration?reconnect={outcome}")

    async def diagnostics(request):
        data = await request.app[SERVICE_KEY].diagnostics()
        body = f'''<section class="card"><h2>Diagnostics</h2><p>Sanitized, allow-listed local status only.</p><pre>{_escape(json.dumps(data, indent=2, sort_keys=True))}</pre><p><a class="button" href="/diagnostics.json" download="controller-diagnostics.json">Download JSON</a></p></section>'''
        return web.Response(text=_layout("Diagnostics", body, request["session"]), content_type="text/html")

    async def diagnostics_json(request):
        data = await request.app[SERVICE_KEY].diagnostics()
        return web.json_response(data, headers={"Content-Disposition": 'attachment; filename="controller-diagnostics.json"'})

    app.add_routes([web.get("/health", health), web.get("/login", login_page), web.post("/login", login), web.post("/logout", logout), web.get("/", overview), web.get("/configuration", configuration), web.post("/configuration", save_configuration), web.post("/reconnect", reconnect), web.get("/diagnostics", diagnostics), web.get("/diagnostics.json", diagnostics_json)])
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Controller configuration web service")
    parser.add_argument("--host", default=os.environ.get("SURRORTG_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SURRORTG_WEB_PORT", "8088")))
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("SURRORTG_CONFIG_PATH", "/etc/srtg/controller.toml")))
    parser.add_argument("--credential", type=Path, default=Path(os.environ.get("SURRORTG_CREDENTIAL_PATH", "/etc/srtg/controller.credential")))
    parser.add_argument("--admin-hash", type=Path, default=Path(os.environ.get("SURRORTG_ADMIN_HASH_PATH", str(DEFAULT_ADMIN_HASH_PATH))))
    parser.add_argument("--management-socket", type=Path, default=Path(os.environ.get("SURRORTG_MANAGEMENT_SOCKET", str(default_socket_path()))))
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--secure-cookie", action="store_true")
    parser.add_argument("--development-admin-password", help="Explicit development-only bootstrap password")
    args = parser.parse_args(argv)
    password_store = AdminPasswordStore(args.admin_hash)
    generated = password_store.provision(args.development_admin_password)
    if generated:
        label = "DEVELOPMENT" if args.development_admin_password else "ONE-TIME"
        print(f"{label} local Controller admin password: {generated}", flush=True)
    allowed = set(args.allowed_host) | {"localhost", "127.0.0.1", "::1", socket.gethostname(), args.host}
    store = ControllerConfigurationStore(args.config, args.credential)
    app = create_app(LocalControllerService(store, args.management_socket), password_store, allowed_hosts=allowed, secure_cookie=args.secure_cookie)
    print(f"Local Controller UI: http://{args.host}:{args.port}", flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
