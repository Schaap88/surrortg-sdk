import hashlib
import json
import unittest
from pathlib import Path

from surrortg.runtime import (
    Backend,
    BackendRegistry,
    BackendStatus,
    ControllerRuntime,
)


class FakeBackend(Backend):
    def __init__(self, robot_id, seat, status, stopped):
        super().__init__(robot_id, seat)
        self._status = status
        self.stopped = stopped

    async def stop(self):
        self.stopped.append(self.robot_id)


class NeutralizingBackend(Backend):
    def __init__(self, robot_id, seat, result="unsupported"):
        super().__init__(robot_id, seat)
        self.result = result
        self.calls = 0
        self._status = BackendStatus(reachable=True, ready=True)

    async def neutralize(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def configure_command(robots=None, **overrides):
    runtime_config = {
        "protocol_version": "2.0",
        "game_id": "12",
        "controller_id": "controller-a",
        "robots": robots
        or [
            {
                "robot_id": "417",
                "seat": 1,
                "implementation_kind": "physical",
                "runtime_config": {"address": "10.0.0.1"},
            }
        ],
    }
    canonical = json.dumps(
        runtime_config, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "protocol_version": "2.0",
        "game_id": "12",
        "controller_id": "controller-a",
        "connection_epoch": "epoch-1",
        "request_id": "request-1",
        "config_revision": digest,
        "config_digest": digest,
        "runtime_config": runtime_config,
        **overrides,
    }


def neutralize_command(**overrides):
    return {
        "protocol_version": "2.0",
        "game_id": "12",
        "controller_id": "controller-a",
        "connection_epoch": "epoch-1",
        "config_revision": configure_command()["config_revision"],
        "request_id": "neutralize-1",
        "robot_id": "417",
        "reason": "allocation_released",
        "deadline_at": "2026-01-01T00:00:05Z",
        **overrides,
    }


class ControllerRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.stopped = []

        async def emit(event, payload):
            self.events.append((event, payload))

        statuses = {
            "417": BackendStatus(reachable=True, ready=True),
            "418": BackendStatus(
                reachable=False,
                ready=False,
                faults=[{
                    "fault_id": "offline",
                    "code": "backend_connection_failed",
                    "severity": "transient",
                    "message": "unavailable",
                }],
            ),
        }
        registry = BackendRegistry(
            lambda robot: FakeBackend(
                str(robot["robot_id"]),
                robot["seat"],
                statuses[str(robot["robot_id"])],
                self.stopped,
            )
        )
        self.runtime = ControllerRuntime(
            "controller-a", emit, registry, clock=lambda: "2026-01-01T00:00:00Z"
        )

    async def test_valid_config_applies_canonical_mapping_and_complete_snapshot(self):
        command = configure_command(
            [
                {"robot_id": "417", "seat": 1, "implementation_kind": "physical", "runtime_config": {"address": "a"}},
                {"robot_id": "418", "seat": 2, "implementation_kind": "simulated", "runtime_config": {}},
            ]
        )
        self.assertTrue(await self.runtime.apply_configuration(command))
        self.assertEqual(list(self.runtime.robots), ["417", "418"])
        self.assertEqual(self.runtime.robot_id_for_seat(2), "418")
        self.assertNotEqual("418", command["controller_id"])
        self.assertEqual(self.events[0][0], "controller.configuration_applied")
        snapshot = self.events[-1][1]
        self.assertEqual(self.events[-1][0], "controller.status_snapshot")
        self.assertEqual(len(snapshot["robots"]), 2)
        self.assertTrue(snapshot["robots"][0]["ready"])
        self.assertFalse(snapshot["robots"][1]["reachable"])
        self.assertEqual(snapshot["config_digest"], command["config_digest"])

    async def test_invalid_config_rejected_with_exact_operation_identity(self):
        command = configure_command(config_digest="sha256:wrong")
        self.assertFalse(await self.runtime.apply_configuration(command))
        event, payload = self.events[-1]
        self.assertEqual(event, "controller.configuration_rejected")
        self.assertEqual(payload["request_id"], command["request_id"])
        self.assertEqual(payload["config_digest"], "sha256:wrong")

    async def test_duplicate_is_idempotent_and_superseded_backends_stop(self):
        first = configure_command()
        await self.runtime.apply_configuration(first)
        original = self.runtime.robots["417"]["backend"]
        await self.runtime.apply_configuration(first)
        self.assertIs(self.runtime.robots["417"]["backend"], original)
        self.assertEqual(self.stopped, [])

        second = configure_command(request_id="request-2")
        await self.runtime.apply_configuration(second)
        self.assertEqual(self.stopped, ["417"])

    async def test_observations_emit_only_changes_with_increasing_sequence(self):
        await self.runtime.apply_configuration(configure_command())
        self.events.clear()
        await self.runtime.update_robot_status(
            "417", BackendStatus(reachable=True, ready=True)
        )
        self.assertEqual(self.events, [])
        await self.runtime.update_robot_status(
            "417", BackendStatus(reachable=False, ready=True)
        )
        self.assertEqual(
            [event for event, _ in self.events],
            ["robot.reachability_changed", "robot.readiness_changed"],
        )
        sequences = [payload["observation_seq"] for _, payload in self.events]
        self.assertEqual(sequences, [3, 4])
        self.assertFalse(self.events[-1][1]["ready"])

    async def test_snapshot_request_requires_current_epoch_and_revision(self):
        command = configure_command()
        await self.runtime.apply_configuration(command)
        before = len(self.events)
        message = type("Message", (), {
            "event": "controller.status_snapshot_request",
            "payload": {
                "connection_epoch": "epoch-1",
                "expected_config_revision": command["config_revision"],
            },
        })
        self.assertTrue(await self.runtime.handle_message(message))
        self.assertEqual(len(self.events), before + 1)

    async def test_disconnect_invalidates_observations_and_reconfigure_uses_new_epoch(self):
        first = configure_command()
        await self.runtime.apply_configuration(first)
        self.assertTrue(self.runtime._observations["417"]["reachable"])

        self.runtime.disconnect()
        self.assertFalse(self.runtime._observations["417"]["reachable"])
        self.assertFalse(self.runtime._observations["417"]["ready"])

        second = configure_command(
            connection_epoch="epoch-2", request_id="request-2"
        )
        await self.runtime.apply_configuration(second)
        self.assertEqual(self.runtime.connection_epoch, "epoch-2")
        stale_request = type("Message", (), {
            "event": "controller.status_snapshot_request",
            "payload": {
                "connection_epoch": "epoch-1",
                "expected_config_revision": second["config_revision"],
            },
        })
        self.assertFalse(await self.runtime.handle_message(stale_request))


class BackendContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_common_backend_contract(self):
        backend = Backend("robot-9", 4)
        await backend.apply_configuration({})
        await backend.start()
        self.assertEqual(await backend.status(), BackendStatus())
        await backend.reconnect()
        await backend.neutralize()
        backend.subscribe(lambda status: status)
        await backend.stop()


class NeutralizationTest(unittest.IsolatedAsyncioTestCase):
    async def make_runtime(self, result):
        events = []
        backend = NeutralizingBackend("417", 1, result)

        async def emit(event, payload):
            events.append((event, payload))

        runtime = ControllerRuntime(
            "controller-a",
            emit,
            BackendRegistry(lambda robot: backend),
            clock=lambda: "2026-01-01T00:00:01Z",
        )
        await runtime.apply_configuration(configure_command())
        events.clear()
        return runtime, backend, events

    async def test_success_is_truthful_and_duplicate_is_idempotent(self):
        runtime, backend, events = await self.make_runtime("succeeded")
        command = neutralize_command()
        self.assertEqual(await runtime.neutralize(command), "succeeded")
        self.assertEqual(await runtime.neutralize(command), "succeeded")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(events[-1][1]["request_id"], "neutralize-1")

    async def test_unsupported_failure_stale_and_conflicting_duplicate(self):
        runtime, backend, _ = await self.make_runtime("unsupported")
        self.assertEqual(await runtime.neutralize(neutralize_command()), "unsupported")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(
            await runtime.neutralize(neutralize_command(reason="different")),
            "rejected",
        )
        self.assertEqual(backend.calls, 1)
        self.assertEqual(
            await runtime.neutralize(
                neutralize_command(request_id="stale", connection_epoch="old")
            ),
            "rejected",
        )

        failed, _, _ = await self.make_runtime(RuntimeError("cannot prove"))
        self.assertEqual(await failed.neutralize(neutralize_command()), "failed")

    async def test_reservation_readiness_is_checked_against_backend(self):
        runtime, backend, events = await self.make_runtime("unsupported")
        command = {
            **neutralize_command(),
            "request_id": "readiness-1",
            "reservation_id": "reservation-1",
        }
        self.assertTrue(await runtime.confirm_readiness(command))
        self.assertEqual(events[-1][0], "robot.readiness_confirmation")
        backend._status.ready = False
        self.assertFalse(await runtime.confirm_readiness(command))


class PlatformFixtureIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_platform_fixture_applies_with_exact_cross_language_digest(self):
        fixture_path = (
            Path(__file__).resolve().parents[4]
            / "groundbreaking-platform"
            / "tests"
            / "Fixtures"
            / "runtime-config-v2.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        events = []

        async def emit(event, payload):
            events.append((event, payload))

        runtime = ControllerRuntime(
            fixture["projection"]["controller_id"],
            emit,
            BackendRegistry(lambda robot: Backend(str(robot["robot_id"]), robot["seat"])),
        )
        command = {
            **fixture["projection"],
            "connection_epoch": "fixture-epoch",
            "request_id": "fixture-request",
            "config_revision": fixture["digest"],
            "config_digest": fixture["digest"],
            "runtime_config": fixture["projection"],
        }
        self.assertTrue(await runtime.apply_configuration(command))
        self.assertEqual(events[0][0], "controller.configuration_applied")
        self.assertEqual(events[0][1]["config_digest"], fixture["digest"])
        self.assertEqual(events[1][0], "controller.status_snapshot")
        self.assertEqual(events[1][1]["robots"][0]["robot_id"], "417")
