import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path

from surrortg.management_ipc import ManagementServer, management_request


class ManagementIPCTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "management.sock"
        self.reconnects = 0

        async def status():
            return {"runtime": {"reachable": True}, "robots": []}

        async def reconnect():
            self.reconnects += 1
            return {"requested": self.reconnects == 1, "state": "reconnecting"}

        self.server = ManagementServer(status, reconnect, self.path)
        self.task = asyncio.create_task(self.server.run())
        for _ in range(100):
            if self.path.exists():
                break
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.temp.cleanup()

    async def test_status_and_idempotent_reconnect(self):
        status = await management_request(self.path, "status")
        self.assertTrue(status["status"]["runtime"]["reachable"])
        first = await management_request(self.path, "reconnect")
        second = await management_request(self.path, "reconnect")
        self.assertTrue(first["reconnect"]["requested"])
        self.assertFalse(second["reconnect"]["requested"])

    async def test_arbitrary_commands_are_rejected(self):
        response = await management_request(self.path, "shell")
        self.assertEqual(response, {"ok": False, "error": "unsupported_command"})

    async def test_absent_and_stale_socket_are_runtime_unavailable(self):
        missing = await management_request(Path(self.temp.name) / "missing", "status")
        self.assertEqual(missing["error"], "runtime_unavailable")
        stale = Path(self.temp.name) / "stale"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(stale))
        sock.close()
        unavailable = await management_request(stale, "status")
        self.assertEqual(unavailable["status"]["runtime"]["state"], "unavailable")

