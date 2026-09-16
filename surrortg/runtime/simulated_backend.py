import logging
import math
from copy import deepcopy

from .backend import Backend, BackendStatus


class SimulatedRobotBackend(Backend):
    """Software outputs using the normal {id, type, command} input envelope.

    Numeric command values have neutral value zero, booleans false. Unsupported
    command shapes are rejected rather than inventing an actuating default.
    Neutralization retains each input's shape and zeroes every output.
    """

    def __init__(self, robot_id, seat):
        super().__init__(robot_id, seat)
        self.started = False
        self.neutralized = True
        self.last_control = None
        self.outputs = {}

    async def start(self):
        self.started = True
        await self.set_state(reachable=True, ready=True)

    async def stop(self):
        self.started = False
        self.outputs = self._neutral(self.outputs)
        self.last_control = None
        self.neutralized = True
        await self.set_state(reachable=False, ready=False)

    async def set_state(self, *, reachable, ready, faults=()):
        """Deterministic fault injection/recovery through ordinary observations."""
        self._status = BackendStatus(
            reachable=self.started and reachable is True,
            ready=self.started and reachable is True and ready is True and not faults,
            faults=deepcopy(list(faults)),
        )
        if not self._status.ready:
            self.outputs = self._neutral(self.outputs)
            self.neutralized = True
            self.last_control = None
        callback = getattr(self, "_status_callback", None)
        if callback:
            await callback(await self.status())

    async def status(self):
        return deepcopy(self._status)

    async def apply_control(self, control):
        if not self.started or not self._status.ready or not self._status.reachable or self._status.faults:
            return False
        if not isinstance(control, dict) or not isinstance(control.get("id"), str) or not control["id"] or not isinstance(control.get("command"), dict):
            return False
        try:
            self._neutral(control["command"])
        except ValueError:
            return False
        # A fresh routed command activates outputs after neutralization; ownership
        # is enforced by ControllerRuntime's current peer/seat routing.
        self.last_control = deepcopy(control)
        self.outputs[control["id"]] = deepcopy(control["command"])
        self.neutralized = self.outputs == self._neutral(self.outputs)
        logging.info("runtime > Robot %s control applied outputs=%s", self.robot_id, self.outputs)
        return True

    async def neutralize(self):
        if not self.started or not self._status.reachable:
            return "rejected"
        self.outputs = self._neutral(self.outputs)
        self.last_control = None
        self.neutralized = self.outputs == self._neutral(self.outputs)
        status = "succeeded" if self.neutralized else "failed"
        logging.info("runtime > Robot %s neutralization %s outputs=%s", self.robot_id, status, self.outputs)
        return status

    @classmethod
    def _neutral(cls, value):
        if isinstance(value, dict):
            return {key: cls._neutral(item) for key, item in value.items()}
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)) and math.isfinite(value):
            return 0
        raise ValueError("Simulated outputs require finite numeric or boolean command values")
