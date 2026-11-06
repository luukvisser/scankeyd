"""The running service: registration loop, button listener, scan worker, writer, batch timer.

Every thread's loop catches what it expects and logs it. Anything else ends
the whole process (exit code 1), so the container restarts it: a service
whose scan thread died silently while the listener and the health check
carried on would be worse than a restart.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import signal
import socket
import threading
import time
from typing import TYPE_CHECKING

from . import capabilities, counters, datagram, printerinfo, scanproto
from .batch import Batch
from .config import PAPERS, Config
from .registration import PinMismatch, Registration, RegistrationError

PROBE_HOLD = 45.0  # seconds: after a capabilities question the printer refuses its scan port for about 35 s

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from .config import Profile

log = logging.getLogger(__name__)


class Status:
    def __init__(self, cfg: Config) -> None:
        self.path = os.path.join(cfg.data_dir, "status.json")
        self.lock = threading.Lock()
        self.listener: Callable[[dict[str, object]], None] | None = None  # the HA link, if any
        self.data: dict[str, object] = {
            "registered_at": None,
            "method": None,
            "last_error": None,
            "last_scan_at": None,
            "last_document": None,
            "open_pages": 0,
            "counters": counters.empty(),
        }

    def update(self, **kw: object) -> None:
        with self.lock:
            self.data.update(kw)
            tmp = f"{self.path}.{secrets.token_hex(4)}.tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(self.data, f)
                os.replace(tmp, self.path)
            except OSError as e:
                log.warning("status file not written: %s", e)
            snapshot = dict(self.data)
        if self.listener:
            try:
                self.listener(snapshot)
            except Exception:  # noqa: BLE001 - a broken link must not stop a scan
                log.exception("status not passed on to Home Assistant")

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return dict(self.data)


class Throttle:
    """Lets a repeated log message through at most once per interval."""

    def __init__(self, interval: float = 60.0) -> None:
        self.interval, self.last, self.dropped = interval, -1e9, 0

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self.last >= self.interval:
            self.last = now
            return True
        self.dropped += 1
        return False


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop = threading.Event()
        self.failure: BaseException | None = None
        self.jobs: queue.Queue[str] = queue.Queue(maxsize=8)  # printer function names
        self.status = Status(cfg)
        self.batch = Batch(cfg, synchronous=False)
        self.registration = Registration(cfg)
        # what the scanner really does (capabilities.py): kept per printer, else probed once when idle
        self.printer_info = printerinfo.fetch(cfg.printer)
        self.serial = self.printer_info.get("serial_number") or cfg.printer
        self.caps = capabilities.load(cfg.data_dir, self.serial) or capabilities.Capabilities()
        self.probe_wanted = threading.Event()
        if not self.caps.probed:
            self.probe_wanted.set()
        self.scanning = False
        self.probing = False
        self.probe_ended = time.monotonic() - PROBE_HOLD
        self.last_activity = time.monotonic()
        self.ha = None
        if cfg.mqtt_host:
            from .ha import HaLink

            self.ha = HaLink(cfg, self.status, self.caps, self.probe_wanted.set, self.printer_info)
            # (applies the settings changed from HA before anything runs)
            self.status.listener = self.ha.on_status

    def _guard(self, loop: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                loop()
            except BaseException as e:  # noqa: BLE001 - see the module docstring
                if not self.stop.is_set():
                    name = getattr(loop, "__name__", repr(loop))
                    log.exception("%s stopped unexpectedly; the service exits so it is restarted", name)
                    self.failure = e
                    self.stop.set()

        return run

    def register_loop(self) -> None:
        failing = False
        while not self.stop.is_set():
            try:
                method = self.registration.renew()
                if failing:
                    log.info("registration works again (%s)", method)
                    if self.ha:  # the printer was away: read who it is again
                        self.ha.refresh_printer()
                failing = False
                self.status.update(registered_at=time.time(), method=method, last_error=None)
            except PinMismatch as e:
                log.error("%s", e)
                self.status.update(last_error=str(e))
                failing = True
            except (RegistrationError, OSError) as e:
                if not failing:
                    log.warning(
                        "registration failed: %s (retrying every %d s; logged again on recovery)",
                        e,
                        self.cfg.register_interval,
                    )
                failing = True
                self.status.update(last_error=str(e))
            self.stop.wait(self.cfg.register_interval)

    def _scan(self, profile: Profile) -> bytes | scanproto.Bilevel | None:
        # A busy printer gets about 6 s. Just after a capabilities question it refuses its scan port
        # for about 35 s, busy or not answering: then the scan keeps trying for that long, so a press
        # during the probe still gets its page.
        held = self.probing or time.monotonic() - self.probe_ended < PROBE_HOLD
        give_up = time.monotonic() + (PROBE_HOLD if held else 5)
        attempt = 0
        while True:
            attempt += 1
            try:
                info: dict[str, object] = {}
                asked = self.cfg.dpi(profile)
                page = scanproto.scan_page(self.cfg.printer, asked, profile.color, PAPERS[self.cfg.paper], info=info)
                real = info.get("resolution")
                if isinstance(real, tuple):
                    if real != (asked, asked):
                        log.info(
                            "scanned at %s (asked %d dpi; the printer's own maximum)", capabilities.label(real), asked
                        )
                    self.status.update(last_resolution=capabilities.label(real))
                return page
            except scanproto.PrinterBusy as e:
                log.info("printer busy (%s), attempt %d", e, attempt)
            except OSError as e:
                if not held:
                    log.error("scan failed: %s", e)
                    counters.failure(self.cfg.data_dir, "scan_failures")
                    return None
                log.info(
                    "printer not taking the scan yet (%s; just after a capabilities question), attempt %d", e, attempt
                )
            except scanproto.ScanError as e:
                log.error("scan failed: %s", e)
                counters.failure(self.cfg.data_dir, "scan_failures")
                return None
            if time.monotonic() >= give_up or self.stop.wait(2):
                break
        log.error("printer stayed busy; scan given up")
        counters.failure(self.cfg.data_dir, "scan_failures")
        return None

    def scan_worker(self) -> None:
        while not self.stop.is_set():
            try:
                func = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            profile = self.cfg.profiles[func]
            self.scanning = True  # from here the prober gives way (it looks before every question)
            try:
                # what was chosen on the panel for this press (logged for now, not used: 06-11-2026)
                panel = scanproto.panel_settings(self.cfg.printer)
                if panel:
                    log.info("panel settings for this press: %s", panel)
                page = self._scan(profile)
            finally:
                self.scanning = False
                self.last_activity = time.monotonic()
            if page is None:
                continue
            try:
                self.batch.add(page, profile, func)
            except OSError as e:
                log.error("page could not be stored (%s); it is lost, scan it again", e)
                counters.failure(self.cfg.data_dir, "scan_failures")
                continue
            self.status.update(open_pages=self.batch.open_pages())

    def prober(self) -> None:
        """Asks the printer what it really does, once per printer (or when HA asks), when nothing has
        been scanned for a minute; a wanted scan stops it, and it tries again ten minutes later."""
        while not self.stop.is_set():
            if not self.probe_wanted.wait(5):
                continue
            if self.scanning or not self.jobs.empty() or time.monotonic() - self.last_activity < 60:
                self.stop.wait(10)
                continue
            log.info("reading the scanner's capabilities (about 3 minutes; a scan goes first)")
            self.probing = True
            try:
                caps = capabilities.probe(
                    self.cfg.printer, lambda: self.scanning or not self.jobs.empty(), self.stop.wait
                )
            finally:
                self.probing = False
                self.probe_ended = time.monotonic()
            if caps is None:
                self.stop.wait(600)
                continue
            self.caps.maximum, self.caps.feeder, self.caps.probed = caps.maximum, caps.feeder, True
            try:
                capabilities.save(self.cfg.data_dir, self.serial, self.caps)
            except OSError as e:
                log.warning("capabilities not kept (%s); read again at the next start", e)
            self.probe_wanted.clear()
            self.last_activity = time.monotonic()
            if self.ha:
                self.ha.capabilities_changed()

    def writer(self) -> None:
        self.batch.writer(self.stop)

    def batch_timer(self) -> None:
        seen = -1.0
        while not self.stop.wait(1):
            try:
                self.batch.tick()
            except OSError as e:
                log.error("finishing the open document failed (%s); retrying", e)
            # counters.json changes with every page, document and failure, of this service and of
            # the scan command alike
            changed = counters.mtime(self.cfg.data_dir)
            if changed != seen:
                seen = changed
                c = counters.read(self.cfg.data_dir)
                self.status.update(
                    counters=c,
                    last_scan_at=c["last_scan_at"],
                    last_document=c["last_document"],
                    open_pages=self.batch.open_pages(),
                )

    def listen(self) -> None:
        dedup = datagram.Deduplicator()
        stranger, malformed = Throttle(), Throttle()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", self.cfg.listen_port))
            sock.settimeout(1)
            try:
                advertised = self.registration.host
            except RegistrationError as e:
                advertised = f"? ({e})"
            log.info(
                "listening for the Scan key on UDP %d; target %r on printer %s, advertised as %s",
                self.cfg.listen_port,
                self.cfg.target_name,
                self.cfg.printer,
                advertised,
            )
            while not self.stop.is_set():
                try:
                    data, (sender, _) = sock.recvfrom(2048)
                except TimeoutError:
                    continue
                if sender != self.cfg.printer:
                    if stranger.allow():
                        log.info("datagram from %s ignored (not the printer)", sender)
                    continue
                press = datagram.parse(data)
                if press is None:
                    if malformed.allow():
                        log.warning("malformed datagram from the printer ignored (%d bytes)", len(data))
                    continue
                if press.user != self.cfg.target_name or press.func not in self.cfg.profiles:
                    log.info("press for %r/%s is not ours; ignored", press.user, press.func)
                    continue
                if not dedup.is_new(sender, press):
                    continue
                p = self.cfg.profiles[press.func]
                # the printer's other fields (P1-P4 and any new ones): what its panel may pass on
                extra = " ".join(
                    f"{k}={v}"
                    for k, v in press.fields.items()
                    if k not in ("TYPE", "BUTTON", "USER", "FUNC", "HOST", "REGID", "SEQ")
                )
                log.info(
                    "Scan key: %s -> %s%s (printer sent %s)",
                    press.func,
                    p.color,
                    f", {p.finish}" if p.finish else "",
                    extra or "nothing else",
                )
                try:
                    self.jobs.put_nowait(press.func)
                except queue.Full:
                    log.warning("too many scans queued; press ignored")

    def run(self) -> int:
        def on_term(signum: int, _frame: FrameType | None) -> None:
            log.info(
                "signal %d: stopping (open pages stay in %s and are finished at the next start)", signum, self.batch.dir
            )
            self.stop.set()

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

        self.batch.recover()
        self.status.update(open_pages=self.batch.open_pages())
        threads = [
            threading.Thread(target=self._guard(f), name=f.__name__, daemon=True)
            for f in (self.register_loop, self.scan_worker, self.writer, self.batch_timer, self.prober)
        ]
        for t in threads:
            t.start()
        if self.ha:
            self.ha.start()
        self._guard(self.listen)()
        self.stop.set()
        if self.ha:
            self.ha.stop()
        deadline = time.monotonic() + 3  # well within docker's 10 s; the disk state is safe anyway
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        return 1 if self.failure else 0
