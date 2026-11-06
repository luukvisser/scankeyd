"""What the connected scanner really does, per colour mode: its highest real resolution (x and y),
and whether it has a document feeder. From the printer itself, once per printer.

The printer answers the scan settings with the resolution it will really scan at (scanproto,
answer 0x2002), its own limits applied: asked for far more than it can, it names its maximum. The
DCP-L2627DWE (06-11-2026): black-and-white and gray 600 x 1200, colour 1200 x 1200; asked for 1200
in gray it delivers 1200 x 1200 pixels, filled in across. So the probe asks once per colour mode at
PROBE_DPI, and once for the feeder (a maximum height of 0: none). An asked-and-not-scanned session
makes the printer refuse its scan port for about 35 s, hence PAUSE between the questions and only
when nothing is being scanned; the result is kept in DATA_DIR/capabilities.json per serial number.

The options are the resolutions to ask for (STEPS), each with what the printer really does at it;
two steps the printer does the same are one option (the lower one is asked for). Before a probe,
or for a printer that names no resolution, the eSCL standard 100-600 dpi, square.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from . import scanproto
from .config import COLORS

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

STEPS = (100, 200, 300, 600, 1200, 2400)
PROBE_DPI = 9600  # far above any flatbed: the printer answers with its maximum
PAUSE = 45.0  # seconds between the probe's questions (the port is refused for about 35 s)
FILE = "capabilities.json"
DEFAULT_MAX = (1200, 1200)  # until the probe has run: taken as asked up to here (not to show 600 for 1200)


class Capabilities:
    def __init__(self, maximum: dict[str, tuple[int, int]] | None = None, feeder: bool | None = None) -> None:
        self.maximum = maximum or {}  # colour -> (x, y)
        self.feeder = feeder
        self.probed = bool(maximum)

    def real(self, color: str, asked: int) -> tuple[int, int]:
        mx, my = self.maximum.get(color, DEFAULT_MAX)
        return min(asked, mx), min(asked, my)

    def options(self, color: str) -> list[tuple[int, tuple[int, int]]]:
        """(the dpi to ask for, what the printer really scans at), lowest first, each real one once."""
        out: list[tuple[int, tuple[int, int]]] = []
        seen: set[tuple[int, int]] = set()
        for step in STEPS:
            real = self.real(color, step)
            if real not in seen:  # a step beyond the maximum gives the same real one: left out
                seen.add(real)
                out.append((step, real))
        return out

    def nearest(self, color: str, asked: int) -> int:
        """The option to ask for that matches asked best: the same real resolution, or the highest
        one not above it."""
        real = self.real(color, asked)
        opts = self.options(color)
        for step, r in opts:
            if r == real:
                return step
        return max((s for s, r in opts if max(r) <= max(real)), default=opts[0][0])

    def to_json(self, serial: str) -> dict[str, Any]:
        return {
            "serial_number": serial,
            "probed": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "maximum": {c: list(v) for c, v in self.maximum.items()},
            "feeder": self.feeder,
        }


def label(real: tuple[int, int]) -> str:
    return f"{real[0]} × {real[1]} dpi"


def load(data_dir: str, serial: str) -> Capabilities | None:
    """The kept probe result for this printer (by serial number), or None."""
    try:
        with open(os.path.join(data_dir, FILE)) as f:
            d = json.load(f)
        if d.get("serial_number") != serial:
            return None
        maximum = {c: (int(v[0]), int(v[1])) for c, v in d["maximum"].items() if c in COLORS}
        return Capabilities(maximum, d.get("feeder"))
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def save(data_dir: str, serial: str, caps: Capabilities) -> None:
    path = os.path.join(data_dir, FILE)
    with open(path + ".tmp", "w") as f:
        json.dump(caps.to_json(serial), f, indent=1)
    os.replace(path + ".tmp", path)


def probe(
    printer: str,
    busy: Callable[[], bool],
    wait: Callable[[float], bool],
    port: int = scanproto.PORT,
    pause: float = PAUSE,
) -> Capabilities | None:
    """Asks the printer per colour mode and for its feeder. busy(): a scan is wanted, so give way
    (None: try again later). wait(seconds): sleeps, True when the service stops."""
    maximum: dict[str, tuple[int, int]] = {}
    questions: list[tuple[str, str]] = [(c, "FB") for c in COLORS] + [("gray", "ADF")]
    feeder = None
    for n, (color, source) in enumerate(questions):
        if n and wait(pause):
            return None
        if busy():
            log.info("capabilities probe postponed: a scan is wanted")
            return None
        try:
            answer = scanproto.ask(printer, PROBE_DPI if source == "FB" else 300, color, source, port)
        except scanproto.PrinterBusy:
            log.info("capabilities probe postponed: the printer is busy")
            return None
        except (scanproto.ScanError, OSError) as e:
            log.warning("capabilities probe: no answer for %s %s (%s)", color, source, e)
            continue
        if source == "FB":
            real = scanproto.answer_resolution(answer)
            if real and min(real) > 0:
                maximum[color] = real
        else:
            area = scanproto.max_area(answer)
            feeder = bool(area and area[1] > 0)
    if not maximum:
        return None
    caps = Capabilities(maximum, feeder)
    log.info(
        "scanner capabilities: %s; document feeder: %s",
        ", ".join(f"{c} up to {label(v)}" for c, v in maximum.items()),
        {True: "yes", False: "no", None: "unknown"}[feeder],
    )
    return caps
