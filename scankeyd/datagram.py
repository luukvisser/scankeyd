"""The button datagram the printer sends to UDP 54925 when its Scan key is used.

Format (observed): bytes 0x02 0x00 <any> 0x30, then ASCII text of
KEY=value; pairs, e.g. TYPE=BR;BUTTON=SCAN;USER="Scan";FUNC=FILE;HOST=...;
APPNUM=5;P1=0;P2=0;P3=0;P4=0;REGID=17638;SEQ=1;
The printer repeats a datagram after a few seconds; REGID+SEQ identify it.

Everything here is untrusted input: the parser never raises on bad data, it
returns None.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_LEN = 1024
MAX_FIELDS = 32


@dataclass(frozen=True)
class ButtonPress:
    user: str
    func: str
    regid: str
    seq: str
    fields: dict[str, str]


def parse(data: bytes) -> ButtonPress | None:
    if not 4 < len(data) <= MAX_LEN:
        return None
    if data[0] != 0x02 or data[1] != 0x00 or data[3] != 0x30:
        return None
    try:
        text = data[4:].decode("ascii")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 0x20 for c in text):
        return None
    fields: dict[str, str] = {}
    for item in text.split(";"):
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.isalnum() or len(fields) >= MAX_FIELDS:
            return None
        fields[key] = value
    user = fields.get("USER", "")
    if len(user) >= 2 and user[0] == user[-1] == '"':
        user = user[1:-1]
    if fields.get("TYPE") != "BR" or fields.get("BUTTON") != "SCAN" or not user or "FUNC" not in fields:
        return None
    return ButtonPress(
        user=user, func=fields["FUNC"], regid=fields.get("REGID", ""), seq=fields.get("SEQ", ""), fields=fields
    )


ANONYMOUS_WINDOW = 10.0  # seconds: a press without REGID and SEQ cannot be told from the next one


class Deduplicator:
    """Drops repeats of the same press (same REGID and SEQ) within a window; a printer that sends
    neither has its repeats (2-10 s apart on the DCP) dropped within ANONYMOUS_WINDOW only, so that a
    second press a little later still scans."""

    def __init__(
        self, window: float = 60.0, clock: Callable[[], float] = time.monotonic, max_entries: int = 1000
    ) -> None:
        self.window = window
        self.max_entries = max_entries
        self.clock = clock
        self.seen: dict[tuple[str, str, str], float] = {}

    def is_new(self, sender: str, press: ButtonPress) -> bool:
        now = self.clock()
        self.seen = {
            k: t
            for k, t in self.seen.items()
            if now - t < (self.window if k[1] or k[2] else min(self.window, ANONYMOUS_WINDOW))
        }
        key = (sender, press.regid, press.seq)
        if key in self.seen:
            return False
        if len(self.seen) >= self.max_entries:  # a flood of made-up presses: forget the oldest
            del self.seen[min(self.seen, key=self.seen.__getitem__)]
        self.seen[key] = now
        return True
