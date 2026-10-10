from .i2c import connected_i2c_addresses, i2c_connected
from .led import LED
from .relay import Relay
from .servo import Servo

__all__ = [
    "LED",
    "Relay",
    "SafeTCS34725",
    "Servo",
    "connected_i2c_addresses",
    "i2c_connected",
]


def __getattr__(name):
    """Load optional hardware drivers only when explicitly requested."""
    if name == "SafeTCS34725":
        from .safe_tcs34725 import SafeTCS34725

        globals()[name] = SafeTCS34725
        return SafeTCS34725
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
