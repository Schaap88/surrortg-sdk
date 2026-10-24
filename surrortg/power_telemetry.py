import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Optional


POWER_STATUS_VERSION = 1
POWER_STATUS_PAYLOAD_SIZE = 19
POWER_AGE_UNAVAILABLE = 0xFFFF
KNOWN_VALIDITY_FLAGS = 0x1F


class PowerValidity(IntFlag):
    VOLTAGE = 1 << 0
    SOC = 1 << 1
    CURRENT = 1 << 2
    POWER = 1 << 3
    DOCK = 1 << 4


class DockState(IntEnum):
    UNKNOWN = 0
    ABSENT = 1
    PRESENT = 2


@dataclass(frozen=True)
class PowerStatus:
    version: int
    validity: PowerValidity
    voltage_mv: Optional[int]
    state_of_charge_percent: Optional[float]
    current_ma: Optional[int]
    power_mw: Optional[int]
    dock_state: DockState
    fuel_gauge_age_ms: Optional[int]
    current_age_ms: Optional[int]
    dock_age_ms: Optional[int]

    @property
    def voltage_v(self):
        return None if self.voltage_mv is None else self.voltage_mv / 1000


def _decode_age(value):
    return None if value == POWER_AGE_UNAVAILABLE else value * 100


def parse_power_status_payload(payload: bytes) -> PowerStatus:
    if len(payload) != POWER_STATUS_PAYLOAD_SIZE:
        raise ValueError("POWER_STATUS payload must be exactly 19 bytes")
    (
        version,
        flags_raw,
        voltage_mv,
        soc_centipercent,
        current_ma,
        power_mw,
        dock_raw,
        fuel_age,
        current_age,
        dock_age,
    ) = struct.unpack(">BBHHhiBHHH", payload)
    if version != POWER_STATUS_VERSION:
        raise ValueError(f"unsupported POWER_STATUS version {version}")
    if flags_raw & ~KNOWN_VALIDITY_FLAGS:
        raise ValueError("POWER_STATUS contains unknown validity flags")
    try:
        dock_state = DockState(dock_raw)
    except ValueError as error:
        raise ValueError(f"invalid dock state {dock_raw}") from error

    flags = PowerValidity(flags_raw)
    if bool(flags & PowerValidity.DOCK) != (
        dock_state != DockState.UNKNOWN
    ):
        raise ValueError("dock validity and state disagree")
    if flags & PowerValidity.SOC and soc_centipercent > 10000:
        raise ValueError("state of charge exceeds 100 percent")
    if flags & PowerValidity.POWER and not (
        flags & PowerValidity.VOLTAGE and flags & PowerValidity.CURRENT
    ):
        raise ValueError("power validity requires voltage and current")
    if not (flags & (PowerValidity.VOLTAGE | PowerValidity.SOC)) and (
        fuel_age != POWER_AGE_UNAVAILABLE
    ):
        raise ValueError("invalid fuel-gauge values must have unavailable age")
    if not (flags & PowerValidity.CURRENT) and (
        current_age != POWER_AGE_UNAVAILABLE
    ):
        raise ValueError("invalid current must have unavailable age")
    if not (flags & PowerValidity.DOCK) and dock_age != POWER_AGE_UNAVAILABLE:
        raise ValueError("invalid dock state must have unavailable age")

    return PowerStatus(
        version=version,
        validity=flags,
        voltage_mv=(
            voltage_mv if flags & PowerValidity.VOLTAGE else None
        ),
        state_of_charge_percent=(
            soc_centipercent / 100 if flags & PowerValidity.SOC else None
        ),
        current_ma=(current_ma if flags & PowerValidity.CURRENT else None),
        power_mw=(power_mw if flags & PowerValidity.POWER else None),
        dock_state=dock_state,
        fuel_gauge_age_ms=_decode_age(fuel_age),
        current_age_ms=_decode_age(current_age),
        dock_age_ms=_decode_age(dock_age),
    )


__all__ = [
    "DockState",
    "PowerStatus",
    "PowerValidity",
    "parse_power_status_payload",
]
