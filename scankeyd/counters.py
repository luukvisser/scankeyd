"""Counts for long-term statistics, and the last scan and document, in DATA_DIR/counters.json.

The running service and the `scan` command both scan, in two processes, so every change is a
read-modify-write under an exclusive lock on the file (fcntl), written as a temporary file and
renamed. The counts only ever go up; they survive restarts and updates. The service passes them on
to Home Assistant (ha.py) when the file changes.

  pages, documents                 by printer function, e.g. {"FILE": 12, "OCR": 3}
  scan_failures                    scans that gave no page (printer busy, a protocol error, a page
                                   that could not be stored)
  documents_failed                 documents moved to failed/ (a page neither JPEG nor PBM)
  last_scan_at, last_document      the last page's time (epoch seconds) and the last document
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

log = logging.getLogger(__name__)

FILE = "counters.json"


def empty() -> dict[str, Any]:
    return {
        "pages": {},
        "documents": {},
        "scan_failures": 0,
        "documents_failed": 0,
        "last_scan_at": None,
        "last_document": None,
    }


def read(data_dir: str) -> dict[str, Any]:
    try:
        with open(os.path.join(data_dir, FILE)) as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        log.warning("%s not read (%s); counting from zero", FILE, e)
        return empty()
    out = empty()
    out.update({k: v for k, v in data.items() if k in out})
    return out


@contextmanager
def _locked(data_dir: str) -> Generator[dict[str, Any]]:
    path = os.path.join(data_dir, FILE)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(data_dir)
        yield data
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def page(data_dir: str, function: str) -> None:
    try:
        with _locked(data_dir) as c:
            c["pages"][function] = int(c["pages"].get(function, 0)) + 1
            c["last_scan_at"] = time.time()
    except OSError as e:
        log.warning("page not counted (%s)", e)


def document(data_dir: str, function: str, name: str) -> None:
    try:
        with _locked(data_dir) as c:
            c["documents"][function] = int(c["documents"].get(function, 0)) + 1
            c["last_document"] = name
    except OSError as e:
        log.warning("document not counted (%s)", e)


def failure(data_dir: str, key: str) -> None:
    """key: scan_failures or documents_failed."""
    try:
        with _locked(data_dir) as c:
            c[key] = int(c.get(key) or 0) + 1
    except OSError as e:
        log.warning("failure not counted (%s)", e)


def mtime(data_dir: str) -> float:
    try:
        return os.stat(os.path.join(data_dir, FILE)).st_mtime
    except OSError:
        return 0.0
