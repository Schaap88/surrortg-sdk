import logging
import struct
from surrortg.devices.tcp.tcp_input import TcpInput
from surrortg.devices.tcp.tcp_protocol import TcpCommandId


class TcpBattery(TcpInput):
    """Class for TCP-controlled battery monitoring."""

    def __init__(self):
        super().__init__()
        self._cmd = TcpCommandId.BATTERY_STATUS
        self.battery_data = {}  # Store received battery data

    async def get_status(self, seat):
        """Request the battery status from the robot."""
        if seat not in self.endpoints:
            logging.warning(f"No endpoint for seat {seat}, cannot request battery status.")
            return None

        endpoint = self.endpoints[seat]
        if endpoint.closed:
            logging.warning(f"Endpoint for seat {seat} is closed.")
            return None

        try:
            # Send the BATTERY_STATUS request
            await endpoint.send(struct.pack("B", self._cmd))
            logging.info(f"Sent BATTERY_STATUS command for seat {seat}")
        except Exception as e:
            logging.error(f"Failed to send battery status request for seat {seat}: {e}")

    async def handle_battery_response(self, seat, data):
        """Process battery status response received from the robot."""
        if len(data) < 8:
            logging.warning(f"Incomplete battery data received for seat {seat}")
            return

        voltage, soc = struct.unpack("<ff", data)  # Expecting 2 floats (voltage & SOC)
        self.battery_data[seat] = {"voltage": voltage, "soc": soc}
        logging.info(f"Battery status for seat {seat}: {voltage:.2f}V, {soc:.1f}%")

        # Here, you could also send this data back to the game engine:
        # self.io.send_custom_event("batteryStatusUpdate", seat=seat, payload=self.battery_data[seat])