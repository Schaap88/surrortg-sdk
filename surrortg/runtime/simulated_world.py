import asyncio
import math
import time


class SimulatedWorld:
    """Small controller-local authority shared by simulated Robot backends."""

    WIDTH = 1000.0
    HEIGHT = 600.0
    TICK_SECONDS = 1 / 15
    SPEED = 120.0
    TURN_SPEED = 2.4

    def __init__(self, game_id=None, publish=None):
        self.game_id = str(game_id) if game_id is not None else None
        self.publish = publish
        self.robots = {}
        self.sequence = 0
        self._task = None
        self._last_tick = None

    def configure(self, game_id, publish):
        self.game_id = str(game_id)
        self.publish = publish

    def add(self, robot_id, seat):
        index = len(self.robots)
        columns = 4
        self.robots[str(robot_id)] = {
            "robot_id": str(robot_id), "x": 140.0 + (index % columns) * 220.0,
            "y": 140.0 + (index // columns) * 220.0, "heading": 0.0,
            "drive": 0.0, "steering": 0.0, "seat": seat,
        }
        if self._task is None or self._task.done():
            self._last_tick = time.monotonic()
            self._task = asyncio.create_task(self._run())

    def remove(self, robot_id):
        self.robots.pop(str(robot_id), None)

    def control(self, robot_id, command):
        robot = self.robots.get(str(robot_id))
        if not robot:
            return
        values = command.get("command", {})
        if "x" in values or "y" in values:
            robot["steering"] = float(values.get("x", 0))
            robot["drive"] = float(values.get("y", 0))
        elif "val" in values:
            key = command.get("id", "").lower()
            robot["steering" if any(part in key for part in ("steer", "turn", "x")) else "drive"] = float(values["val"])

    def neutralize(self, robot_id):
        robot = self.robots.get(str(robot_id))
        if robot:
            robot["drive"] = robot["steering"] = 0.0

    async def _run(self):
        try:
            while self.robots:
                await asyncio.sleep(self.TICK_SECONDS)
                now = time.monotonic()
                dt = min(now - self._last_tick, 0.2)
                self._last_tick = now
                for robot in self.robots.values():
                    robot["heading"] = (robot["heading"] + robot["steering"] * self.TURN_SPEED * dt) % (2 * math.pi)
                    distance = robot["drive"] * self.SPEED * dt
                    robot["x"] = min(self.WIDTH, max(0.0, robot["x"] + math.cos(robot["heading"]) * distance))
                    robot["y"] = min(self.HEIGHT, max(0.0, robot["y"] + math.sin(robot["heading"]) * distance))
                if self.publish and self.game_id:
                    self.sequence += 1
                    await self.publish({
                        "version": 1, "game_id": self.game_id, "sequence": self.sequence,
                        "arena": {"width": self.WIDTH, "height": self.HEIGHT},
                        "robots": [{key: round(robot[key], 3) if key != "robot_id" else robot[key]
                                    for key in ("robot_id", "x", "y", "heading")}
                                   for robot in self.robots.values()],
                    })
        finally:
            self._task = None
