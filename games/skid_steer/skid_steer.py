# games/skid_steer/skid_steer.py

import struct
import logging
import asyncio

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

    async def _handle_battery_response(self, seat, cmd_id, data):
        if cmd_id == TcpCommandId.BATTERY_STATUS:
            voltage, soc = data
            logging.info(f"Battery seat {seat}: {voltage:.2f}V, {soc:.1f}%")
            self.battery_voltage, self.battery_soc = voltage, soc
            
            # Send to signaling server directly here
            self.io.send_telemetry(
                seat=seat,
                payload={"voltage": voltage, "soc": soc}
            )

    async def request_battery_status(self, seat):
        """Send battery status request without reading."""
        endpoint = self.endpoints.get(seat)
        if not endpoint or endpoint.closed:
            logging.warning(f"Endpoint missing or closed for seat {seat}")
            return
        try:
            cmd_bytes = struct.pack("BB", TcpCommandId.BATTERY_STATUS,0)
            logging.info(f"Sending BATTERY_STATUS request: {cmd_bytes.hex()} to seat {seat}")
            await endpoint.send(cmd_bytes)
        except Exception as e:
            logging.error(f"Battery request to seat {seat} failed: {e}")
