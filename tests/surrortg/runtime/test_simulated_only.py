import os
import unittest
from unittest.mock import patch

from surrortg.runtime import BackendRegistry, SimulatedRobotBackend


class SimulatedOnlyTest(unittest.TestCase):
    def test_physical_unknown_or_missing_kind_is_rejected_before_factory(self):
        called = []
        registry = BackendRegistry(lambda robot: called.append(robot))
        with patch.dict(os.environ, {'SIMULATED_ONLY': 'true'}):
            for kind in ('physical', 'unknown', None):
                with self.assertRaisesRegex(ValueError, 'Simulation-only'):
                    registry.create({'robot_id': 'r', 'seat': 0, 'implementation_kind': kind})
        self.assertEqual(called, [])

    def test_simulated_fixture_is_allowed_without_starting_it(self):
        with patch.dict(os.environ, {'SIMULATED_ONLY': 'true'}):
            backend = BackendRegistry().create({'robot_id': 'r', 'seat': 0, 'implementation_kind': 'simulated'})
        self.assertIsInstance(backend, SimulatedRobotBackend)
