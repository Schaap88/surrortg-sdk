# games/skid_steer/skid_steer.py

import logging

from surrortg.devices.tcp import TcpCar, TcpCommandId, TcpActuator

class Skidsteer(TcpCar):
    """Class for TCP-controlled converted Double Eagle E594 """

    def __init__(
        self,
        game_io,
        throttle_mult=0.5,
        steering_mult=0.2,
        lift_mult=1.0,
        tilt_mult=1.0,
    ):
        super().__init__(game_io, throttle_mult, steering_mult)
        self.io = game_io

        # Register inputs using add_inputs
        self.add_inputs(
            {
                "lift": TcpActuator(TcpCommandId.CUSTOM_1, lift_mult),
                "tilt": TcpActuator(TcpCommandId.CUSTOM_2, tilt_mult),
            }
        )
        logging.info("Lift and Tilt actuators added.")

    async def handle_config(self, ge_config):
        """Handle robot configuration"""
        logging.info(f"Configuring with: {ge_config}")
        return await super().handle_config(
            ge_config=ge_config
        )
