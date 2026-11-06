"""Brother's network scan protocol on TCP 54921, flatbed: JPEG (gray, color) or 1-bit (bw).

Observed with a DCP-L2627DWE (2026); see docs/PROTOCOL.md.

  printer: "+OK 200\r\n"                      (busy: "-NG 401\r\n")
  host:    ESC "SSP\n" KEY=value\n ... 0x80     scan parameters
  printer: 0x00 "SSP" len16 <payload>          payload holds TLVs (id16, len16, value);
                                                id 0x2003 = maximum area in pixels (u32 w, u32 h)
  host:    ESC "XSC\n" RESO=..\n AREA=0,0,w,h\n MODE=NORMAL\n 0x80
  printer: 0x00 0x00                            (acknowledgement)
           blocks: 0x00 0x02 <12-byte rest>     data block; bytes 6-7 = data length (LE),
                   <data>                       bytes 10-11 = lines done; data = the JPEG, split
           0x00 0x01 <12-byte rest>     black-and-white: one block per scan line, bytes 6-7 =
                   <data>                       length; data = the line in packbits; 1 = black
           0x00 0x21 <4 bytes>                  end of page

A JPEG page is the concatenation of the data blocks; a black-and-white page
is the decoded lines, each the same number of bytes.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from dataclasses import dataclass

PORT = 54921
MAX_PAGE_BYTES = 64 * 1024 * 1024
CLR = {"gray": "GRAY256", "color": "C24BIT", "bw": "TEXT"}
COMP = {"gray": "JPEG", "color": "JPEG", "bw": "RLENGTH"}


class ScanError(Exception):
    pass


class PrinterBusy(ScanError):
    pass


def ssp_command(resolution: int, color: str, source: str = "FB") -> bytes:
    params = [
        ("OS", "LNX"),
        ("PSRC", source),
        ("RESO", f"{resolution},{resolution}"),
        ("CLR", CLR[color]),
        ("AREA", "NORMAL"),
        ("MRGN", "0,0,0,0"),
        ("DPLX", "OFF"),
        ("BRIT", "50"),
        ("CONT", "50"),
        ("COMP", COMP[color]),
        ("JSF", "420"),
        ("IPRC", "NORMAL"),
        ("PTYPE", "NORMAL"),
        ("PAGE", "0"),
        ("LONG", "OFF"),
        ("CARR", "OFF"),
        ("RMGC", "OFF"),
        ("DTDF", "OFF"),
        ("DT4V", "OFF"),
        ("DSKW", "OFF"),
        ("LSMD", "OFF"),
        ("RMBP", "OFF"),
        ("RMMR", "OFF"),
        ("GMMA", "OFF"),
        ("TONE", "OFF"),
        ("QTFD", "OFF"),
        ("ATCN", "OFF"),
        ("ATCRP", "OFF"),
        ("USER", ""),
    ]
    return b"\x1bSSP\n" + b"".join(f"{k}={v}\n".encode() for k, v in params) + b"\x80"


def xsc_command(resolution: int, width: int, height: int) -> bytes:
    return f"\x1bXSC\nRESO={resolution},{resolution}\nAREA=0,0,{width},{height}\nMODE=NORMAL\n".encode() + b"\x80"


PANEL_TAGS = {0x1001: "key", 0x1002: "scan", 0x1003: "duplex", 0x1004: "image", 0x1005: "file"}


def panel_settings(printer: str, port: int = PORT) -> dict[str, list[int]] | None:
    """What was chosen on the printer's panel for the press at hand (ESC GKP; layout as Brother's
    brscan5 and github.com/aurimasniekis/go-brother-scan-to-network-server read it): per field the
    raw byte values, 0xff where the panel left something unset. "scan" holds the resolution (x, y as
    16-bit values in its first four bytes), colour type, paper, direction, brightness, contrast.
    None when the printer has nothing waiting (status 0x81 without a press, 06-11-2026) or does not
    know the command. A question on its own connection: unlike scan settings without a scan, it
    leaves the scan port free."""
    try:
        with socket.create_connection((printer, port), timeout=10) as sock:
            r = _Reader(sock, time.monotonic() + 15)
            if not r.line().startswith(b"+OK"):
                return None
            sock.sendall(b"\x1bGKP\n\x80")
            head = r.exact(8)
            if head[1:4] != b"GKP":
                return None
            n = struct.unpack_from("<I", head, 4)[0]
            if head[0] != 0 or not 0 < n <= 65536:
                return None
            body = r.exact(n)
    except (OSError, ScanError):
        return None
    out: dict[str, list[int]] = {}
    i = 0
    while i + 4 <= len(body):
        tag, size = struct.unpack_from("<HH", body, i)
        if tag == 0xF0F0:
            break
        value = body[i + 4 : i + 4 + size]
        if tag == 0x1002 and len(value) >= 4:
            out["resolution"] = list(struct.unpack_from("<HH", value))
            out["scan"] = list(value[4:])
        else:
            out[PANEL_TAGS.get(tag, f"{tag:#06x}")] = list(value)
        i += 4 + size
    return out


def answer_fields(payload: bytes) -> dict[int, bytes]:
    """The SSP answer's fields: 0x20xx type, 16-bit length, value; after a short prefix (two bytes on
    the DCP-L2627DWE). Seen (06-11-2026): 0x2001 the paper source (2 flatbed, 1 feeder), 0x2002 the
    resolution the printer will really scan at (x, y: its own limits applied), 0x2003 the maximum
    area at that resolution (pixels; height 0 for a feeder the printer does not have)."""
    for start in range(min(8, len(payload))):
        i, found = start, {}
        while i + 4 <= len(payload):
            tid, ln = struct.unpack_from("<HH", payload, i)
            if tid >> 8 != 0x20 or i + 4 + ln > len(payload):
                break
            found[tid] = payload[i + 4 : i + 4 + ln]
            i += 4 + ln
        if found:
            return found
    return {}


def max_area(payload: bytes) -> tuple[int, int] | None:
    """TLV 0x2003 (maximum area) of the SSP answer."""
    v = answer_fields(payload).get(0x2003, b"")
    return struct.unpack_from("<II", v) if len(v) >= 8 else None


def answer_resolution(payload: bytes) -> tuple[int, int] | None:
    """TLV 0x2002: the resolution (x, y) the printer will really scan at."""
    v = answer_fields(payload).get(0x2002, b"")
    return struct.unpack_from("<HH", v) if len(v) >= 4 else None


def ask(printer: str, resolution: int, color: str, source: str = "FB", port: int = PORT) -> bytes:
    """The printer's answer to the scan settings, without a scan: the connection closes before the
    command that starts one. The printer then refuses its scan port for about 35 s (06-11-2026): only
    for the capabilities probe (capabilities.py), never between scans."""
    with socket.create_connection((printer, port), timeout=15) as sock:
        r = _Reader(sock, time.monotonic() + 30)
        greeting = r.line()
        if greeting.startswith(b"-NG"):
            raise PrinterBusy(greeting.strip().decode("ascii", "replace"))
        if not greeting.startswith(b"+OK 200"):
            raise ScanError(f"unexpected greeting {greeting[:32]!r}")
        sock.sendall(ssp_command(resolution, color, source))
        head = r.exact(6)
        if head[:4] != b"\x00SSP":
            raise ScanError(f"unexpected answer to SSP: {head.hex()}")
        return r.exact(struct.unpack_from("<H", head, 4)[0])


log = logging.getLogger(__name__)

# Longest silence within a scan. 30 s was too short once: a colour scan right after a
# black-and-white one failed after 45 s (01-11-2026, cause not seen; most likely the end of page
# below, which a 60 s wait also could not survive).
IDLE_TIMEOUT = 60
# After the end-of-page marker the printer sends 4 closing bytes. From the Scan key it sent 2 and
# held the rest (a gray page, 06-11-2026: "nothing for 60 s, 2 of 4 bytes", the page itself
# complete), most likely until "scan another page?" on its display is answered. The page is in by
# then: those bytes are waited for this long, and not needed.
TAIL_TIMEOUT = 2.0


class _Reader:
    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self.sock = sock
        self.deadline = deadline

    def exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise ScanError("printer took too long")
            self.sock.settimeout(min(left, IDLE_TIMEOUT))
            try:
                chunk = self.sock.recv(min(65536, n - len(out)))
            except TimeoutError as e:
                raise ScanError(
                    f"printer stopped sending (nothing for {IDLE_TIMEOUT} s, {len(out)} of {n} bytes of this block)"
                ) from e
            if not chunk:
                raise ScanError(f"printer closed the connection ({len(out)} of {n} bytes)")
            out += chunk
        return bytes(out)

    def tail(self, n: int, timeout: float) -> bytes:
        """Up to n bytes that arrive within timeout seconds; what came, without an error."""
        out = bytearray()
        end = time.monotonic() + timeout
        while len(out) < n:
            left = end - time.monotonic()
            if left <= 0:
                break
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(n - len(out))
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def line(self, limit: int = 64) -> bytes:
        out = bytearray()
        while not out.endswith(b"\r\n"):
            if len(out) >= limit:
                raise ScanError(f"no greeting from the printer: {bytes(out)[:32]!r}")
            out += self.exact(1)
        return bytes(out)


@dataclass
class Bilevel:
    """A 1-bit page: rows of width_bytes bytes, most significant bit first, 1 = black."""

    width_bytes: int
    rows: bytes

    @property
    def height(self) -> int:
        return len(self.rows) // self.width_bytes

    def to_pbm(self) -> bytes:
        return b"P4\n%d %d\n" % (self.width_bytes * 8, self.height) + self.rows


def unpackbits(data: bytes, limit: int) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        n = data[i]
        i += 1
        if n < 128:
            if i + n + 1 > len(data):
                raise ScanError("truncated packbits literal")
            out += data[i : i + n + 1]
            i += n + 1
        elif n > 128:
            if i >= len(data):
                raise ScanError("truncated packbits run")
            out += bytes([data[i]]) * (257 - n)
            i += 1
        if len(out) > limit:
            raise ScanError("packbits line longer than the limit")
    return bytes(out)


def mm_to_px(mm: float, resolution: int) -> int:
    return int(mm / 25.4 * resolution)


def scan_page(
    printer: str,
    resolution: int = 300,
    color: str = "gray",
    paper_mm: tuple[float, float] = (210.0, 297.0),
    port: int = PORT,
    total_timeout: float | None = None,
    info: dict[str, object] | None = None,
) -> bytes | Bilevel:
    """Scans one page from the flatbed: JPEG bytes, or a Bilevel page for color="bw".

    total_timeout: 180 s up to 600 dpi, longer above (an A4 colour page at 1200 dpi took 122 s,
    06-11-2026). info, when given, gets "resolution": the (x, y) the printer said it scans at."""
    if total_timeout is None:
        total_timeout = 180.0 * max(1.0, resolution / 600)
    deadline = time.monotonic() + total_timeout
    with socket.create_connection((printer, port), timeout=15) as sock:
        r = _Reader(sock, deadline)
        greeting = r.line()
        if greeting.startswith(b"-NG"):
            raise PrinterBusy(greeting.strip().decode("ascii", "replace"))
        if not greeting.startswith(b"+OK 200"):
            raise ScanError(f"unexpected greeting {greeting[:32]!r}")

        sock.sendall(ssp_command(resolution, color))
        head = r.exact(6)
        if head[:4] != b"\x00SSP":
            raise ScanError(f"unexpected answer to SSP: {head.hex()}")
        payload = r.exact(struct.unpack_from("<H", head, 4)[0])
        if info is not None:
            info["resolution"] = answer_resolution(payload)
        width, height = mm_to_px(paper_mm[0], resolution), mm_to_px(paper_mm[1], resolution)
        area = max_area(payload)
        if area:
            width, height = min(width, area[0]), min(height, area[1])

        sock.sendall(xsc_command(resolution, width, height))
        jpeg = bytearray()
        rows, width_bytes = bytearray(), None
        while True:
            kind = r.exact(2)
            if kind == b"\x00\x00":
                continue
            if kind == b"\x00\x02":
                rest = r.exact(12)
                length = struct.unpack_from("<H", rest, 4)[0]
                if len(jpeg) + length > MAX_PAGE_BYTES:
                    raise ScanError("page larger than the limit")
                jpeg += r.exact(length)
                continue
            if kind == b"\x00\x01":
                rest = r.exact(12)
                line = unpackbits(r.exact(struct.unpack_from("<H", rest, 4)[0]), 4096)
                if width_bytes is None:
                    width_bytes = len(line)
                if len(line) != width_bytes or not width_bytes:
                    raise ScanError(f"line of {len(line)} bytes in a page of {width_bytes}-byte lines")
                if len(rows) + len(line) > MAX_PAGE_BYTES:
                    raise ScanError("page larger than the limit")
                rows += line
                continue
            if kind == b"\x00\x21":
                closing = r.tail(4, TAIL_TIMEOUT)
                if len(closing) < 4:
                    log.info("end of page: %d of 4 closing bytes (the printer may ask for another page)", len(closing))
                break
            raise ScanError(f"unknown block {kind.hex()} after {len(jpeg)} bytes")
    if color == "bw":
        if jpeg or not rows:
            raise ScanError("expected a black-and-white page")
        assert width_bytes is not None  # set by the first row, and there are rows
        return Bilevel(width_bytes, bytes(rows))
    if not (jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")):
        raise ScanError(f"the page is not a complete JPEG ({len(jpeg)} bytes)")
    return bytes(jpeg)
