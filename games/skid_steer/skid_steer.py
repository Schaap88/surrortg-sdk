# games/skid_steer/skid_steer.py

import struct
import logging
import asyncio

from surrortg.devices.tcp import TcpCar, TcpCommandId, TcpActuator

class Skidsteer(TcpCar):
    """Class for TCP-controlled M5 Rover"""

    def __init__(
        self,
        game_io,
        throttle_mult=0.5,
        steering_mult=0.2,
        lift_mult=1.0,
        tilt_mult=1.0,
    ):
        super().__init__(game_io)
        self.battery_voltage = None
        self.battery_soc = None

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
            ge_config=ge_config,
            bot_listener_cb=self._handle_battery_response
        )

    async def _handle_battery_response(self, seat, cmd_id, value):
        """Process battery status updates"""
        if cmd_id == TcpCommandId.BATTERY_STATUS:
            self.battery_voltage, self.battery_soc = value
            logging.info(f"Battery Status - Seat {seat}: {self.battery_soc}% SOC, {self.battery_voltage:.2f}V")

    async def request_battery_status(self, seat):
        endpoint = self.endpoints.get(seat)
        if endpoint and not endpoint.closed:
            try:
                # Send command + dummy value
                await endpoint.send(struct.pack("BB", TcpCommandId.BATTERY_STATUS, 0))
                
                # Read and discard leftover bytes
                while True:
                    data = await endpoint.receive(100)
                    if not data:
                        break
            except Exception as e:
                logging.error(f"Battery protocol error: {e}")
