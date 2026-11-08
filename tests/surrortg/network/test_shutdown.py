import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import socketio
from aiohttp import web

from surrortg.game import Game
from surrortg.network.socket_handler import SocketHandler
from surrortg.runtime.controller_runtime import ControllerRuntime


class TransportShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = socketio.AsyncServer(async_mode="aiohttp")
        app = web.Application()
        server.attach(app)
        server.on(
            "connect", lambda sid, environ, auth: True, namespace="/signaling"
        )
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.handler = SocketHandler(f"http://127.0.0.1:{port}")
        self.handler.local_socket_handler = None
        self.server = server

    async def asyncTearDown(self):
        await self.handler.shutdown()
        await self.server.shutdown()
        await self.runner.cleanup()

    async def test_cancelled_run_closes_real_session_and_engineio_tasks(self):
        connected = asyncio.Event()
        self.handler.socketio_namespace.on_connect_handler = connected.set
        run_task = asyncio.create_task(self.handler.run())
        await asyncio.wait_for(connected.wait(), 2)
        client = self.handler.socketio_namespace.sio
        # Let _connect finish and wait() start awaiting the Engine.IO reader.
        await asyncio.sleep(0.05)
        reader = client.eio.read_loop_task
        writer = client.eio.write_loop_task
        session = client.eio.http
        run_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, 2)
        self.assertTrue(session.closed)
        self.assertTrue(reader.done())
        self.assertFalse(reader.cancelled())
        self.assertTrue(writer.done())
        await self.handler.shutdown()  # repeated cleanup is harmless

    async def test_interrupted_connect_closes_partial_http_session(self):
        started = asyncio.Event()
        sessions = []

        async def interrupted_connect(client, *args, **kwargs):
            client.eio.http = aiohttp.ClientSession()
            sessions.append(client.eio.http)
            started.set()
            await asyncio.Future()

        with patch.object(
            socketio.AsyncClient, "connect", interrupted_connect
        ):
            task = asyncio.create_task(self.handler.run())
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(sessions[0].closed)

    async def test_failed_attempt_is_closed_before_retry(self):
        sessions = []
        retried = asyncio.Event()

        async def failed_connect(client, *args, **kwargs):
            if sessions:
                self.assertTrue(sessions[-1].closed)
                retried.set()
            client.eio.http = aiohttp.ClientSession()
            sessions.append(client.eio.http)
            raise OSError("connection failed")

        with patch.object(
            socketio.AsyncClient, "connect", failed_connect
        ), patch(
            "surrortg.network.socket_handler.SOCKETIO_CONNECTION_MIN_SLEEP", 0
        ):
            task = asyncio.create_task(self.handler.run())
            await asyncio.wait_for(retried.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(session.closed for session in sessions))

    async def test_cancellation_does_not_hide_transport_cleanup_failure(self):
        started = asyncio.Event()

        async def run():
            started.set()
            try:
                await asyncio.Future()
            finally:
                raise RuntimeError("transport cleanup failed")

        self.handler.socketio_namespace.run = run
        task = asyncio.create_task(self.handler.run())
        await started.wait()
        task.cancel()
        with self.assertRaisesRegex(RuntimeError, "transport cleanup failed"):
            await task

    async def test_unexpected_disconnect_failure_propagates(self):
        namespace = self.handler.socketio_namespace
        session = aiohttp.ClientSession()
        client = socketio.AsyncClient()
        client.eio.http = session
        client.shutdown = AsyncMock(side_effect=RuntimeError("close failed"))
        namespace.sio = client
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await namespace.shutdown()
        self.assertTrue(session.closed)


class GameShutdownTest(unittest.IsolatedAsyncioTestCase):
    def make_game(self):
        game = Game()
        game._exit_reason = -1
        game._exception = None
        game._current_ge_task = None
        self.events = []

        def action(name):
            async def run(*args):
                self.events.append(name)

            return AsyncMock(side_effect=run)

        self.started = asyncio.Event()

        async def block():
            self.started.set()
            await asyncio.Future()

        game._io = SimpleNamespace(
            _socket_handler=SimpleNamespace(
                run=block, shutdown=action("socket shutdown")
            ),
            management_server=SimpleNamespace(
                run=AsyncMock(side_effect=block),
                close=action("management close"),
            ),
            shutdown_inputs=action("inputs shutdown"),
            controller_runtime=SimpleNamespace(shutdown=action("neutralize")),
        )
        game.on_exit = action("on_exit")
        return game

    def assert_cleanup_order(self):
        self.assertEqual(
            self.events,
            [
                "on_exit",
                "inputs shutdown",
                "neutralize",
                "socket shutdown",
                "management close",
            ],
        )

    async def test_normal_signal_shutdown_preserves_safety_order(self):
        game = self.make_game()
        task = asyncio.create_task(game._main())
        await self.started.wait()
        game._exit_signal_handler(2)
        await task
        self.assert_cleanup_order()
        self.assertTrue(game._main_task.done())
        self.assertTrue(game._management_task.done())

    async def test_interrupted_startup_cleans_up_and_propagates_cancellation(
        self,
    ):
        game = self.make_game()
        game.on_init = self.io_startup
        task = asyncio.create_task(game._main())
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_cleanup_order()
        self.assertFalse(game.io._can_register_inputs)
        self.assertFalse(game.io._can_register_configs)

    async def io_startup(self):
        self.started.set()
        await asyncio.Future()

    async def test_signal_during_startup_runs_safety_cleanup(self):
        game = self.make_game()
        game.on_init = self.io_startup
        task = asyncio.create_task(game._main())
        await self.started.wait()
        game._exit_signal_handler(1)
        await task
        self.assert_cleanup_order()
        self.assertEqual(game._exit_reason, 1)

    async def test_startup_failure_is_preserved_after_cleanup(self):
        game = self.make_game()
        error = RuntimeError("startup failed")
        game.on_init = AsyncMock(side_effect=error)
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            await game._main()
        self.assertIs(game._exception, error)
        self.assertEqual(game._exit_reason, 0)
        self.assert_cleanup_order()

    async def test_on_exit_failure_still_neutralizes_and_closes(self):
        game = self.make_game()
        game.on_init = AsyncMock(side_effect=RuntimeError("startup failed"))
        game.on_exit = AsyncMock(side_effect=RuntimeError("on_exit failed"))
        with self.assertRaisesRegex(RuntimeError, "on_exit failed"):
            await game._main()
        self.assertEqual(
            self.events,
            [
                "inputs shutdown",
                "neutralize",
                "socket shutdown",
                "management close",
            ],
        )


class RuntimeShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_joins_gameplay_and_telemetry_tasks(self):
        runtime = ControllerRuntime("controller", AsyncMock())
        tasks = [asyncio.create_task(asyncio.sleep(999)) for _ in range(2)]
        runtime._gameplay_task, runtime._telemetry_task = tasks
        await runtime.shutdown()
        self.assertTrue(all(task.cancelled() for task in tasks))

    async def test_shutdown_exposes_unexpected_task_failure(self):
        runtime = ControllerRuntime("controller", AsyncMock())

        async def fail():
            raise RuntimeError("telemetry failed")

        runtime._telemetry_task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "telemetry failed"):
            await runtime.shutdown()
