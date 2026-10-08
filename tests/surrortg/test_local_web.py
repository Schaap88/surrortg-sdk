import asyncio
import re
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from surrortg.controller_config import ControllerConfig, ControllerConfigurationStore
from surrortg.local_web import (
    AdminPasswordStore,
    Authentication,
    LocalControllerService,
    create_app,
)


PASSWORD = "local-development-admin"


class MutableClock:
    def __init__(self):
        self.value = 1000

    def __call__(self):
        return self.value


class LocalWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ControllerConfigurationStore(root / "controller.toml", root / "credential")
        self.config = ControllerConfig(1, "controller-a", "https://signal.test/signaling", "12", "games.bot")
        self.store.update(self.config)
        self.store.replace_secret("controller-secret-never-disclose")
        self.runtime_status = {
            "runtime": {"reachable": True, "state": "running", "uptime_seconds": 42},
            "transport": {"connected": True, "state": "connected"},
            "admission": {"state": "admitted", "game_id": "12", "controller_id": "44", "connection_epoch": "epoch-1"},
            "applied_configuration": {"revision": "runtime-revision", "digest": "digest"},
            "robots": [
                {"robot_id": "r1", "seat": 0, "implementation_kind": "simulated", "backend_reachable": True, "ready": True, "faults": []},
                {"robot_id": "r2", "seat": 1, "implementation_kind": "physical", "backend_reachable": False, "ready": False, "faults": [{"fault_id": "f1", "code": "offline", "severity": "transient", "message": "private detail"}]},
            ],
            "unexpected": {"credential": "bad"},
        }
        self.reconnecting = False

        async def ipc(path, command, timeout=1):
            if command == "status":
                return {"ok": True, "status": self.runtime_status}
            requested = not self.reconnecting
            self.reconnecting = True
            return {"ok": True, "reconnect": {"requested": requested, "state": "reconnecting"}}

        self.passwords = AdminPasswordStore(root / "admin.hash")
        self.passwords.provision(PASSWORD)
        self.clock = MutableClock()
        app = create_app(
            LocalControllerService(self.store, root / "runtime.sock", ipc),
            self.passwords,
            session_ttl=60,
            clock=self.clock,
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self, password=PASSWORD):
        return await self.client.post("/login", data={"password": password}, allow_redirects=False)

    async def csrf(self):
        response = await self.client.get("/configuration")
        body = await response.text()
        return re.search(r'name="csrf" value="([^"]+)"', body).group(1)

    async def test_login_logout_and_unauthenticated_write(self):
        response = await self.client.post("/configuration", data={})
        self.assertEqual(response.status, 401)
        response = await self.login()
        self.assertEqual(response.status, 303)
        token = await self.csrf()
        response = await self.client.post("/logout", data={"csrf": token}, allow_redirects=False)
        self.assertEqual(response.status, 303)
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)

    async def test_invalid_login_rate_limit_and_session_expiry(self):
        for _ in range(5):
            response = await self.login("definitely-wrong")
            self.assertEqual(response.status, 401)
        response = await self.login("definitely-wrong")
        self.assertEqual(response.status, 429)
        self.clock.value += 61
        await self.login()
        self.clock.value += 61
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)

    async def test_csrf_host_and_origin_are_rejected(self):
        await self.login()
        response = await self.client.post("/reconnect", data={})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/health", headers={"Host": "evil.example"})
        self.assertEqual(response.status, 400)
        response = await self.client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)

    async def test_overview_states_and_sanitization(self):
        await self.login()
        response = await self.client.get("/")
        body = await response.text()
        self.assertIn("Degraded", body)
        self.assertIn("offline", body)
        self.assertIn("simulated", body)
        self.assertNotIn("private detail", body)
        self.assertNotIn("controller-secret-never-disclose", body)
        self.runtime_status = {
            "runtime": {"reachable": True, "state": "running"},
            "transport": {"connected": True, "state": "connected"},
            "admission": {"state": "pending"},
            "robots": [],
        }
        body = await (await self.client.get("/")).text()
        self.assertIn("Connected, not admitted", body)

    async def test_valid_update_empty_secret_conflict_and_validation(self):
        await self.login()
        token = await self.csrf()
        values = {"csrf": token, "revision": self.config.revision, "device_id": "controller-a", "signaling_endpoint": "https://new.test/signaling", "game_id": "12", "runtime_module": "games.bot", "credential": ""}
        response = await self.client.post("/configuration", data=values)
        self.assertEqual(response.status, 200)
        self.assertIn("Reconnect is required", await response.text())
        self.assertEqual(self.store.load_secret(), "controller-secret-never-disclose")
        response = await self.client.post("/configuration", data=values)
        self.assertEqual(response.status, 409)
        values.update({"revision": self.store.load().revision, "signaling_endpoint": "ftp://invalid"})
        response = await self.client.post("/configuration", data=values)
        self.assertEqual(response.status, 400)

    async def test_credential_replacement_and_restart_classification(self):
        await self.login()
        token = await self.csrf()
        values = {"csrf": token, "revision": self.config.revision, "device_id": "controller-b", "signaling_endpoint": self.config.signaling_endpoint, "game_id": "12", "runtime_module": "games.bot", "credential": "replacement-secret"}
        response = await self.client.post("/configuration", data=values)
        body = await response.text()
        self.assertIn("runtime restart is required", body)
        self.assertEqual(self.store.load_secret(), "replacement-secret")
        self.assertNotIn("replacement-secret", body)

    async def test_reconnect_is_post_only_and_idempotent(self):
        await self.login()
        self.assertEqual((await self.client.get("/reconnect")).status, 405)
        token = await self.csrf()
        first = await self.client.post("/reconnect", data={"csrf": token}, allow_redirects=False)
        self.assertEqual(first.status, 303)
        self.assertIn("reconnect=requested", first.headers["Location"])
        page = await self.client.get(first.headers["Location"])
        self.assertIn("Reconnect requested", await page.text())
        # Refreshing the result page is GET-only and cannot issue another action.
        await self.client.get(first.headers["Location"])
        self.assertTrue(self.reconnecting)
        second = await self.client.post("/reconnect", data={"csrf": token}, allow_redirects=False)
        self.assertIn("reconnect=already", second.headers["Location"])

    async def test_diagnostics_are_allow_listed_and_redacted(self):
        await self.login()
        response = await self.client.get("/diagnostics.json")
        payload = await response.json()
        serialized = str(payload)
        self.assertTrue(payload["configuration_valid"])
        self.assertIn("runtime", payload)
        self.assertIn("python_version", payload["process"])
        self.assertNotIn("controller-secret-never-disclose", serialized)
        self.assertNotIn("private detail", serialized)
        self.assertNotIn("unexpected", serialized)


class RuntimeUnavailableTest(unittest.IsolatedAsyncioTestCase):
    async def test_absent_refused_and_timeout_remain_useful_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControllerConfigurationStore(root / "controller.toml", root / "credential")
            store.update(ControllerConfig(1, "c", "http://localhost:3000/signaling", "1"))

            async def unavailable(path, command, timeout=1):
                await asyncio.sleep(0.01)
                return {"ok": False, "error": "runtime_unavailable", "status": {"runtime": {"reachable": False, "state": "unavailable"}}}

            service = LocalControllerService(store, root / "missing.sock", unavailable)
            overview = await asyncio.wait_for(service.overview(), timeout=0.2)
            self.assertEqual(overview["overall"][0], "not-configured")
            self.assertFalse(overview["runtime"]["runtime"]["reachable"])
            result = await asyncio.wait_for(service.reconnect(), timeout=0.2)
            self.assertEqual(result["error"], "runtime_unavailable")


class AuthenticationUnitTest(unittest.TestCase):
    def test_password_hash_never_stores_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AdminPasswordStore(Path(directory) / "admin.hash")
            store.provision(PASSWORD)
            self.assertTrue(store.verify(PASSWORD))
            self.assertNotIn(PASSWORD, store.path.read_text())
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
