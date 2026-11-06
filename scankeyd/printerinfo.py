"""Who the printer is: model, serial number and more, from its eSCL capabilities page (read-only).

eSCL (Apple's AirScan, which most network printers serve over plain HTTP) describes the scanner at
/eSCL/ScannerCapabilities. scankeyd reads it only to show the printer in `doctor` and in Home
Assistant; scanning itself uses Brother's protocol. Brother names itself BRW (Wi-Fi) or BRN
(wired) plus its MAC address in the admin link, which is where the MAC comes from. A printer
without eSCL gives nothing: every field is optional.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request

TIMEOUT = 5
FIELDS = {
    "model": "pwg:MakeAndModel",
    "manufacturer": "scan:Manufacturer",
    "serial_number": "pwg:SerialNumber",
    "uuid": "scan:UUID",
    "escl_version": "pwg:Version",
    "admin_url": "scan:AdminURI",
}


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, tag in FIELDS.items():
        m = re.search(rf"<{re.escape(tag)}>([^<]{{1,200}})</{re.escape(tag)}>", text)
        if m:
            out[key] = m.group(1).strip()
    host = urllib.parse.urlsplit(out.get("admin_url", "")).hostname or ""
    m = re.match(r"^br[nw]([0-9a-f]{12})\b", host, re.I)
    if m:
        out["mac"] = ":".join(m.group(1)[i : i + 2] for i in range(0, 12, 2)).lower()
    return out


def fetch(printer: str) -> dict[str, str]:
    """The printer's description; {} when it has no eSCL or does not answer."""
    try:
        with urllib.request.urlopen(f"http://{printer}/eSCL/ScannerCapabilities", timeout=TIMEOUT) as r:
            return parse(r.read(262144).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
