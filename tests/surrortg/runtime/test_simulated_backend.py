import unittest

from surrortg.network.socket_handler import Message
from surrortg.runtime import (
    Backend, BackendRegistry, ControllerRuntime, SimulatedRobotBackend, TcpRobotBackend,
)
from .test_controller_runtime import configure_command, neutralize_command


class SimulatedBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_selection_respects_kind_not_address(self):
        registry = BackendRegistry()
        for config in ({}, {"address": "unused"}):
            robot = {"robot_id": "1", "seat": 0, "runtime_config": config, "implementation_kind": "simulated"}
            self.assertIsInstance(registry.create(robot), SimulatedRobotBackend)
        robot["implementation_kind"] = "physical"
        self.assertIsInstance(registry.create(robot), TcpRobotBackend)
        robot["runtime_config"] = {}
        backend = registry.create(robot)
        self.assertIs(type(backend), Backend)
        self.assertFalse((await backend.status()).ready)
        self.assertEqual(await backend.neutralize(), "unsupported")
        self.assertEqual(await TcpRobotBackend("2", 1).neutralize(), "unsupported")

    async def test_lifecycle_fault_recovery_controls_and_verified_neutral(self):
        backend = SimulatedRobotBackend("1", 0)
        observations = []

        async def observe(status):
            observations.append(status)

        backend.subscribe(observe)
        await backend.apply_configuration({})
        self.assertFalse(await backend.apply_control({"id": "j", "command": {"x": 1}}))
        self.assertEqual(await backend.neutralize(), "rejected")
        await backend.start()
        self.assertTrue(backend.started)
        self.assertTrue(observations[-1].reachable)
        self.assertTrue(observations[-1].ready)
        control = {"id": "joystick_main", "type": "joystick", "command": {"x": .5, "y": -1, "pressed": True}}
        self.assertTrue(await backend.apply_control(control))
        control["command"]["x"] = 1
        self.assertEqual(backend.last_control["command"]["x"], .5)
        self.assertFalse(backend.neutralized)
        self.assertFalse(await backend.apply_control({"id": "bad", "command": {"x": float("nan")}}))
        self.assertFalse(await backend.apply_control({"id": "bad", "command": {"action": "drive"}}))
        self.assertEqual(await backend.neutralize(), "succeeded")
        self.assertEqual(backend.outputs, {"joystick_main": {"x": 0, "y": 0, "pressed": False}})
        self.assertIsNone(backend.last_control)
        self.assertEqual(await backend.neutralize(), "succeeded")
        await backend.set_state(reachable=False, ready=True)
        self.assertFalse((await backend.status()).ready)
        self.assertFalse(await backend.apply_control(control))
        fault = {"fault_id": "test", "code": "simulated_fault", "severity": "transient", "message": "test", "blocking": True}
        await backend.set_state(reachable=True, ready=True, faults=[fault])
        self.assertFalse((await backend.status()).ready)
        await backend.set_state(reachable=True, ready=True)
        self.assertTrue((await backend.status()).ready)
        await backend.stop()
        self.assertFalse(backend.started)
        self.assertFalse((await backend.status()).reachable)
        self.assertTrue(backend.neutralized)

    async def test_real_runtime_routing_confirmation_observations_and_reuse(self):
        events = []

        async def emit(event, payload):
            events.append((event, payload))

        runtime = ControllerRuntime("controller-a", emit)
        config = configure_command([{"robot_id": "417", "seat": 1, "implementation_kind": "simulated", "runtime_config": {}}])
        self.assertTrue(await runtime.apply_configuration(config))
        self.assertEqual([event for event, _ in events], [
            "controller.configuration_applied", "robot.reachability_changed",
            "robot.readiness_changed", "controller.status_snapshot",
        ])
        backend = runtime.robots["417"]["backend"]
        self.assertTrue(events[-1][1]["robots"][0]["ready"])
        command = neutralize_command(config_revision=config["config_revision"])
        readiness = {**command, "reservation_id": "r"}
        self.assertTrue(await runtime.confirm_readiness(readiness))
        self.assertFalse(await runtime.confirm_readiness({**readiness, "connection_epoch": "old"}))
        self.assertFalse(await runtime.confirm_readiness({**readiness, "config_revision": "old"}))

        async def message(event, payload, src="gameEngine", seat=1):
            return await runtime.handle_message(Message(event, "controller-a", src=src, seat=seat, payload=payload))

        control = {"id": "joystick_main", "type": "joystick", "command": {"x": .7, "y": -.2}}
        self.assertFalse(await message("gameControls", control, src="player-a"))
        await message("newPeer", {"id": "player-a", "seat": 1})
        await message("enableRouting", {"seat": 1})
        self.assertFalse(await message("gameControls", control, src="attacker"))
        self.assertFalse(await message("gameControls", control, src="player-a", seat=0))
        self.assertTrue(await message("gameControls", control, src="player-a"))
        self.assertEqual(backend.last_control, control)
        await message("peerLeft", {"id": "player-a"})
        await message("disableRouting", {"seat": 1})
        self.assertEqual(await runtime.neutralize(command), "succeeded")
        self.assertEqual(backend.outputs, {"joystick_main": {"x": 0, "y": 0}})
        self.assertEqual(await runtime.neutralize(command), "succeeded")
        self.assertFalse(await message("gameControls", control, src="player-a"))
        await message("newPeer", {"id": "player-b", "seat": 1})
        await message("enableRouting", {"seat": 1})
        self.assertTrue(await message("gameControls", control, src="player-b"))
        # Duplicate correlated release must not actuate again after reuse.
        self.assertEqual(await runtime.neutralize(command), "succeeded")
        self.assertEqual(backend.last_control, control)
        events.clear()
        await backend.set_state(reachable=False, ready=True)
        self.assertEqual([event for event, _ in events], ["robot.reachability_changed", "robot.readiness_changed"])
        self.assertFalse(await runtime.confirm_readiness(readiness))
        fault = {"fault_id": "test", "code": "simulated_fault", "severity": "transient", "message": "test", "blocking": True}
        await backend.set_state(reachable=True, ready=True, faults=[fault])
        self.assertFalse(await runtime.confirm_readiness(readiness))
        self.assertIn("robot.fault_raised", [event for event, _ in events])
        await backend.set_state(reachable=True, ready=True)
        self.assertTrue(await runtime.confirm_readiness(readiness))
        self.assertIn("robot.fault_cleared", [event for event, _ in events])
        await runtime.emit_snapshot()
        self.assertTrue(events[-1][1]["robots"][0]["ready"])
        runtime.disconnect()
        events.clear()
        await backend.set_state(reachable=False, ready=False)
        self.assertEqual(events, [])
