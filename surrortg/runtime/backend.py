import asyncio
import logging
import math
import re
import struct
from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class BackendStatus:
    reachable: bool = False
    ready: bool = False
    faults: list[dict] = field(default_factory=list)


class Backend:
    """Minimal implementation-neutral Robot backend contract."""

    def __init__(self, robot_id: str, seat: int):
        self.robot_id = robot_id
        self.seat = seat
        self._status = BackendStatus()

    async def apply_configuration(self, runtime_config: dict) -> None:
        self.runtime_config = dict(runtime_config)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def status(self) -> BackendStatus:
        return self._status

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    async def neutralize(self) -> str:
        """Return a truthful terminal status; generic backends cannot prove it."""
        return "unsupported"

    async def apply_control(self, control: dict) -> bool:
        """Optional normalized input sink; legacy physical inputs remain in GameIO."""
        return False

    def subscribe(self, callback) -> None:
        self._status_callback = callback


class TcpRobotBackend(Backend):
    """Canonical owner of an ESP32 SRTG TCP command connection.

    Readiness requires a validated firmware PING response.  Neutralization is
    deliberately transport-level: a drained STOP frame is success, not proof
    that physical actuators reached neutral.
    """

    PING = bytes((200, 0))
    STOP = bytes((0xFF, 0))
    BATTERY_STATUS = 100
    BATTERY_REQUEST = bytes((BATTERY_STATUS, 0))
    COMMANDS = {"lift": 5, "tilt": 7}
    MULTIPLIERS = {
        "steering": 0.2,
        "throttle": 0.5,
        "lift": 1.0,
        "tilt": 1.0,
    }

    def __init__(
        self,
        robot_id: str,
        seat: int,
        connector=None,
        *,
        connect_timeout=5,
        response_timeout=2,
        heartbeat_interval=2,
        battery_interval=10,
        reconnect_initial=0.1,
        reconnect_max=5,
    ):
        super().__init__(robot_id, seat)
        if connector is None:
            async def connector(address):
                from surrortg.tcp_transport import (
                    BOT_TCP_PORT,
                    open_tcp_endpoint,
                )

                return await open_tcp_endpoint(address, BOT_TCP_PORT)

        self._connector = connector
        self._endpoint = None
        self._running = False
        self._monitor_task = None
        self._write_lock = asyncio.Lock()
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._heartbeat_interval = heartbeat_interval
        self._battery_interval = battery_interval
        self._next_battery_poll = None
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self.battery = None

    async def apply_configuration(self, runtime_config: dict) -> None:
        await super().apply_configuration(runtime_config)
        self.address = runtime_config.get("address")
        if not isinstance(self.address, str) or not self.address:
            raise ValueError("TCP Robot requires runtime_config.address")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._connect_and_validate()
        self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        self._running = False
        task, self._monitor_task = self._monitor_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._endpoint is not None and self._status.ready:
            await self.neutralize()
        await self._close_endpoint()
        self._endpoint = None
        await self._set_status(False, False)

    async def apply_control(self, control: dict) -> bool:
        if not self._running or not self._status.ready or self._endpoint is None:
            return False
        try:
            input_id = control["id"]
            command = control["command"]
            if input_id in {"joystick_main", "drive_joystick"}:
                frames = (
                    self._frame(3, command["x"], "steering"),
                    self._frame(1, command["y"], "throttle"),
                )
            elif input_id in self.COMMANDS:
                frames = (
                    self._frame(
                        self.COMMANDS[input_id], command["val"], input_id
                    ),
                )
            else:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return await self._write(b"".join(frames))

    async def neutralize(self) -> str:
        if self._endpoint is None or not self._status.ready:
            return "rejected"
        return "succeeded" if await self._write(self.STOP) else "failed"

    async def _connect_and_validate(self):
        try:
            endpoint = await asyncio.wait_for(
                self._connector(self.address), self._connect_timeout
            )
            if endpoint is None:
                raise ConnectionError("TCP connection failed")
            self._endpoint = endpoint
            if not await self._write(self.PING, mark_failed=False):
                raise ConnectionError("PING write failed")
            response = await asyncio.wait_for(
                endpoint.receive_exactly(2), self._response_timeout
            )
            if response != self.PING:
                raise ValueError("invalid PING response")
            self._next_battery_poll = (
                asyncio.get_running_loop().time() + self._battery_interval
            )
            await self._set_status(True, True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            diagnostic = self._exception_diagnostic(error)
            logging.warning(
                "runtime > Robot %s seat=%s TCP connection failed: %s",
                self.robot_id,
                self.seat,
                diagnostic,
            )
            await self._close_endpoint()
            await self._set_status(
                False,
                False,
                self._fault("backend_connection_failed", diagnostic),
            )
            return False

    async def _monitor(self):
        delay = self._reconnect_initial
        while self._running:
            if not self._status.ready:
                await asyncio.sleep(delay)
                if await self._connect_and_validate():
                    delay = self._reconnect_initial
                else:
                    delay = min(self._reconnect_max, max(delay * 2, delay))
                continue
            try:
                await asyncio.sleep(self._heartbeat_interval)
                now = asyncio.get_running_loop().time()
                request = self.PING
                if now >= self._next_battery_poll:
                    request = self.BATTERY_REQUEST + request
                    self._next_battery_poll = now + self._battery_interval
                if not await self._write(request):
                    continue
                await self._read_until_ping()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._connection_lost(str(error))

    async def _read_until_ping(self):
        while True:
            command = await asyncio.wait_for(
                self._endpoint.receive_exactly(1), self._response_timeout
            )
            command_id = command[0]
            if command_id == self.BATTERY_STATUS:
                payload = await asyncio.wait_for(
                    self._endpoint.receive_exactly(8), self._response_timeout
                )
                voltage, soc = struct.unpack("<ff", payload)
                if not (math.isfinite(voltage) and math.isfinite(soc)):
                    raise ValueError("malformed battery response")
                self.battery = {
                    "voltage": voltage,
                    "soc": soc,
                }
                continue
            value = await asyncio.wait_for(
                self._endpoint.receive_exactly(1), self._response_timeout
            )
            if command_id != self.PING[0] or value != self.PING[1:]:
                raise ValueError("invalid PING response")
            return

    async def _write(self, data, mark_failed=True):
        endpoint = self._endpoint
        if endpoint is None:
            return False
        try:
            async with self._write_lock:
                result = await endpoint.send(data)
            if result is False:
                raise ConnectionError("TCP write failed")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if mark_failed:
                await self._connection_lost(str(error))
            return False

    async def _connection_lost(self, message):
        await self._close_endpoint()
        await self._set_status(
            False,
            False,
            self._fault("backend_connection_lost", message),
        )

    async def _close_endpoint(self):
        endpoint, self._endpoint = self._endpoint, None
        if endpoint is not None:
            try:
                await endpoint.close()
            except Exception:
                pass

    async def _set_status(self, reachable, ready, fault=None):
        new = BackendStatus(
            reachable=reachable,
            ready=reachable and ready,
            faults=[fault] if fault else [],
        )
        changed = new != self._status
        self._status = new
        callback = getattr(self, "_status_callback", None)
        if changed and callback:
            result = callback(deepcopy(new))
            if asyncio.iscoroutine(result):
                await result

    @staticmethod
    def _fault(code, message):
        return {
            "fault_id": "physical-tcp",
            "code": code,
            "severity": "transient",
            "message": message,
        }

    @staticmethod
    def _exception_diagnostic(error):
        """Return bounded error detail without common credential forms."""
        message = " ".join(str(error).split()) or "no detail"
        message = re.sub(
            r"(?i)\b(password|token|secret|credential)\s*[=:]\s*\S+",
            r"\1=<redacted>",
            message,
        )
        message = re.sub(
            r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@",
            r"\1<redacted>@",
            message,
        )
        return f"{type(error).__name__}: {message[:300]}"

    @classmethod
    def _frame(cls, command_id, value, multiplier):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("control value must be numeric")
        value = float(value)
        if not math.isfinite(value) or value < -1 or value > 1:
            raise ValueError("control value outside normalized range")
        scaled = value * cls.MULTIPLIERS[multiplier]
        return bytes((command_id, int(100 + scaled * 100)))
