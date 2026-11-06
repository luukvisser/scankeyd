"""Just enough SNMPv1 (BER) for one GET and one SET of an OCTET STRING."""

from __future__ import annotations

import secrets
import socket
import time


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int) -> bytes:
    return _tlv(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.insert(0, 0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(chunk)
    return _tlv(0x06, out)


def _read(buf: bytes, i: int) -> tuple[int, bytes, int]:
    if i + 2 > len(buf):
        raise ValueError("short BER")
    tag, n = buf[i], buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or i + k > len(buf):
            raise ValueError("bad BER length")
        n = int.from_bytes(buf[i : i + k], "big")
        i += k
    if i + n > len(buf):
        raise ValueError("BER value past end")
    return tag, buf[i : i + n], i + n


def message(pdu_tag: int, community: bytes, oid: str, value: bytes | None, request_id: int) -> bytes:
    val = b"\x05\x00" if value is None else _tlv(0x04, value)
    varbinds = _tlv(0x30, _tlv(0x30, _oid(oid) + val))
    pdu = _tlv(pdu_tag, _int(request_id) + _int(0) + _int(0) + varbinds)
    return _tlv(0x30, _int(0) + _tlv(0x04, community) + pdu)


def parse_response(data: bytes, request_id: int) -> tuple[int, bytes]:
    """Returns (error-status, first value) of a GetResponse, or raises ValueError."""
    _, msg, _ = _read(data, 0)
    _, _, i = _read(msg, 0)  # version
    _, _, i = _read(msg, i)  # community
    tag, pdu, _ = _read(msg, i)
    if tag != 0xA2:
        raise ValueError("not a GetResponse")
    _, rid, j = _read(pdu, 0)
    if int.from_bytes(rid, "big", signed=True) != request_id:
        raise ValueError("request id mismatch")
    _, err, j = _read(pdu, j)
    _, _, j = _read(pdu, j)  # error-index
    _, vbl, _ = _read(pdu, j)
    value = b""
    if vbl:
        _, vb, _ = _read(vbl, 0)
        _, _, k = _read(vb, 0)
        _, value, _ = _read(vb, k)
    return int.from_bytes(err, "big"), value


def _exchange(
    host: str,
    port: int,
    pdu_tag: int,
    community: bytes,
    oid: str,
    value: bytes | None,
    timeout: float = 3.0,
    tries: int = 2,
) -> tuple[int, bytes]:
    rid = secrets.randbelow(2**31)
    msg = message(pdu_tag, community, oid, value, rid)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, port))
        for _ in range(tries):
            s.send(msg)
            deadline = time.monotonic() + timeout  # stray datagrams cannot stretch the wait
            while (left := deadline - time.monotonic()) > 0:
                s.settimeout(left)
                try:
                    data = s.recv(4096)
                except TimeoutError:
                    break
                try:
                    return parse_response(data, rid)
                except ValueError:
                    continue  # not our answer
    raise TimeoutError(f"no SNMP answer from {host}")


def get(host: str, oid: str, community: bytes = b"public", port: int = 161) -> tuple[int, bytes]:
    return _exchange(host, port, 0xA0, community, oid, None)


def set_string(host: str, oid: str, value: bytes, community: bytes = b"internal", port: int = 161) -> int:
    err, _ = _exchange(host, port, 0xA3, community, oid, value)
    return err
