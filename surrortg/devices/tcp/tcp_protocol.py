from enum import IntEnum

from surrortg.tcp_transport import TcpEndpoint, open_tcp_endpoint


class TcpCommandId(IntEnum):
    """Emun for 8-bit command identifiers of TCP-controlled bots"""

    THROTTLE = 1
    THROTTLE_CAL = 2
    STEER = 3
    STEER_CAL = 4
    CUSTOM_1 = 5
    CUSTOM_1_CAL = 6
    CUSTOM_2 = 7
    CUSTOM_2_CAL = 8
    CUSTOM_3 = 9
    CUSTOM_3_CAL = 10
    CUSTOM_4 = 11
    CUSTOM_4_CAL = 12
    BATTERY_STATUS = 100
    PING = 200
    STOP = 0xFF


__all__ = ["TcpCommandId", "TcpEndpoint", "open_tcp_endpoint"]
