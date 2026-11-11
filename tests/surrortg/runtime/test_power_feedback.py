import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from surrortg.runtime import SimulatedRobotBackend, TcpRobotBackend
from surrortg.runtime.simulated_world import SimulatedWorld


class SimulatedPowerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.world = SimulatedWorld()
        self.backend = SimulatedRobotBackend("417", 1, self.world)
        await self.backend.start()

    async def asyncTearDown(self):
        await self.backend.stop()
        if self.world._task:
            await self.world._task

    async def test_version_validity_current_ages_and_boolean_input(self):
        power = self.backend._power_observation()["power"]
        self.assertEqual(power, {
            "version": 1, "validity": 7, "voltage_mv": 7400,
            "state_of_charge_percent": 100, "current_ma": 200,
            "power_mw": None, "dock_state": "unknown", "fuel_gauge_age_ms": 0,
            "current_age_ms": 0, "dock_age_ms": None,
        })
        await self.backend.apply_control({"id": "joystick_main", "command": {"x": -.5, "y": .2, "pressed": True}})
        self.assertEqual(self.backend._power_observation()["power"]["current_ma"], 800)
        await self.backend.neutralize()
        self.assertEqual(self.backend._power_observation()["power"]["current_ma"], 200)

    async def test_charge_counts_only_non_neutral_time_and_floors(self):
        with patch("surrortg.runtime.simulated_backend.time", SimpleNamespace(monotonic=lambda: 0)):
            await self.backend.apply_control({"id": "joystick_main", "command": {"x": 0, "y": 1}})
        with patch("surrortg.runtime.simulated_backend.time", SimpleNamespace(monotonic=lambda: 60)):
            self.assertEqual(self.backend._power_observation()["power"]["state_of_charge_percent"], 99)
            await self.backend.neutralize()
        with patch("surrortg.runtime.simulated_backend.time", SimpleNamespace(monotonic=lambda: 120)):
            self.assertEqual(self.backend._power_observation()["power"]["state_of_charge_percent"], 99)
            await self.backend.apply_control({"id": "joystick_main", "command": {"y": -1}})
        with patch("surrortg.runtime.simulated_backend.time", SimpleNamespace(monotonic=lambda: 100000)):
            self.assertEqual(self.backend._power_observation()["power"]["state_of_charge_percent"], 5)

    async def test_stall_only_when_driving_outward_at_each_edge(self):
        robot = self.world.robots["417"]
        for x, y, heading in [(1000, 100, 0), (0, 100, math.pi),
                              (100, 600, math.pi / 2), (100, 0, 3 * math.pi / 2)]:
            robot.update(x=x, y=y, heading=heading)
            await self.backend.apply_control({"id": "joystick_main", "command": {"x": 0, "y": 1}})
            self.assertEqual(self.backend._power_observation()["power"]["current_ma"], 3800)
            self.assertFalse(self.world.is_moving("417"))
            await self.backend.apply_control({"id": "joystick_main", "command": {"x": 0, "y": -1}})
            self.assertEqual(self.backend._power_observation()["power"]["current_ma"], 1400)
            self.assertTrue(self.world.is_moving("417"))
        await self.backend.neutralize()
        self.assertFalse(self.world.is_pinned("417"))

    async def test_one_second_cadence_idempotent_start_and_stop(self):
        observed = []
        self.backend.subscribe_telemetry(observed.append)
        task = self.backend._telemetry_task
        await self.backend.start()
        self.assertIs(task, self.backend._telemetry_task)
        await asyncio.sleep(.05)
        self.assertEqual(observed, [])
        await asyncio.sleep(1)
        self.assertEqual(len(observed), 1)
        await asyncio.sleep(1)
        self.assertEqual(len(observed), 2)
        await self.backend.stop()
        self.assertTrue(task.done())
        await asyncio.sleep(.05)
        self.assertEqual(len(observed), 2)

    async def test_replaced_backend_cannot_remove_or_control_new_world_entry(self):
        replacement = SimulatedRobotBackend("417", 1, self.world)
        try:
            await replacement.start()
            await replacement.apply_control({"id": "joystick_main", "command": {"y": .5}})
            await self.backend.apply_control({"id": "joystick_main", "command": {"y": -1}})
            self.assertEqual(self.world.robots["417"]["drive"], .5)
            await self.backend.neutralize()
            self.assertEqual(self.world.robots["417"]["drive"], .5)
            await self.backend.stop()
            self.assertIn("417", self.world.robots)
        finally:
            await replacement.stop()

    async def test_stop_cancels_pending_async_callback(self):
        entered = asyncio.Event()

        async def callback(_):
            entered.set()
            await asyncio.Event().wait()

        self.backend.subscribe_telemetry(callback)
        await asyncio.wait_for(entered.wait(), 2)
        task = self.backend._telemetry_task
        await asyncio.wait_for(self.backend.stop(), .5)
        self.assertTrue(task.cancelled())


class PhysicalSamplingTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_per_robot_override(self):
        backend = TcpRobotBackend("417", 1)
        self.assertEqual(backend._telemetry_interval, 1)
        self.assertEqual(backend._heartbeat_interval, 2)
        await backend.apply_configuration({"address": "unused", "telemetry_interval": 7})
        self.assertEqual(backend._telemetry_interval, 7)
        self.assertEqual(TcpRobotBackend("418", 2, telemetry_interval=3)._telemetry_interval, 3)
        for interval in [True, 0, -1, "1", float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                await backend.apply_configuration({"address": "unused", "telemetry_interval": interval})


