"""Skid Steer Open World entry point for the current Controller runtime.

The modern ControllerRuntime owns simulated and physical Robot backends.  This
entry point registers compatibility bindings for the normalized runtime inputs
without constructing the legacy Skidsteer/TcpCar, which would create a second
connection to the physical Robot.
"""

import argparse
import logging

from surrortg import Game
from surrortg.inputs import Joystick, LinearActuator


class RuntimeDriveInput(Joystick):
    """Compatibility binding; ControllerRuntime performs the actual actuation."""

    async def handle_coordinates(self, x, y, seat=0):
        logging.debug("Skid Steer drive input seat=%s x=%s y=%s", seat, x, y)

    async def reset(self, seat=0):
        logging.debug("Skid Steer drive input reset seat=%s", seat)


class RuntimeActuatorInput(LinearActuator):
    """Compatibility binding; ControllerRuntime performs the actual actuation."""

    async def drive_actuator(self, val, seat=0):
        pass


class SkidSteerOpenWorldGame(Game):
    """Open-world shell; missions and scoring can be layered on later."""

    async def on_init(self):
        self.io.register_inputs({
            "joystick_main": RuntimeDriveInput(),
            "drive_joystick": RuntimeDriveInput(),
            "lift": RuntimeActuatorInput(),
            "tilt": RuntimeActuatorInput(),
        })


if __name__ == "__main__":
    parser = argparse.ArgumentParser("Skid Steer Open World")
    parser.add_argument(
        "-c",
        "--conf",
        metavar="",
        help="path to configuration .toml file",
        required=False,
    )
    args = parser.parse_args()
    SkidSteerOpenWorldGame().run(config_path=args.conf)
