import unittest

from surrortg.runtime.controller_runtime import ControllerRuntime


class LocalRuntimeStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_connected_admitted_robot_status_is_sanitized(self):
        async def emit(event, payload):
            pass

        runtime = ControllerRuntime("controller-a", emit)
        runtime.game_id = "12"
        runtime.connection_epoch = "epoch-1"
        runtime.applied_config_revision = "revision"
        runtime.applied_config_digest = "digest"
        runtime.robots = {"robot-a": {"seat": 0, "backend": object()}}
        runtime._observations = {"robot-a": {
            "reachable": True,
            "ready": False,
            "faults": {"fault-a": {
                "fault_id": "fault-a",
                "code": "blocked",
                "severity": "transient",
                "message": "diagnostic detail must stay private",
            }},
        }}

        status = await runtime.local_status(True)
        self.assertEqual(status["admission"]["state"], "admitted")
        self.assertEqual(status["applied_configuration"]["revision"], "revision")
        self.assertTrue(status["robots"][0]["backend_reachable"])
        self.assertFalse(status["robots"][0]["ready"])
        self.assertNotIn("message", status["robots"][0]["faults"][0])

    async def test_disconnected_and_not_admitted_are_explicit(self):
        async def emit(event, payload):
            pass

        status = await ControllerRuntime("controller-a", emit).local_status(False)
        self.assertEqual(status["transport"]["state"], "disconnected")
        self.assertEqual(status["admission"]["state"], "not_admitted")
        self.assertEqual(status["robots"], [])
