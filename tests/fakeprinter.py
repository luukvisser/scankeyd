"""A fake printer for tests: phoenix registration (plain HTTP, digest), SNMP SET, scan protocol."""

from __future__ import annotations

import hashlib
import http.server
import json
import socket
import socketserver
import struct
import threading
import time
from typing import ClassVar, override

REALM, NONCE = "Machine", "abc123"


def fake_jpeg(width: int = 40, height: int = 30, comps: int = 1, size: int = 200_000) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 8 + 3 * comps, 8, height, width, comps) + b"\x01\x11\x00" * comps
    body = bytes((i * 7) % 251 for i in range(size))
    return b"\xff\xd8" + sof + b"\xff\xda\x00\x02" + body + b"\xff\xd9"


def packbits(data: bytes) -> bytes:
    """Simple PackBits encoder: runs of 3+ equal bytes as runs, the rest as literals."""
    out, i = bytearray(), 0
    while i < len(data):
        j = i
        while j < len(data) and j - i < 128 and data[j] == data[i]:
            j += 1
        if j - i >= 3:
            out += bytes([257 - (j - i), data[i]])
            i = j
            continue
        k = i
        while k < len(data) and k - i < 128 and not (k + 2 < len(data) and data[k] == data[k + 1] == data[k + 2]):
            k += 1
        out += bytes([k - i - 1]) + data[i:k]
        i = k
    return bytes(out)


def fake_rows(width_bytes: int = 306, height: int = 40) -> bytes:
    return b"".join((bytes([0] * (width_bytes - 10)) + bytes(range(r, r + 10))) for r in range(height))


class PhoenixHandler(http.server.BaseHTTPRequestHandler):
    entries: ClassVar[list[list[str]]] = []
    status = 0

    @override
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (the base class's name)
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Digest ") or not self._valid(auth):
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate", f'Digest realm="{REALM}", nonce="{NONCE}", opaque="op", algorithm="MD5", qop="auth"'
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        req = json.loads(body)["request"]
        PhoenixHandler.entries.append([r["string_value"] for r in req])
        data = json.dumps({"response": [{"key": r["key"], "status": PhoenixHandler.status} for r in req]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _valid(self, auth: str) -> bool:
        p = {k.strip(): v.strip().strip('"') for k, v in (x.split("=", 1) for x in auth[7:].split(","))}
        ha1 = hashlib.md5(f"Public:{REALM}:0000".encode()).hexdigest()
        ha2 = hashlib.md5(f"POST:{p['uri']}".encode()).hexdigest()
        want = hashlib.md5(f"{ha1}:{NONCE}:{p['nc']}:{p['cnonce']}:auth:{ha2}".encode()).hexdigest()
        return p.get("response") == want and p.get("opaque") == "op" and p.get("username") == "Public"


def start_phoenix() -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PhoenixHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ScanHandler(socketserver.BaseRequestHandler):
    jpeg = b""
    rows: ClassVar[tuple[int, bytes] | None] = None  # (width_bytes, rows) for a black-and-white page
    busy = False
    hold_tail = False  # end the page as the real printer did from its Scan key: 2 of 4 closing bytes
    seen: ClassVar[list[tuple[bytes, bytes]]] = []

    def _until_80(self) -> bytes:
        buf = b""
        while not buf.endswith(b"\x80"):
            chunk = self.request.recv(1)
            if not chunk:
                break
            buf += chunk
        return buf

    @override
    def handle(self) -> None:
        if ScanHandler.busy:
            self.request.sendall(b"-NG 401\r\n")
            return
        self.request.sendall(b"+OK 200\r\n")
        ssp = self._until_80()
        # answers as the DCP-L2627DWE did (06-11-2026): the resolution it really scans at (b/w and gray
        # at most 600 x 1200, colour 1200 x 1200), and no document feeder (a maximum height of 0)
        params = dict(line.split("=", 1) for line in ssp.decode("ascii", "replace").splitlines() if "=" in line)
        reso = int(params.get("RESO", "300,300").split(",")[0])
        colour = params.get("CLR", "") == "C24BIT"
        x, y = min(reso, 1200 if colour else 600), min(reso, 1200)
        feeder = params.get("PSRC") == "ADF"
        payload = (
            b"\x00\x00\x00"
            + struct.pack("<HHH", 0x2001, 2, 1 if feeder else 2)
            + struct.pack("<HHHH", 0x2002, 4, x, y)
            + struct.pack("<HHII", 0x2003, 8, 2479 * x // 300, 0 if feeder else 3437 * y // 300)
        )
        self.request.sendall(b"\x00SSP" + struct.pack("<H", len(payload)) + payload)
        xsc = self._until_80()
        if not xsc:  # asked without a scan (the capabilities probe): the client is gone
            return
        ScanHandler.seen.append((ssp, xsc))
        self.request.sendall(b"\x00\x00")
        if ScanHandler.rows:
            wb, rows = ScanHandler.rows
            for r in range(0, len(rows), wb):
                enc = packbits(rows[r : r + wb])
                self.request.sendall(b"\x00\x01\x01\x00\x08\x00" + struct.pack("<H", len(enc)) + b"\x00" * 6 + enc)
            self.request.sendall(b"\x00\x21\x01\x00\x00\x20")
            return
        data, lines = ScanHandler.jpeg, 0
        for i in range(0, len(data), 65522):
            chunk = data[i : i + 65522]
            lines += 100
            self.request.sendall(
                b"\x00\x02\x01\x00\x11\x00"
                + struct.pack("<H", len(chunk))
                + b"\x00\x00"
                + struct.pack("<H", lines)
                + b"\x00\x00"
                + chunk
            )
        if ScanHandler.hold_tail:
            self.request.sendall(b"\x00\x21\x01\x00")
            time.sleep(5)  # the rest only after "scan another page?" is answered: not within the test
            return
        self.request.sendall(b"\x00\x21\x01\x00\x00\x20")


def start_scanner() -> socketserver.ThreadingTCPServer:
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ScanHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class SnmpServer:
    """Answers SET requests with error-status 0 and remembers the values."""

    def __init__(self) -> None:
        from scankeyd import snmp

        self.snmp = snmp
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.values: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        s = self.snmp
        while True:
            data, addr = self.sock.recvfrom(4096)
            _, msg, _ = s._read(data, 0)
            _, _, i = s._read(msg, 0)
            _, comm, i = s._read(msg, i)
            tag, pdu, _ = s._read(msg, i)
            _, rid, j = s._read(pdu, 0)
            _, _, j = s._read(pdu, j)
            _, _, j = s._read(pdu, j)
            _, vbl, _ = s._read(pdu, j)
            _, vb, _ = s._read(vbl, 0)
            _, oid, k = s._read(vb, 0)
            _, value, _ = s._read(vb, k)
            if comm == b"internal" and tag == 0xA3:
                self.values.append(value)
            resp_pdu = s._tlv(
                0xA2,
                s._tlv(0x02, rid)
                + s._int(0)
                + s._int(0)
                + s._tlv(0x30, s._tlv(0x30, s._tlv(0x06, oid) + s._tlv(0x04, value))),
            )
            self.sock.sendto(s._tlv(0x30, s._int(0) + s._tlv(0x04, comm) + resp_pdu), addr)
