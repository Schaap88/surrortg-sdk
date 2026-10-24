DEVICE_IDENTITY_VERSION = 1
DEVICE_IDENTITY_PAYLOAD_SIZE = 7


def canonical_mac(mac: bytes) -> str:
    if len(mac) != 6:
        raise ValueError("MAC address must contain exactly six bytes")
    if not any(mac) or mac == b"\xff" * 6 or mac[0] & 1:
        raise ValueError("invalid station MAC address")
    return ":".join(f"{octet:02x}" for octet in mac)


def parse_device_identity_payload(payload: bytes) -> str:
    if len(payload) != DEVICE_IDENTITY_PAYLOAD_SIZE:
        raise ValueError("DEVICE_IDENTITY payload must be exactly seven bytes")
    if payload[0] != DEVICE_IDENTITY_VERSION:
        raise ValueError(f"unsupported DEVICE_IDENTITY version {payload[0]}")
    return canonical_mac(payload[1:])


__all__ = ["canonical_mac", "parse_device_identity_payload"]
