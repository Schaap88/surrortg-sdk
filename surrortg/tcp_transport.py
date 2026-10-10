"""TCP transport primitives without optional hardware dependencies."""

import asyncio
import logging

BOT_TCP_PORT = 31338


class TcpEndpoint:
    """High-level interface for TCP stream endpoints."""

    def __init__(self, reader, writer, host, port):
        self._closed = False
        self._reader = reader
        self._writer = writer
        self._address = host
        self._port = port

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        await self._writer.wait_closed()

    async def send(self, data):
        if self._closed:
            logging.error(f"Endpoint to {self._address} is closed")
            return False
        try:
            self._writer.write(data)
            await self._writer.drain()
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError):
            logging.error(
                f"Could not send data to {self._address}, connection down!"
            )
            return False

    async def receive(self, n=100):
        if self._closed:
            logging.error(f"Endpoint to {self._address} is closed")
            return
        return await self._reader.read(n)

    async def receive_exactly(self, n):
        if self._closed:
            logging.error(f"Endpoint to {self._address} is closed")
            return
        return await self._reader.readexactly(n)

    @property
    def address(self):
        return (self._address, self._port)

    @property
    def closed(self):
        return self._closed


async def open_tcp_endpoint(host, port, timeout=5):
    """Open a TCP endpoint, returning ``None`` for connection failures."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        return TcpEndpoint(reader, writer, host, port)
    except asyncio.TimeoutError:
        logging.error(f"Connection to {host}:{port} timeouted")
        return None
    except OSError:
        logging.error(f"No route to host {host}")
        return None
