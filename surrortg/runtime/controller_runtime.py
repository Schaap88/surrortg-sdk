import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from .backend import BackendStatus
from .backend_registry import BackendRegistry
from .simulated_world import SimulatedWorld

PROTOCOL_VERSION = "2.0"
CONFIGURE = "controller.configure"
SNAPSHOT_REQUEST = "controller.status_snapshot_request"
NEUTRALIZE = "robot.neutralize"
CONFIRM_READINESS = "robot.confirm_readiness"
NEUTRALIZATION_STATUSES = {"succeeded", "failed", "rejected", "unsupported"}


class RuntimeConfigurationError(ValueError):
    pass


class ControllerRuntime:
    def __init__(self, controller_id, emit, backend_registry=None, clock=None, result_cache_ttl=300):
        self.controller_id = str(controller_id)
        self._emit = emit
        self.simulated_world = SimulatedWorld(publish=self._emit_world_snapshot)
        self._backends = backend_registry or BackendRegistry(simulated_world=self.simulated_world)
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        self.robots = {}
        self.seats = {}
        self._observations = {}
        self.snapshot_seq = 0
        self.applied_config_revision = None
        self.applied_config_digest = None
        self.connection_epoch = None
        self.game_id = None
        self._last_operation = None
        self._neutralization_results = {}
        self._result_cache_ttl = result_cache_ttl
        self._peers = {}
        self._enabled_seats = set()

    async def _emit_world_snapshot(self, snapshot):
        await self._emit("simulation.world_snapshot", snapshot)

    async def handle_message(self, message):
        if message.event in {"newPeer", "peerLeft", "enableRouting", "disableRouting", "gameControls"}:
            return await self._handle_controls(message)
        if message.event == CONFIGURE:
            return await self.apply_configuration(message.payload)
        if message.event == SNAPSHOT_REQUEST:
            if message.payload.get("connection_epoch") != self.connection_epoch:
                return False
            if message.payload.get("expected_config_revision") != self.applied_config_revision:
                return False
            await self.emit_snapshot()
            return True
        if message.event == NEUTRALIZE:
            return await self.neutralize(message.payload)
        if message.event == CONFIRM_READINESS:
            return await self.confirm_readiness(message.payload)
        return False

    async def _handle_controls(self, message):
        if message.dst != self.controller_id:
            return False
        payload = message.payload or {}
        if message.event == "gameControls":
            seat = self._peers.get(message.src)
            if seat is None or seat != message.seat or seat not in self._enabled_seats:
                return False
            robot_id = self.robot_id_for_seat(seat)
            if robot_id is None:
                return False
            return await self.robots[robot_id]["backend"].apply_control(payload)
        if message.src != "gameEngine":
            return False
        if message.event == "newPeer" and payload.get("seat") in self.seats:
            self._peers[payload["id"]] = payload["seat"]
        elif message.event == "peerLeft":
            seat = self._peers.pop(payload.get("id"), None)
            if seat is not None:
                await self._neutralize_seats((seat,))
        elif message.event == "enableRouting":
            self._enabled_seats.update([payload["seat"]] if "seat" in payload else self.seats)
        elif message.event == "disableRouting":
            seats = [payload["seat"]] if "seat" in payload else list(self.seats)
            self._enabled_seats.difference_update(seats)
            await self._neutralize_seats(seats)
        return True

    async def confirm_readiness(self, command):
        valid = self._valid_robot_command(command) and command.get("reservation_id")
        reachable = ready = False
        if valid:
            try:
                status = await self.robots[str(command["robot_id"])]["backend"].status()
                reachable = status.reachable is True
                ready = reachable and status.ready is True and not status.faults
            except Exception:
                pass
        result = {key: command.get(key) for key in (
            "protocol_version", "game_id", "controller_id", "connection_epoch",
            "config_revision", "request_id", "reservation_id", "robot_id",
        )}
        result.update({"reachable": reachable, "ready": ready, "confirmed_at": self._clock()})
        await self._emit("robot.readiness_confirmation", result)
        return ready

    async def neutralize(self, command):
        self._expire_neutralization_results()
        request_id = command.get("request_id")
        signature = json.dumps(command, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        cached = self._neutralization_results.get(request_id)
        if cached:
            if cached["signature"] == signature:
                await self._emit("robot.neutralization_result", cached["result"])
                return cached["result"]["status"]
            result = self._neutralization_result(command, "rejected")
            await self._emit("robot.neutralization_result", result)
            return "rejected"
        status = "rejected"
        if self._valid_neutralization(command):
            try:
                status = await self.robots[str(command["robot_id"])]["backend"].neutralize()
                if status not in NEUTRALIZATION_STATUSES:
                    status = "unsupported"
            except Exception:
                status = "failed"
        result = self._neutralization_result(command, status)
        if request_id:
            self._neutralization_results[request_id] = {
                "signature": signature, "result": result, "stored_at": time.monotonic(),
            }
        await self._emit("robot.neutralization_result", result)
        return status

    def _valid_neutralization(self, command):
        required = ("protocol_version", "game_id", "controller_id", "connection_epoch", "config_revision", "request_id", "robot_id", "reason", "deadline_at")
        return all(command.get(key) for key in required) and self._valid_robot_command(command)

    def _valid_robot_command(self, command):
        return command.get("protocol_version") == PROTOCOL_VERSION and str(command.get("game_id")) == self.game_id and str(command.get("controller_id")) == self.controller_id and command.get("connection_epoch") == self.connection_epoch and command.get("config_revision") == self.applied_config_revision and str(command.get("robot_id")) in self.robots

    def _neutralization_result(self, command, status):
        result = {key: command.get(key) for key in ("protocol_version", "game_id", "controller_id", "connection_epoch", "config_revision", "request_id", "robot_id")}
        return {**result, "status": status, "completed_at": self._clock()}

    def _expire_neutralization_results(self):
        cutoff = time.monotonic() - self._result_cache_ttl
        self._neutralization_results = {request_id: cached for request_id, cached in self._neutralization_results.items() if cached["stored_at"] >= cutoff}

    def disconnect(self):
        """Invalidate transport-scoped positive observations."""
        try:
            asyncio.get_running_loop().create_task(
                self._neutralize_seats(tuple(self.seats))
            )
        except RuntimeError:
            pass
        for observation in self._observations.values():
            observation["reachable"] = False
            observation["ready"] = False
        self._peers.clear()
        self._enabled_seats.clear()
        self.connection_epoch = None

    async def shutdown(self):
        """Fail safe and release all configured backends on orderly exit."""
        await self._neutralize_seats(tuple(self.seats))
        for robot in self.robots.values():
            await robot["backend"].stop()

    async def _neutralize_seats(self, seats):
        for seat in set(seats):
            robot_id = self.robot_id_for_seat(seat)
            if robot_id is None:
                continue
            try:
                await self.robots[robot_id]["backend"].neutralize()
            except Exception:
                logging.exception(
                    "runtime > Robot %s neutralization attempt failed", robot_id
                )

    async def local_status(self, transport_connected=False):
        """Return sanitized observations; this is never Game authority."""
        robots = []
        for robot_id, robot in self.robots.items():
            observation = self._observations.get(robot_id, {})
            reachable = observation.get("reachable") is True
            faults = [
                {key: fault[key] for key in (
                    "fault_id", "code", "severity", "observed_at"
                ) if key in fault}
                for fault in observation.get("faults", {}).values()
            ]
            robots.append({
                "robot_id": robot_id,
                "seat": robot["seat"],
                "implementation_kind": robot.get("implementation_kind"),
                "backend_reachable": reachable,
                "ready": reachable and observation.get("ready") is True,
                "faults": faults,
            })
        return {
            "runtime": {"reachable": True, "state": "running"},
            "transport": {
                "connected": bool(transport_connected),
                "state": "connected" if transport_connected else "disconnected",
            },
            "admission": {
                "state": "admitted" if self.connection_epoch else (
                    "pending" if transport_connected else "not_admitted"
                ),
                "game_id": self.game_id,
                "controller_id": self.controller_id,
                "connection_epoch": self.connection_epoch,
            },
            "applied_configuration": {
                "revision": self.applied_config_revision,
                "digest": self.applied_config_digest,
            },
            "robots": robots,
        }

    async def apply_configuration(self, command):
        operation = self._operation_key(command)
        if operation == self._last_operation:
            await self._emit("controller.configuration_applied", self._ack(command))
            await self.emit_snapshot()
            return True

        candidates = {}
        try:
            config = self._validate(command)
            for robot in config["robots"]:
                backend = self._backends.create(robot)
                await backend.apply_configuration(robot["runtime_config"])
                candidates[str(robot["robot_id"])] = {
                    "robot_id": str(robot["robot_id"]),
                    "seat": robot["seat"],
                    "implementation_kind": robot["implementation_kind"],
                    "backend": backend,
                }
        except Exception as error:
            for candidate in candidates.values():
                await candidate["backend"].stop()
            await self._emit("controller.configuration_rejected", {
                **self._ack(command),
                "reason": {"code": "invalid_configuration", "message": str(error)},
            })
            return False

        for candidate in candidates.values():
            try:
                await candidate["backend"].start()
                candidate["status"] = await candidate["backend"].status()
            except Exception as error:
                # Endpoint availability is a Robot fact, not config rejection.
                candidate["status"] = BackendStatus(faults=[{
                    "fault_id": "backend-unreachable",
                    "code": "backend_connection_failed",
                    "severity": "transient",
                    "message": str(error),
                }])

        previous = self.robots
        self.robots = candidates
        self._peers.clear()
        self._enabled_seats.clear()
        self.seats = {robot["seat"]: robot_id for robot_id, robot in candidates.items()}
        self.game_id = str(command["game_id"])
        self.simulated_world.configure(self.game_id, self._emit_world_snapshot)
        self.connection_epoch = command["connection_epoch"]
        self.applied_config_revision = command["config_revision"]
        self.applied_config_digest = command["config_digest"]
        self._last_operation = operation
        self._observations = {robot_id: {"seq": 0, "reachable": False, "ready": False, "faults": {}} for robot_id in candidates}
        initial_statuses = {}
        for robot_id, candidate in candidates.items():
            initial_statuses[robot_id] = candidate.pop("status")
            backend = candidate["backend"]

            async def observed(status, robot_id=robot_id, backend=backend):
                # Discard callbacks from replaced backends or disconnected epochs.
                if self.robots.get(robot_id, {}).get("backend") is backend and self.connection_epoch is not None:
                    await self.update_robot_status(robot_id, status)

            backend.subscribe(observed)
        for old in previous.values():
            await old["backend"].stop()

        await self._emit("controller.configuration_applied", self._ack(command))
        logging.info("runtime > Controller %s configuration applied revision=%s", self.controller_id, self.applied_config_revision)
        for robot_id, status in initial_statuses.items():
            await self.update_robot_status(robot_id, status)
        await self.emit_snapshot()
        for robot_id, observation in self._observations.items():
            logging.info("runtime > Robot %s reachable=%s ready=%s", robot_id, observation["reachable"], observation["ready"])
        return True

    async def update_robot_status(self, robot_id, status):
        robot_id = str(robot_id)
        if robot_id not in self.robots:
            return False
        old = self._observations[robot_id]
        reachable = status.reachable is True
        ready = reachable and status.ready is True
        previous_ready = old["ready"]
        if reachable != old["reachable"]:
            old["reachable"] = reachable
            await self._emit_robot("robot.reachability_changed", robot_id, {"reachable": reachable})
        if ready != previous_ready:
            old["ready"] = ready
            await self._emit_robot("robot.readiness_changed", robot_id, {"ready": ready})
        new_faults = {fault["fault_id"]: fault for fault in status.faults}
        for fault_id, fault in new_faults.items():
            if fault_id not in old["faults"]:
                await self._emit_robot("robot.fault_raised", robot_id, {"fault": self._normalize_fault(fault)})
        for fault_id in old["faults"].keys() - new_faults.keys():
            await self._emit_robot("robot.fault_cleared", robot_id, {"fault_id": fault_id})
        old["faults"] = new_faults
        logging.info(
            "runtime > Robot %s seat=%s kind=%s reachable=%s ready=%s faults=%s",
            robot_id,
            self.robots[robot_id]["seat"],
            self.robots[robot_id]["implementation_kind"],
            reachable,
            ready,
            sorted(
                fault.get("code", fault_id)
                for fault_id, fault in new_faults.items()
            ),
        )
        return True

    async def emit_snapshot(self):
        self.snapshot_seq += 1
        robots = []
        for robot_id, robot in self.robots.items():
            observation = self._observations[robot_id]
            robots.append({
                "robot_id": robot_id, "seat": robot["seat"],
                "reachable": observation["reachable"], "ready": observation["ready"],
                "active_faults": [self._normalize_fault(fault) for fault in observation["faults"].values()],
                "observation_seq": observation["seq"],
            })
        await self._emit("controller.status_snapshot", {
            "protocol_version": PROTOCOL_VERSION, "game_id": self.game_id,
            "controller_id": self.controller_id, "connection_epoch": self.connection_epoch,
            "config_revision": self.applied_config_revision,
            "config_digest": self.applied_config_digest,
            "snapshot_seq": self.snapshot_seq, "observed_at": self._clock(), "robots": robots,
        })

    def robot_id_for_seat(self, seat):
        return self.seats.get(seat)

    def _validate(self, command):
        required = ["protocol_version", "game_id", "controller_id", "connection_epoch", "request_id", "config_revision", "config_digest", "runtime_config"]
        if any(not command.get(key) for key in required):
            raise RuntimeConfigurationError("Missing configure field")
        if command["protocol_version"] != PROTOCOL_VERSION or str(command["controller_id"]) != self.controller_id:
            raise RuntimeConfigurationError("Unsupported protocol or Controller identity")
        config = command["runtime_config"]
        if str(config.get("game_id")) != str(command["game_id"]) or str(config.get("controller_id")) != self.controller_id:
            raise RuntimeConfigurationError("Runtime configuration identity mismatch")
        canonical = json.dumps(config, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if digest != command["config_digest"] or command["config_revision"] != command["config_digest"]:
            raise RuntimeConfigurationError("Runtime configuration digest mismatch")
        robots = config.get("robots")
        if not isinstance(robots, list):
            raise RuntimeConfigurationError("robots must be a list")
        ids, seats = set(), set()
        for robot in robots:
            robot_id, seat = str(robot.get("robot_id", "")), robot.get("seat")
            if not robot_id or not isinstance(seat, int) or robot_id in ids or seat in seats:
                raise RuntimeConfigurationError("Invalid or duplicate Robot identity/routing")
            if robot.get("implementation_kind") not in ("physical", "simulated") or not isinstance(robot.get("runtime_config"), dict):
                raise RuntimeConfigurationError("Invalid Robot runtime configuration")
            ids.add(robot_id); seats.add(seat)
        return config

    def _install_status(self, robot_id, status):
        observation = self._observations[robot_id]
        observation["reachable"] = status.reachable is True
        observation["ready"] = observation["reachable"] and status.ready is True
        observation["faults"] = {fault["fault_id"]: fault for fault in status.faults}

    async def _emit_robot(self, event, robot_id, values):
        observation = self._observations[robot_id]
        observation["seq"] += 1
        await self._emit(event, {
            "game_id": self.game_id, "controller_id": self.controller_id,
            "connection_epoch": self.connection_epoch, "robot_id": robot_id,
            "config_revision": self.applied_config_revision,
            "observation_seq": observation["seq"], "observed_at": self._clock(), **values,
        })

    def _normalize_fault(self, fault):
        return {**fault, "observed_at": fault.get("observed_at", self._clock())}

    @staticmethod
    def _operation_key(command):
        return tuple(command.get(key) for key in ("connection_epoch", "request_id", "config_revision", "config_digest"))

    @staticmethod
    def _ack(command):
        return {key: command.get(key) for key in (
            "protocol_version", "game_id", "controller_id", "connection_epoch",
            "request_id", "config_revision", "config_digest",
        )}
