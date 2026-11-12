import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from surrortg import get_config
from surrortg.game import Game
from surrortg.runtime import BackendRegistry, BackendStatus, ControllerRuntime, TcpRobotBackend
from surrortg.network.message_router import MultiSeatMessageRouter
from surrortg.network.socket_handler import Message
from games.skid_steer.open_world import SkidSteerOpenWorldGame


class DevelopmentBootstrapTest(unittest.IsolatedAsyncioTestCase):
    def test_skid_steer_open_world_uses_current_game_runtime(self):
        self.assertTrue(issubclass(SkidSteerOpenWorldGame, Game))
        self.assertEqual(
            SkidSteerOpenWorldGame.on_init.__qualname__,
            'SkidSteerOpenWorldGame.on_init',
        )

    def test_local_configuration_uses_current_admission_identity(self):
        root = Path(__file__).resolve().parents[3]
        config = get_config(str(root / 'configs/development/srtg.example.toml'))
        self.assertEqual(config['device_id'], 'dev-controller-001')
        self.assertEqual(config['game_engine']['id'], '1')
        self.assertEqual(config['game_engine']['url'], 'http://127.0.0.1:3000/signaling')
        self.assertNotIn('sources', config)

    async def test_unconfigured_physical_backend_cannot_claim_readiness_or_safety(self):
        backend = BackendRegistry().create({
            'robot_id': '1', 'seat': 0, 'implementation_kind': 'physical',
            'runtime_config': {},
        })
        await backend.apply_configuration({})
        await backend.start()
        status = await backend.status()
        self.assertFalse(status.reachable)
        self.assertFalse(status.ready)
        self.assertEqual(status.faults, [])
        self.assertEqual(await backend.neutralize(), 'unsupported')
        await backend.stop()


class OpenWorldInputDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.router = MultiSeatMessageRouter(lambda message: None)

        def register_inputs(inputs):
            for input_id, dev in inputs.items():
                self.router.register_input(input_id, dev)

        game = SkidSteerOpenWorldGame()
        game._io = SimpleNamespace(register_inputs=register_inputs)
        await game.on_init()
        self.backend = TcpRobotBackend("10", 1)
        self.send = AsyncMock(return_value=True)
        self.backend._endpoint = SimpleNamespace(send=self.send)
        self.backend._running = True
        self.backend._status = BackendStatus(reachable=True, ready=True)
        self.runtime = ControllerRuntime("controller-a", AsyncMock())
        self.runtime.robots = {"10": {"backend": self.backend}}
        self.runtime.seats = {1: "10"}
        for event, payload in (
            ("newPeer", {"id": "player", "seat": 1, "clientType": "player"}),
            ("enableRouting", {"seat": 1}),
        ):
            message = Message(event, "controller-a", src="gameEngine", payload=payload)
            await self.runtime.handle_message(message)
            await self.router.handle_message(message)

    async def asyncTearDown(self):
        tasks = list(self.router.router.reset_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def route_control(self, input_id, command):
        message = Message(
            "gameControls", "controller-a", src="player", seat=1,
            payload={"id": input_id, "command": command},
        )
        applied = await self.runtime.handle_message(message)
        await self.router.handle_message(message)
        return applied

    async def test_supported_controls_do_not_warn_or_apply_twice(self):
        with self.assertNoLogs(level="WARNING"):
            for input_id, command in (
                ("joystick_main", {"x": 1, "y": -1}),
                ("drive_joystick", {"x": 0, "y": 0}),
                ("lift", {"val": 0.5}),
                ("tilt", {"val": -0.5}),
            ):
                self.assertTrue(await self.route_control(input_id, command))
        self.assertEqual([call.args[0] for call in self.send.await_args_list], [
            b"\x03\x78\x01\x00", b"\x03\x64\x01\x64", b"\x05\x96", b"\x07\x32",
        ])
        self.assertIn(1, self.router.router.reset_tasks)
        # Compatibility bindings remain inert when the legacy watchdog resets.
        for binding in self.router.router.inputs.values():
            await binding.dev.reset(1)
        self.assertEqual(self.send.await_count, 4)

    async def test_unknown_control_still_warns_and_is_not_applied(self):
        with self.assertLogs(level="WARNING") as logs:
            self.assertFalse(await self.route_control("unknown", {"val": 1}))
        self.assertIn("Seat 1 received input from an unregistered source: 'unknown'", logs.output[0])
        self.send.assert_not_awaited()
