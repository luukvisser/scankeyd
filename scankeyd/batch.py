"""Collects pages into documents and delivers each as a folder of the pages as scanned.

A document holds the pages of one printer function (and so one colour
mode). It finishes BATCH_TIMEOUT seconds after its last page, at once for a
profile with close/single, or when a page of another function arrives.

Two steps, so delivering never holds up the next scan:
  seal     (quick, under the lock): the open pages move to DATA_DIR/out/<name>/
  deliver  (outside the lock): OUTPUT_DIR/<function>/<name>/ with the pages
           (page-0001.jpg: the printer's JPEG byte for byte; page-0001.pbm:
           black-and-white, the printer's 1-bit lines) and document.json
The pages are not re-encoded or combined: making a PDF, straightening and
text recognition are steps of their own after the folder (README, "Steps").
Nothing is lost on a restart: open pages are sealed and sealed documents
delivered at start. A document folder is written as a hidden .<name>.part
folder and renamed when complete, so a folder watcher never sees half a
document. A document with a page that is neither a JPEG nor a binary PBM
moves to DATA_DIR/failed/; a write error (full disk, permissions) is retried
with a growing pause.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from typing import TYPE_CHECKING, cast

from . import __version__, counters
from .config import FUNCTIONS, Config, Profile

if TYPE_CHECKING:
    from collections.abc import Callable

    from .scanproto import Bilevel

log = logging.getLogger(__name__)

RETRY_FIRST, RETRY_MAX = 30, 1800
FORMAT = 1  # document.json's format; raised on an incompatible change
UNKNOWN_FUNCTION = "unknown"  # documents sealed by brscan-free (the earlier name), which kept no function
PBM_RE = re.compile(rb"P4\s+(\d+)\s+(\d+)\s")
# -rNNN: a page's turn, written by brscan-free (the earlier name); ignored
PAGE_RE = re.compile(r"^page-(\d{4})-(\d{8}-\d{6})(?:-r\d{3})?\.(jpg|pbm)$")


def page_field(name: str, group: int) -> str:
    """A field of a page file name; only for names already matched against PAGE_RE."""
    m = PAGE_RE.match(name)
    assert m is not None, name
    return m.group(group)


def safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "scan"


def check_page(data: bytes, ext: str) -> None:
    """Raises ValueError for a page that is not what its name says: a JPEG (start and end marker)
    or a binary PBM whose size matches its header."""
    if ext == "jpg":
        if not (data.startswith(b"\xff\xd8") and data.rstrip(b"\x00").endswith(b"\xff\xd9")):
            raise ValueError("not a complete JPEG")
        return
    m = PBM_RE.match(data)
    if not m or len(data) - m.end() != (int(m.group(1)) + 7) // 8 * int(m.group(2)):
        raise ValueError("not a binary PBM of its stated size")


def _write_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Batch:
    def __init__(
        self,
        cfg: Config,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        synchronous: bool = True,
        subdir: str = "",
    ) -> None:
        """synchronous: write documents in the caller's thread (tests, the scan command); the daemon
        passes False and runs writer() in a thread of its own."""
        self.cfg = cfg
        self.clock = clock
        self.monotonic = monotonic
        self.synchronous = synchronous
        root = os.path.join(cfg.data_dir, subdir) if subdir else cfg.data_dir
        self.dir = os.path.join(root, "batch")
        self.out_dir = os.path.join(root, "out")
        self.failed_dir = os.path.join(root, "failed")
        self.profile_path = os.path.join(self.dir, "profile.json")
        for d in (self.dir, self.out_dir, cfg.output_dir):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.seq = 0
        self.last_page = 0.0
        self.retry_at: dict[str, tuple[float, int]] = {}  # sealed name -> (monotonic time, pause)
        self.last_written: str | None = None

    # ---- the open document -------------------------------------------------------------

    def pages(self) -> list[str]:
        return sorted(os.path.join(self.dir, f) for f in os.listdir(self.dir) if PAGE_RE.match(f))

    def _profile(self, directory: str | None = None) -> Profile | None:
        try:
            with open(os.path.join(directory, "profile.json") if directory else self.profile_path) as f:
                d = json.load(f)
            return Profile(str(d["color"]), ocr=bool(d.get("ocr")), resolution=int(d.get("resolution") or 0))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _function(self, directory: str | None = None) -> str:
        """The open (or a sealed) document's printer function, lower case for its folder."""
        try:
            with open(os.path.join(directory, "profile.json") if directory else self.profile_path) as f:
                func = str(json.load(f)["function"]).upper()
            return func.lower() if func in FUNCTIONS else UNKNOWN_FUNCTION
        except (OSError, ValueError, KeyError, TypeError):
            return UNKNOWN_FUNCTION

    def add(self, page: bytes | Bilevel, profile: Profile, function: str = "FILE") -> None:
        """page: JPEG bytes, or a scanproto.Bilevel (anything with to_pbm()); function: the printer
        function pressed (FILE, IMAGE, OCR, EMAIL), which names the document's folder."""
        with self.lock:
            current = self._profile()
            pages = self.pages()
            if pages and (
                profile.finish == "single"
                or current is None
                or current.color != profile.color
                or self.cfg.dpi(current) != self.cfg.dpi(profile)  # one resolution per document (its PDF)
                or self._function() != function.lower()
            ):
                self._seal()
                pages = []
            if not pages:
                with open(self.profile_path + ".tmp", "w") as f:
                    json.dump(
                        {
                            "color": profile.color,
                            "function": function.upper(),
                            "ocr": profile.ocr,
                            "resolution": self.cfg.dpi(profile),
                        },
                        f,
                    )
                os.replace(self.profile_path + ".tmp", self.profile_path)
            number = max((int(page_field(os.path.basename(p), 1)) for p in pages), default=0) + 1
            data, ext = (cast("Bilevel", page).to_pbm(), "pbm") if hasattr(page, "to_pbm") else (page, "jpg")
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
            _write_atomic(os.path.join(self.dir, f"page-{number:04d}-{stamp}.{ext}"), data)
            self.last_page = self.monotonic()
            log.info("page added (%s %s, %d in this document)", function.upper(), profile.color, len(pages) + 1)
            counters.page(self.cfg.data_dir, function.upper())
            if profile.finish in ("close", "single"):
                self._seal()
        self._after_seal()

    def tick(self) -> None:
        with self.lock:
            if self.pages() and self.monotonic() - self.last_page >= self.cfg.batch_timeout:
                self._seal()
        self._after_seal()

    def open_pages(self) -> int:
        with self.lock:
            return len(self.pages())

    def recover(self) -> None:
        with self.lock:
            if self.pages():
                log.info("%d page(s) left from before a restart: finishing that document", len(self.pages()))
                self._seal()
        self.write_pending()

    def _name(self, first_page: str, folder: str) -> str:
        """Named after the first page's scan time (not the moment the batch closed, which can be the
        next function's press, 01-11-2026)."""
        m = PAGE_RE.match(os.path.basename(first_page))
        try:
            now = time.strptime(m.group(2), "%Y%m%d-%H%M%S") if m else time.localtime(self.clock())
        except ValueError:
            now = time.localtime(self.clock())
        self.seq += 1
        base = safe(
            self.cfg.filename.format(
                date=time.strftime("%Y-%m-%d", now),
                time=time.strftime("%H%M%S", now),
                target=self.cfg.target_name,
                seq=self.seq,
                function=folder,  # file, ocr, image, email (or unknown)
            )
        )
        name, n = base, 1
        while os.path.exists(os.path.join(self.cfg.output_dir, folder, name)) or os.path.exists(
            os.path.join(self.out_dir, name)
        ):
            n += 1
            name = f"{base}_{n}"
        return name

    def _seal(self) -> None:
        """Moves the open pages into out/<name>/ (caller holds the lock)."""
        pages = self.pages()
        if not pages:
            return
        name = self._name(pages[0], self._function())
        target = os.path.join(self.out_dir, name)
        os.makedirs(target)
        for p in pages:
            os.replace(p, os.path.join(target, os.path.basename(p)))
        if os.path.exists(self.profile_path):
            os.replace(self.profile_path, os.path.join(target, "profile.json"))

    def _after_seal(self) -> None:
        if self.synchronous:
            self.write_pending()
        else:
            self.wake.set()

    # ---- writing sealed documents (outside the lock) -------------------------------------

    def write_pending(self) -> None:
        for name in sorted(os.listdir(self.out_dir)):
            due = self.retry_at.get(name)
            if due and self.monotonic() < due[0]:
                continue
            self._write(name)

    def writer(self, stop: threading.Event) -> None:
        """The daemon's writer thread."""
        while not stop.is_set():
            self.write_pending()
            self.wake.wait(5)
            self.wake.clear()

    def _write(self, name: str) -> None:
        src = os.path.join(self.out_dir, name)
        files = sorted(f for f in os.listdir(src) if PAGE_RE.match(f))
        folder = self._function(src)
        profile = self._profile(src)
        try:
            pages = []
            for n, f in enumerate(files, 1):
                ext = page_field(f, 3)
                with open(os.path.join(src, f), "rb") as fh:
                    data = fh.read()
                check_page(data, ext)
                pages.append((f"page-{n:04d}.{ext}", data))
        except (ValueError, OSError) as e:
            self._fail(name, f"its pages cannot be read: {e}")
            return
        if not pages:
            self._fail(name, "no pages")
            return
        m = PAGE_RE.match(files[0])
        try:
            scanned = time.strftime("%Y-%m-%dT%H:%M:%S", time.strptime(m.group(2), "%Y%m%d-%H%M%S")) if m else ""
        except ValueError:
            scanned = ""
        document = {
            "format": FORMAT,
            "function": folder.upper() if folder != UNKNOWN_FUNCTION else None,
            "color": profile.color if profile else None,
            "ocr": profile.ocr if profile else False,  # the function's wish: a text layer, by a later step
            "resolution": self.cfg.dpi(profile) if profile else self.cfg.resolution,
            "pages": [p for p, _ in pages],
            "scanned": scanned,  # local time of the first page
            "target": self.cfg.target_name,
            "scankeyd": __version__,
        }
        dest_dir = os.path.join(self.cfg.output_dir, folder)
        part = os.path.join(dest_dir, f".{name}.part")
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.rmtree(part, ignore_errors=True)  # a half one from an earlier attempt
            os.makedirs(part)
            for p, data in pages:
                with open(os.path.join(part, p), "wb") as f:
                    f.write(data)
            with open(os.path.join(part, "document.json"), "w") as f:
                json.dump(document, f, indent=1)
            final, n = os.path.join(dest_dir, name), 1
            while os.path.exists(final):  # taken meanwhile (the scan command's document of the same name)
                n += 1
                final = os.path.join(dest_dir, f"{name}_{n}")
            os.replace(part, final)
        except OSError as e:
            pause = min(RETRY_MAX, self.retry_at.get(name, (0, RETRY_FIRST // 2))[1] * 2)
            if name not in self.retry_at:
                log.error("delivering %s failed (%s); kept in %s, retrying with growing pauses", name, e, src)
            self.retry_at[name] = (self.monotonic() + pause, pause)
            return
        self.retry_at.pop(name, None)
        shutil.rmtree(src, ignore_errors=True)
        self.last_written = f"{folder}/{os.path.basename(final)}"
        counters.document(self.cfg.data_dir, folder.upper(), self.last_written)
        log.info("document delivered: %s (%d page(s))", self.last_written, len(pages))

    def _fail(self, name: str, why: str) -> None:
        os.makedirs(self.failed_dir, exist_ok=True)
        dest, n = os.path.join(self.failed_dir, name), 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(self.failed_dir, f"{name}_{n}")
        os.replace(os.path.join(self.out_dir, name), dest)
        self.retry_at.pop(name, None)
        counters.failure(self.cfg.data_dir, "documents_failed")
        log.error("document %s not written, %s; its files are in %s", name, why, dest)
