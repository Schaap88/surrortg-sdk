import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from surrortg.runtime.controller_runtime import ControllerRuntime


class GameplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_bound_event_retries_with_same_identifier_and_ack_stops_delivery(self):
        events = []

        async def emit(event, payload):
            events.append((event, dict(payload)))
            if len(events) == 1:
                raise ConnectionError('temporary')

        runtime = ControllerRuntime('c', emit)
        runtime.game_id = '1'
        runtime.connection_epoch = 'epoch'
        runtime.applied_config_revision = 'rev'
        runtime.seats = {0: '1'}
        runtime.robots = {'1': {'backend': SimpleNamespace(neutralize=AsyncMock(), stop=AsyncMock()), 'implementation_kind': 'simulated'}}
        self.assertIsNone(runtime.objective_completed(0, 'first_construction_task'))
        await runtime.handle_message(SimpleNamespace(event='newPeer', src='gameEngine', dst='c', payload={
            'id': 'p', 'seat': 0, 'robot_id': '1', 'session_id': 'session', 'allocation_id': 'session'}))
        await runtime.handle_message(SimpleNamespace(event='enableRouting', src='gameEngine', dst='c', payload={'seat': 0}))
        event_id = runtime.objective_completed(0, 'first_construction_task')
        await asyncio.sleep(.6)
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0], events[1])
        self.assertEqual(events[0][1]['event_id'], event_id)
        await runtime.handle_message(SimpleNamespace(event='gameplay.ack', src='gameEngine', payload={'event_id': event_id, 'status': 'retryable'}))
        self.assertIn(event_id, runtime._gameplay_pending)
        await runtime.handle_message(SimpleNamespace(event='gameplay.ack', src='gameEngine', payload={'event_id': event_id, 'status': 'accepted'}))
        self.assertFalse(runtime._gameplay_pending)
        runtime.disconnect()
        self.assertIsNone(runtime.objective_completed(0, 'first_construction_task'))
        await runtime.shutdown()

    async def test_development_trigger_is_opt_in_and_simulated_only(self):
        runtime = ControllerRuntime('c', AsyncMock())
        runtime.seats = {0: '1'}
        runtime.robots = {'1': {'implementation_kind': 'simulated'}}
        runtime._gameplay_bindings[0] = {'session_id': 'session'}
        runtime.objective_completed = lambda seat, key: 'event'
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(runtime.development_complete_objective(0))
        with patch.dict('os.environ', {'GROUNDBREAKING_DEV_COMPLETE_OBJECTIVE': '1'}):
            runtime.robots['1']['implementation_kind'] = 'physical'
            self.assertIsNone(runtime.development_complete_objective(0))
            runtime.robots['1']['implementation_kind'] = 'simulated'
            self.assertEqual(runtime.development_complete_objective(0), 'event')
            self.assertIsNone(runtime.development_complete_objective(0))
