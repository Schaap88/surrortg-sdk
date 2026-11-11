import asyncio
import inspect
import logging
import math
import time
from copy import deepcopy

from .backend import Backend, BackendStatus


class SimulatedRobotBackend(Backend):
    """Software outputs using the normal {id, type, command} input envelope.

    Numeric command values have neutral value zero, booleans false. Unsupported
    command shapes are rejected rather than inventing an actuating default.
    Neutralization retains each input's shape and zeroes every output.
    """

    def __init__(self, robot_id, seat, world=None):
        super().__init__(robot_id, seat)
        self.started = False
        self.neutralized = True
        self.last_control = None
        self.outputs = {}
        self.world = world
        self._telemetry_callback = None
        self._telemetry_task = None
        self._charge = 100.0
        self._charge_updated_at = None

    def subscribe_telemetry(self, callback):
        self._telemetry_callback = callback

    def _largest_input(self):
        def values(value):
            if isinstance(value, dict):
                return max((values(item) for item in value.values()), default=0)
            return abs(value) if not isinstance(value, bool) else 0
        return min(1.0, values(self.outputs))

    def _update_charge(self):
        now = time.monotonic()
        if self.started and self._charge_updated_at is not None and self._largest_input():
            self._charge = max(5.0, self._charge - (now - self._charge_updated_at) / 60)
        self._charge_updated_at = now

    def _power_observation(self):
        self._update_charge()
        stalled = self._largest_input() and self.world and self.world.is_pinned(self.robot_id)
        return {"kind": "power", "power": {
            "version": 1, "validity": 7, "voltage_mv": 7400,
            "state_of_charge_percent": round(self._charge, 2),
            "current_ma": 3800 if stalled else round(200 + 1200 * self._largest_input()),
            "power_mw": None, "dock_state": "unknown",
            "fuel_gauge_age_ms": 0, "current_age_ms": 0, "dock_age_ms": None,
        }}

    async def _emit_telemetry(self):
        while self.started:
            await asyncio.sleep(1)
            observation = self._power_observation()
            if self._telemetry_callback:
                try:
                    result = self._telemetry_callback(observation)
                    if inspect.isawaitable(result):
                        await result
                except Exception as error:
                    logging.warning("runtime > Simulated telemetry failed: %s", type(error).__name__)

    async def start(self):
        if self.started:
            return
        self._charge_updated_at = time.monotonic()
        self.started = True
        self.world and self.world.add(self.robot_id, self.seat, owner=self)
        await self.set_state(reachable=True, ready=True)
        self._telemetry_task = asyncio.create_task(self._emit_telemetry())

    async def stop(self):
        self._update_charge()
        self.started = False
        task, self._telemetry_task = self._telemetry_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.outputs = self._neutral(self.outputs)
        self.last_control = None
        self.neutralized = True
        self.world and self.world.remove(self.robot_id, owner=self)
        await self.set_state(reachable=False, ready=False)

    async def set_state(self, *, reachable, ready, faults=()):
        """Deterministic fault injection/recovery through ordinary observations."""
        self._update_charge()
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
        self._update_charge()
        self.last_control = deepcopy(control)
        self.outputs[control["id"]] = deepcopy(control["command"])
        self.world and self.world.control(self.robot_id, control, owner=self)
        self.neutralized = self.outputs == self._neutral(self.outputs)
        logging.info("runtime > Robot %s control applied outputs=%s", self.robot_id, self.outputs)
        return True

    async def neutralize(self):
        if not self.started or not self._status.reachable:
            return "rejected"
        self._update_charge()
        self.outputs = self._neutral(self.outputs)
        self.last_control = None
        self.neutralized = self.outputs == self._neutral(self.outputs)
        self.world and self.world.neutralize(self.robot_id, owner=self)
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
