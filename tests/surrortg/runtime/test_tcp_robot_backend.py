import asyncio
import struct
import sys
import unittest
from unittest.mock import AsyncMock, patch

from surrortg.network.socket_handler import Message
from surrortg.runtime import BackendRegistry, ControllerRuntime, TcpRobotBackend

from .test_controller_runtime import configure_command, neutralize_command


class FakeEsp32:
    def __init__(
        self,
        ping=b"\xc8\x00",
        close_after_ping=False,
        stall=False,
        battery=None,
    ):
        self.ping = ping
        self.close_after_ping = close_after_ping
        self.stall = stall
        self.battery = battery
        self.frames = []
        self.connections = 0
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self.connections += 1
        ping_count = 0
        try:
            while True:
                frame = await reader.readexactly(2)
                self.frames.append(frame)
                if frame == b"\x64\x00" and self.battery is not None:
                    writer.write(
                        bytes((100,)) + struct.pack("<ff", *self.battery)
                    )
                    await writer.drain()
                if frame == b"\xc8\x00":
                    ping_count += 1
                    if self.stall:
                        continue
                    writer.write(self.ping)
                    await writer.drain()
                    if self.close_after_ping:
                        return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


def backend_for(server, **options):
    async def connector(_address):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        return TestEndpoint(reader, writer)

    return TcpRobotBackend(
        "417",
        1,
        connector,
        connect_timeout=0.2,
        response_timeout=0.06,
        heartbeat_interval=0.03,
        battery_interval=0.04,
        reconnect_initial=0.01,
        reconnect_max=0.03,
        **options,
    )


class TestEndpoint:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def send(self, data):
        try:
            self.writer.write(data)
            await self.writer.drain()
            return True
        except (ConnectionError, OSError):
            return False

    async def receive_exactly(self, count):
        return await self.reader.readexactly(count)

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


async def eventually(predicate, timeout=0.5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


class TcpRobotBackendTest(unittest.IsolatedAsyncioTestCase):
    async def configure(self, backend):
        await backend.apply_configuration({"address": "robot.local"})
        return backend

    async def test_default_tcp_backend_does_not_import_optional_color_driver(
        self,
    ):
        endpoint = FailedEndpoint()
        endpoint.receive_exactly = AsyncMock(return_value=b"\xc8\x00")

        sys.modules.pop("adafruit_tcs34725", None)
        for module_name in tuple(sys.modules):
            if module_name == "surrortg.devices" or module_name.startswith(
                "surrortg.devices."
            ):
                sys.modules.pop(module_name)
        with patch.dict(sys.modules, {"adafruit_tcs34725": None}), patch(
            "surrortg.tcp_transport.open_tcp_endpoint",
            new=AsyncMock(return_value=endpoint),
        ) as connect:
            backend = await self.configure(TcpRobotBackend("417", 1))
            await backend.start()

        self.assertTrue((await backend.status()).ready)
        connect.assert_awaited_once_with("robot.local", 31338)
        self.assertNotIn("surrortg.devices", sys.modules)
        await backend.stop()

    async def test_connection_fault_has_sanitized_exception_diagnostic(self):
        async def connector(_address):
            raise RuntimeError(
                "token=private-value at tcp://user:password@robot.local"
            )

        backend = await self.configure(TcpRobotBackend("417", 1, connector))
        with self.assertLogs(level="WARNING") as logs:
            await backend.start()
        diagnostic = (await backend.status()).faults[0]["message"]
        self.assertEqual(
            diagnostic,
            "RuntimeError: token=<redacted> at tcp://<redacted>@robot.local",
        )
        self.assertIn(diagnostic, "\n".join(logs.output))
        self.assertNotIn("private-value", "\n".join(logs.output))
        self.assertNotIn("password", "\n".join(logs.output))
        await backend.stop()

    async def test_ping_is_required_for_readiness(self):
        async with FakeEsp32() as server:
            backend = await self.configure(backend_for(server))
            await backend.start()
            self.assertTrue((await backend.status()).ready)
            self.assertEqual(server.frames[0], b"\xc8\x00")
            await backend.stop()

        for response in (b"\xc8\x01", b""):
            async with FakeEsp32(ping=response, stall=response == b"") as server:
                backend = await self.configure(backend_for(server))
                await backend.start()
                status = await backend.status()
                self.assertFalse(status.reachable)
                self.assertFalse(status.ready)
                self.assertEqual(status.faults[0]["fault_id"], "physical-tcp")
                await backend.stop()

    async def test_controls_preserve_legacy_scaling_and_neutral_values(self):
        async with FakeEsp32() as server:
            backend = await self.configure(backend_for(server))
            await backend.start()
            self.assertTrue(await backend.apply_control({
                "id": "joystick_main",
                "command": {"x": 1.0, "y": -1.0},
            }))
            self.assertTrue(await backend.apply_control({
                "id": "lift", "command": {"val": 0.5}
            }))
            self.assertTrue(await backend.apply_control({
                "id": "tilt", "command": {"val": -0.5}
            }))
            self.assertTrue(await backend.apply_control({
                "id": "drive_joystick", "command": {"x": 0, "y": 0}
            }))
            await eventually(lambda: len(server.frames) >= 7)
            controls = [
                frame
                for frame in server.frames
                if frame not in (b"\xc8\x00", b"\x64\x00")
            ]
            self.assertEqual(controls, [
                b"\x03\x78", b"\x01\x32", b"\x05\x96", b"\x07\x32",
                b"\x03\x64", b"\x01\x64",
            ])
            await backend.stop()
            await eventually(lambda: b"\xff\x00" in server.frames)
            self.assertIn(b"\xff\x00", server.frames)

    async def test_stop_and_unavailable_connection_results(self):
        async with FakeEsp32() as server:
            backend = await self.configure(backend_for(server))
            await backend.start()
            self.assertEqual(await backend.neutralize(), "succeeded")
            await eventually(lambda: b"\xff\x00" in server.frames)
            await backend.stop()
        self.assertEqual(await backend.neutralize(), "rejected")
        self.assertFalse(await backend.apply_control({
            "id": "lift", "command": {"val": 1}
        }))

    async def test_eof_is_observed_and_reconnect_recovers_readiness(self):
        async with FakeEsp32(close_after_ping=True) as server:
            backend = await self.configure(backend_for(server))
            observations = []

            async def observed(status):
                observations.append(status)

            backend.subscribe(observed)
            await backend.start()
            await eventually(
                lambda: server.connections >= 2
                and [item.ready for item in observations][-3:]
                == [True, False, True],
                timeout=0.8,
            )
            await backend.stop()

    async def test_stalled_connection_fails_closed(self):
        async with FakeEsp32() as server:
            backend = await self.configure(backend_for(server))
            await backend.start()
            self.assertTrue((await backend.status()).ready)
            server.stall = True
            await eventually(lambda: not backend._status.ready)
            self.assertFalse(await backend.apply_control({
                "id": "lift", "command": {"val": 1}
            }))
            await backend.stop()

    async def test_battery_response_is_parsed_without_stealing_ping(self):
        async with FakeEsp32(battery=(7.4, 82.5)) as server:
            backend = await self.configure(backend_for(server))
            await backend.start()
            self.assertTrue((await backend.status()).ready)
            await eventually(lambda: backend.battery is not None)
            self.assertIn(b"\x64\x00", server.frames)
            self.assertAlmostEqual(backend.battery["voltage"], 7.4, places=4)
            self.assertAlmostEqual(backend.battery["soc"], 82.5, places=4)
            await backend.stop()

    async def test_malformed_control_is_not_transmitted(self):
        async with FakeEsp32() as server:
            backend = await self.configure(backend_for(server))
            await backend.start()
            before = len(server.frames)
            self.assertFalse(await backend.apply_control({
                "id": "drive_joystick", "command": {"x": 2, "y": 0}
            }))
            self.assertFalse(await backend.apply_control({
                "id": "unknown", "command": {"val": 0}
            }))
            self.assertEqual(len(server.frames), before)
            await backend.stop()

    async def test_runtime_safety_result_and_backend_replacement(self):
        async with FakeEsp32() as server:
            created = []

            def factory(robot):
                backend = backend_for(server)
                created.append(backend)
                return backend

            events = []

            async def emit(event, payload):
                events.append((event, payload))

            runtime = ControllerRuntime(
                "controller-a", emit, BackendRegistry(factory)
            )
            first = configure_command()
            self.assertTrue(await runtime.apply_configuration(first))
            self.assertEqual(
                await runtime.neutralize(neutralize_command()), "succeeded"
            )
            self.assertEqual(
                events[-1][0], "robot.neutralization_result"
            )
            self.assertEqual(events[-1][1]["status"], "succeeded")

            await runtime.handle_message(Message(
                "newPeer", "controller-a", src="gameEngine",
                payload={"id": "player-a", "seat": 1},
            ))
            await runtime.handle_message(Message(
                "enableRouting", "controller-a", src="gameEngine",
                payload={"seat": 1},
            ))
            await runtime.handle_message(Message(
                "disableRouting", "controller-a", src="gameEngine",
                payload={"seat": 1},
            ))
            await runtime.handle_message(Message(
                "newPeer", "controller-a", src="gameEngine",
                payload={"id": "player-b", "seat": 1},
            ))
            await runtime.handle_message(Message(
                "peerLeft", "controller-a", src="gameEngine",
                payload={"id": "player-b"},
            ))
            runtime.disconnect()
            await asyncio.sleep(0)
            second = configure_command(request_id="replacement")
            self.assertTrue(await runtime.apply_configuration(second))
            self.assertFalse(created[0]._running)
            self.assertTrue(created[1]._running)
            await runtime.shutdown()
            self.assertFalse(created[1]._running)
            self.assertGreaterEqual(server.frames.count(b"\xff\x00"), 5)


class FailedEndpoint:
    def __init__(self):
        self.writes = 0
        self.closed = False

    async def send(self, _data):
        self.writes += 1
        return self.writes == 1

    async def receive_exactly(self, _count):
        return b"\xc8\x00"

    async def close(self):
        self.closed = True


class TcpRobotWriteFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_connection_attempt_is_bounded(self):
        async def connector(_address):
            await asyncio.sleep(1)

        backend = TcpRobotBackend(
            "417",
            1,
            connector,
            connect_timeout=0.02,
            reconnect_initial=1,
        )
        await backend.apply_configuration({"address": "robot.local"})
        await asyncio.wait_for(backend.start(), 0.1)
        status = await backend.status()
        self.assertFalse(status.ready)
        self.assertEqual(
            status.faults[0]["code"], "backend_connection_failed"
        )
        await backend.stop()

    async def test_failed_drain_fails_closed(self):
        endpoint = FailedEndpoint()

        async def connector(_address):
            return endpoint

        backend = TcpRobotBackend("417", 1, connector)
        await backend.apply_configuration({"address": "robot.local"})
        await backend.start()
        self.assertTrue((await backend.status()).ready)
        self.assertEqual(await backend.neutralize(), "failed")
        self.assertFalse((await backend.status()).ready)
        self.assertTrue(endpoint.closed)
        await backend.stop()
