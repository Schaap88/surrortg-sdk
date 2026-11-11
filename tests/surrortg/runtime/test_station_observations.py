import asyncio
import hashlib
import json
import unittest

from surrortg.runtime import Backend, BackendRegistry, ControllerRuntime
from .test_controller_runtime import configure_command


def station_config(kind="simulated", stations=None, **overrides):
    command = configure_command([{
        "robot_id": "417", "seat": 1, "implementation_kind": kind, "runtime_config": {},
    }], **overrides)
    command["runtime_config"]["stations"] = stations if stations is not None else [
        {"key": "hopper", "module": "mass_flow", "simulated": True, "max_rate_g_per_s": 100},
        {"key": "belt", "module": "mass_flow", "simulated": True, "max_rate_g_per_s": 50},
    ]
    digest = "sha256:" + hashlib.sha256(json.dumps(
        command["runtime_config"], ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    command.update(config_revision=digest, config_digest=digest)
    return command


class StationObservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []

        async def emit(event, payload):
            self.events.append((event, payload))

        self.runtime = ControllerRuntime("controller-a", emit, clock=lambda: "2026-11-11T12:00:00Z")

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        if self.runtime.simulated_world._task:
            await self.runtime.simulated_world._task

    def observations(self):
        return [payload for event, payload in self.events if event == "station.observation"]

    async def test_payload_per_station_sequences_rate_total_and_robot_telemetry_path(self):
        command = station_config(connection_epoch=3)
        await self.runtime.apply_configuration(command)
        await asyncio.sleep(1.05)
        first = self.observations()
        self.assertEqual(len(first), 2)
        for observation in first:
            self.assertEqual(observation, {
                "schema_version": 1, "game_id": "12", "controller_id": "controller-a",
                "connection_epoch": 3, "config_revision": command["config_revision"],
                "station_key": observation["station_key"], "seq": 1,
                "observed_at": "2026-11-11T12:00:00Z",
                "mass_flow": {"rate_g_per_s": 0, "total_g": 0},
            })
        telemetry = [payload for event, payload in self.events if event == "robot.telemetry_observed"]
        self.assertEqual(len(telemetry), 1)
        self.assertEqual(telemetry[0]["robot_id"], "417")
        self.assertEqual(telemetry[0]["power"]["version"], 1)
        backend = self.runtime.robots["417"]["backend"]
        await backend.apply_control({"id": "joystick_main", "command": {"x": 0, "y": 1}})
        await asyncio.sleep(2.05)
        readings = self.observations()
        self.assertEqual(len(readings), 6)
        for index, maximum in [(0, 100), (1, 50)]:
            rising, latest = readings[2 + index], readings[4 + index]
            self.assertEqual(latest["seq"], 3)
            self.assertLess(0, rising["mass_flow"]["rate_g_per_s"])
            self.assertLess(rising["mass_flow"]["rate_g_per_s"], latest["mass_flow"]["rate_g_per_s"])
            self.assertLess(latest["mass_flow"]["rate_g_per_s"], maximum)
            self.assertGreater(latest["mass_flow"]["total_g"], rising["mass_flow"]["total_g"])
        await backend.neutralize()
        await asyncio.sleep(1.05)
        self.assertLess(self.observations()[-1]["mass_flow"]["rate_g_per_s"], readings[-1]["mass_flow"]["rate_g_per_s"])

    async def test_reconfigure_duplicate_disconnect_and_reconnect_tasks(self):
        command = station_config()
        await self.runtime.apply_configuration(command)
        old = self.runtime._station_task
        await self.runtime.apply_configuration(command)
        self.assertIs(old, self.runtime._station_task)
        await self.runtime.apply_configuration(station_config(connection_epoch=2, request_id="new"))
        self.assertTrue(old.done())
        self.assertIn("417", self.runtime.simulated_world.robots)
        await asyncio.sleep(1.05)
        self.assertEqual(len(self.observations()), 2)
        self.assertTrue(all(o["connection_epoch"] == 2 for o in self.observations()))
        self.runtime._station_totals["12"]["hopper"] = 123
        current = self.runtime._station_task
        self.runtime.disconnect()
        await asyncio.sleep(.05)
        self.assertTrue(current.done())
        self.assertEqual(len(self.observations()), 2)
        await self.runtime.apply_configuration(station_config(connection_epoch=3, request_id="third"))
        await asyncio.sleep(1.05)
        self.assertEqual([o["seq"] for o in self.observations()[-2:]], [2, 2])
        self.assertEqual(self.observations()[-2]["mass_flow"]["total_g"], 123)
        current = self.runtime._station_task
        await self.runtime.shutdown()
        self.assertTrue(current.done())

    async def test_physical_only_ignores_simulated_stations(self):
        self.runtime._backends = BackendRegistry(lambda robot: Backend("417", 1))
        await self.runtime.apply_configuration(station_config(kind="physical"))
        self.assertIsNone(self.runtime._station_task)
        self.assertEqual(self.observations(), [])

    async def test_non_simulated_station_and_removal(self):
        command = station_config()
        command["runtime_config"]["stations"][0]["simulated"] = False
        stations = command["runtime_config"]["stations"][:1]
        await self.runtime.apply_configuration(station_config(stations=stations))
        self.assertIsNone(self.runtime._station_task)
        await self.runtime.apply_configuration(station_config(request_id="enabled"))
        task = self.runtime._station_task
        await self.runtime.apply_configuration(station_config(stations=[], request_id="removed"))
        self.assertTrue(task.done())
        self.assertIsNone(self.runtime._station_task)

    async def test_invalid_station_rejected_without_replacing_task(self):
        await self.runtime.apply_configuration(station_config())
        task = self.runtime._station_task
        station = station_config()["runtime_config"]["stations"][0]
        for invalid in [{**station, "key": "Bad"}, {**station, "max_rate_g_per_s": 0},
                        {**station, "simulated": "true"}, {**station, "module": "other"}]:
            self.assertFalse(await self.runtime.apply_configuration(station_config(stations=[invalid])))
            self.assertIs(task, self.runtime._station_task)
        self.assertFalse(await self.runtime.apply_configuration(station_config(stations=[station, station])))

    async def test_disconnect_cancels_inflight_station_emit(self):
        await self.runtime.apply_configuration(station_config())
        entered = asyncio.Event()

        async def blocked_emit(event, payload):
            if event == "station.observation":
                entered.set()
                await asyncio.Event().wait()

        self.runtime._emit = blocked_emit
        await asyncio.wait_for(entered.wait(), 2)
        task = self.runtime._station_task
        self.runtime.disconnect()
        await asyncio.sleep(.05)
        self.assertTrue(task.cancelled())
