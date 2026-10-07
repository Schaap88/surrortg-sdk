"""Narrow local status/reconnect management IPC."""

import asyncio
import json
import os
from pathlib import Path


DEFAULT_SOCKET_PATH = Path("/run/srtg/controller-management.sock")
IO_TIMEOUT = 2


def default_socket_path():
    """Use systemd's runtime directory, or a private rootless fallback."""
    production = DEFAULT_SOCKET_PATH.parent
    if production.is_dir() and os.access(production, os.W_OK):
        return DEFAULT_SOCKET_PATH
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    private = runtime_root / f"srtg-{os.getuid()}"
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    return private / DEFAULT_SOCKET_PATH.name


async def _close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=IO_TIMEOUT)
    except (OSError, asyncio.TimeoutError, ConnectionError):
        pass


class ManagementServer:
    def __init__(self, status_provider, reconnect, socket_path=None):
        self.status_provider = status_provider
        self.reconnect = reconnect
        self.socket_path = (
            Path(socket_path) if socket_path else default_socket_path()
        )
        self.server = None

    async def run(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o660)
        try:
            await self.server.serve_forever()
        except asyncio.CancelledError:
            raise
        finally:
            await self.close()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _handle(self, reader, writer):
        response = None
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readline(), timeout=IO_TIMEOUT
                )
                request = json.loads(raw.decode("utf-8"))
                command = request.get("command")
                if command == "status":
                    response = {
                        "ok": True,
                        "status": await self.status_provider(),
                    }
                elif command == "reconnect":
                    response = {
                        "ok": True,
                        "reconnect": await self.reconnect(),
                    }
                else:
                    response = {"ok": False, "error": "unsupported_command"}
            except Exception:
                response = {"ok": False, "error": "invalid_request"}
            writer.write(
                json.dumps(response, separators=(",", ":")).encode() + b"\n"
            )
            await asyncio.wait_for(writer.drain(), timeout=IO_TIMEOUT)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await _close_writer(writer)
            except asyncio.CancelledError:
                # close() above is synchronous and sufficient during shutdown.
                pass


async def management_request(socket_path, command, timeout=1):
    """Return explicit runtime-unavailable state instead of raising IPC errors."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)), timeout=timeout
        )
        writer.write(json.dumps({"command": command}).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        raw = await asyncio.wait_for(reader.readline(), timeout)
        response = json.loads(raw.decode())
        await _close_writer(writer)
        writer = None
        return response
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
        return {
            "ok": False,
            "error": "runtime_unavailable",
            "status": {"runtime": {"reachable": False, "state": "unavailable"}},
        }
    finally:
        if writer is not None:
            await _close_writer(writer)
