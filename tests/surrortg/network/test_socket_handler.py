import unittest
from unittest.mock import patch

from surrortg.network.socket_handler import (
    SOCKETIO_NAMESPACE,
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


if __name__ == "__main__":
    unittest.main()
