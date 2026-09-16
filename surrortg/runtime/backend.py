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
    """Existing TCP endpoint adapted behind canonical Robot identity."""

    def __init__(self, robot_id: str, seat: int, connector=None):
        super().__init__(robot_id, seat)
        if connector is None:
            async def connector(address):
                from surrortg.devices.tcp.tcp_bot import BOT_TCP_PORT
                from surrortg.devices.tcp.tcp_protocol import open_tcp_endpoint

                return await open_tcp_endpoint(address, BOT_TCP_PORT)

        self._connector = connector
        self._endpoint = None

    async def apply_configuration(self, runtime_config: dict) -> None:
        await super().apply_configuration(runtime_config)
        self.address = runtime_config.get("address")
        if not isinstance(self.address, str) or not self.address:
            raise ValueError("TCP Robot requires runtime_config.address")

    async def start(self) -> None:
        try:
            self._endpoint = await self._connector(self.address)
            connected = self._endpoint is not None
            self._status = BackendStatus(reachable=connected, ready=connected)
        except Exception as error:
            self._status = BackendStatus(
                faults=[{
                    "fault_id": "backend-unreachable",
                    "code": "backend_connection_failed",
                    "severity": "transient",
                    "message": str(error),
                }]
            )

    async def stop(self) -> None:
        if self._endpoint is not None:
            await self._endpoint.close()
        self._endpoint = None
        self._status.reachable = False
        self._status.ready = False
