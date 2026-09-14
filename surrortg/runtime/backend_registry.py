from .backend import Backend, TcpRobotBackend


class BackendRegistry:
    def __init__(self, factory=None):
        self._factory = factory or self._default_factory

    def create(self, robot: dict) -> Backend:
        return self._factory(robot)

    @staticmethod
    def _default_factory(robot: dict) -> Backend:
        backend_class = (
            TcpRobotBackend
            if robot.get("runtime_config", {}).get("address")
            else Backend
        )
        return backend_class(str(robot["robot_id"]), robot["seat"])
