from .backend import Backend, TcpRobotBackend
from .simulated_backend import SimulatedRobotBackend


class BackendRegistry:
    def __init__(self, factory=None, simulated_world=None):
        self._factory = factory or self._default_factory
        self.simulated_world = simulated_world

    def create(self, robot: dict) -> Backend:
        backend = self._factory(robot)
        if isinstance(backend, SimulatedRobotBackend) and self.simulated_world:
            backend.world = self.simulated_world
        return backend

    @staticmethod
    def _default_factory(robot: dict) -> Backend:
        if robot.get("implementation_kind") == "simulated":
            return SimulatedRobotBackend(str(robot["robot_id"]), robot["seat"])
        if robot.get("implementation_kind") != "physical":
            raise ValueError("Unsupported Robot implementation_kind")
        backend_class = (
            TcpRobotBackend
            if robot.get("runtime_config", {}).get("address")
            else Backend
        )
        return backend_class(str(robot["robot_id"]), robot["seat"])
