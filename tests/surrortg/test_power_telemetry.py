import struct
import unittest

from surrortg.device_identity import (
    canonical_mac,
    parse_device_identity_payload,
)
from surrortg.power_telemetry import (
    DockState,
    PowerValidity,
    parse_power_status_payload,
)


class PowerTelemetryProtocolTest(unittest.TestCase):
    def test_valid_and_invalid_measurements_are_explicit(self):
        payload = struct.pack(
            ">BBHHhiBHHH",
            1,
            int(PowerValidity.CURRENT),
            0,
            0,
            -1,
            0,
            0,
            0xFFFF,
            7,
            0xFFFF,
        )
        status = parse_power_status_payload(payload)
        self.assertIsNone(status.voltage_mv)
        self.assertIsNone(status.state_of_charge_percent)
        self.assertEqual(status.current_ma, -1)
        self.assertIsNone(status.power_mw)
        self.assertEqual(status.dock_state, DockState.UNKNOWN)
        self.assertIsNone(status.fuel_gauge_age_ms)
        self.assertEqual(status.current_age_ms, 700)

    def test_malformed_power_payloads_are_rejected(self):
        valid = struct.pack(
            ">BBHHhiBHHH",
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFF,
        )
        malformed = (
            valid[:-1],
            b"\x02" + valid[1:],
            b"\x01\x80" + valid[2:],
            struct.pack(">BBHHhiBHHH", 1, 0x10, 0, 0, 0, 0, 0, 0, 0, 0),
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_power_status_payload(payload)

    def test_identity_canonicalization_and_validation(self):
        self.assertEqual(
            parse_device_identity_payload(
                b"\x01" + bytes.fromhex("AABBCC010203")
            ),
            "aa:bb:cc:01:02:03",
        )
        for mac in (b"\0" * 6, b"\xff" * 6, bytes.fromhex("010203040506")):
            with self.subTest(mac=mac):
                with self.assertRaises(ValueError):
                    canonical_mac(mac)


if __name__ == "__main__":
    unittest.main()
