import unittest
from pathlib import Path

from surrortg import get_config
from surrortg.runtime import BackendRegistry


class DevelopmentBootstrapTest(unittest.IsolatedAsyncioTestCase):
    def test_local_configuration_uses_current_admission_identity(self):
        root = Path(__file__).resolve().parents[3]
        config = get_config(str(root / 'configs/development/srtg.example.toml'))
        self.assertEqual(config['device_id'], 'dev-controller-001')
        self.assertEqual(config['game_engine']['id'], '1')
        self.assertEqual(config['game_engine']['url'], 'http://127.0.0.1:3000/signaling')
        self.assertNotIn('sources', config)

    async def test_unconfigured_software_backend_cannot_claim_readiness_or_safety(self):
        backend = BackendRegistry().create({
            'robot_id': '1', 'seat': 0, 'implementation_kind': 'simulated',
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
