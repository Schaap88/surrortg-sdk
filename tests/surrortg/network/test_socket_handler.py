import asyncio
import unittest
import warnings
from unittest.mock import patch

from surrortg.network.socket_handler import (
    SOCKETIO_NAMESPACE,
    SocketHandler,
    SocketioNamespace,
)


class FakeAsyncClient:
    instances = []

    def __init__(self, **kwargs):
        self.options = kwargs
        self.connected = False
        self.connect_calls = []
        self.handlers = {}
        FakeAsyncClient.instances.append(self)

    def event(self, namespace=None):
        def register(handler):
            self.handlers[(namespace, handler.__name__)] = handler
            return handler

        return register

    def register_namespace(self, namespace):
        self.namespace = namespace
        namespace._set_client(self)

    async def connect(self, url, **kwargs):
        self.connect_calls.append((url, kwargs))
        self.connected = True
        await self.namespace.on_connect()

    async def disconnect(self):
        self.connected = False


class SocketioNamespaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAsyncClient.instances.clear()

    async def test_modern_client_connects_with_namespace_and_identity(self):
        connected = []
        namespace = SocketioNamespace(
            SOCKETIO_NAMESPACE,
            "https://signaling.example/signaling",
            {
                "clientType": "controller",
                "clientId": "controller a",
                "gameId": "12",
                "token": "a&b",
            },
            lambda message: None,
            lambda: connected.append(True),
            lambda: None,
            None,
            None,
        )

        with patch(
            "surrortg.network.socket_handler.socketio.AsyncClient",
            FakeAsyncClient,
        ):
            await namespace._connect()

        client = FakeAsyncClient.instances[0]
        self.assertTrue(client.options["reconnection"])
        self.assertEqual(connected, [True])
        url, options = client.connect_calls[0]
        self.assertEqual(
            url,
            "https://signaling.example?clientType=controller&clientId="
            "controller+a&gameId=12&token=a%26b",
        )
        self.assertEqual(options["namespaces"], [SOCKETIO_NAMESPACE])
        self.assertEqual(options["transports"], ["websocket"])

    async def test_disconnect_callback_runs_before_reconnect(self):
        lifecycle = []
        namespace = SocketioNamespace(
            SOCKETIO_NAMESPACE,
            "https://signaling.example",
            {},
            lambda message: None,
            lambda: lifecycle.append("connected"),
            lambda: lifecycle.append("disconnected"),
            None,
            None,
        )

        await namespace.on_connect()
        await namespace.on_disconnect("transport error")
        await namespace.on_connect()

        self.assertEqual(
            lifecycle, ["connected", "disconnected", "connected"]
        )
        self.assertTrue(namespace.connected)


class SocketHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_schedules_both_handlers_and_works_on_python_313(self):
        handler = SocketHandler("https://signaling.example")

        async def socketio_run():
            await asyncio.sleep(0.01)

        async def local_run():
            await asyncio.sleep(0.02)

        handler.socketio_namespace.run = socketio_run
        handler.local_socket_handler.run = local_run

        run_task = asyncio.create_task(handler.run())
        await asyncio.sleep(0)

        self.assertEqual(len(handler._run_tasks), 2)
        self.assertTrue(all(task.done() is False for task in handler._run_tasks))

        await asyncio.wait_for(run_task, timeout=1)

    async def test_shutdown_cancels_pending_run_tasks(self):
        handler = SocketHandler("https://signaling.example")

        async def block():
            await asyncio.sleep(999)

        handler._run_tasks = [
            asyncio.create_task(block()),
            asyncio.create_task(block()),
        ]

        await handler.shutdown()

        self.assertTrue(all(task.done() for task in handler._run_tasks))
        self.assertListEqual(handler._run_tasks, [])

    async def test_child_exception_is_propagated_without_unawaited_coroutine_warning(self):
        handler = SocketHandler("https://signaling.example")

        async def socketio_run():
            raise RuntimeError("socketio boom")

        async def local_run():
            await asyncio.sleep(999)

        handler.socketio_namespace.run = socketio_run
        handler.local_socket_handler.run = local_run

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            with self.assertRaisesRegex(RuntimeError, "socketio boom"):
                await handler.run()


if __name__ == "__main__":
    unittest.main()
