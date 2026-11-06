"""The command line: run, scan, doctor and health (USAGE)."""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
import os
import socket
import sys
import time
import urllib.request

from . import __version__, config, scanproto
from .batch import Batch
from .registration import local_address_for

USAGE = """scankeyd [run|scan|doctor|health]

  run      the service (default)
  scan [FUNCTION]  scan one page now with that function's profile (FILE, IMAGE, OCR,
           EMAIL; default FILE) and write it as a document of its own
  doctor   check the configuration, the printer and the network, read-only
  health   exit 0 when the last registration is recent (for a container healthcheck)
"""


def doctor(cfg: config.Config) -> int:
    ok = True

    def report(good: bool, text: str) -> None:
        nonlocal ok
        ok &= good
        print(("  ok   " if good else "  FAIL ") + text)

    print(f"scankeyd {__version__}: printer {cfg.printer}, target {cfg.target_name!r}")
    try:
        detected = local_address_for(cfg.printer)
    except OSError as e:
        report(False, f"no route to the printer: {e}")
        return 1
    advertised = cfg.advertise_ip or detected
    report(True, f"advertised address {advertised}" + ("" if cfg.advertise_ip else " (detected)"))
    if ipaddress.IPv4Address(advertised) in ipaddress.IPv4Network("172.16.0.0/12") and ipaddress.IPv4Address(
        cfg.printer
    ) not in ipaddress.IPv4Network("172.16.0.0/12"):
        report(
            False,
            "that looks like a Docker-internal address the printer cannot reach: set ADVERTISE_IP "
            "to this host's LAN address and publish UDP 54925, or use host networking",
        )
    for port, what in ((443, "HTTPS (registration)"), (scanproto.PORT, "scan protocol")):
        try:
            with socket.create_connection((cfg.printer, port), timeout=5) as s:
                if port == scanproto.PORT:
                    s.settimeout(5)
                    greeting = s.recv(16)
                    report(greeting.startswith(b"+OK"), f"port {port} {what}: {greeting.strip()[:16]!r}")
                else:
                    report(True, f"port {port} {what} reachable")
        except OSError as e:
            if port == 443 and cfg.register_method != "phoenix":
                print(f"  info port 443 not reachable ({e}): registration will use SNMP")
            else:
                report(False, f"port {port} {what}: {e}")
    try:
        with urllib.request.urlopen(f"http://{cfg.printer}/eSCL/ScannerCapabilities", timeout=5) as r:
            text = r.read(65536).decode("utf-8", "replace")
        model = text.split("<pwg:MakeAndModel>")[1].split("<")[0] if "<pwg:MakeAndModel>" in text else "?"
        report(True, f"model {model}")
    except (OSError, IndexError) as e:
        print(f"  info model unknown (no eSCL: {e})")
    try:
        os.makedirs(cfg.output_dir, exist_ok=True)
        probe = os.path.join(cfg.output_dir, ".scankeyd-write-test")
        with open(probe, "w"):
            pass
        os.unlink(probe)
        report(True, f"output folder {cfg.output_dir} writable")
    except OSError as e:
        report(False, f"output folder {cfg.output_dir}: {e}")
    print("all checks passed" if ok else "some checks failed")
    return 0 if ok else 1


def health(cfg: config.Config) -> int:
    try:
        with open(os.path.join(cfg.data_dir, "status.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return 1
    at = st.get("registered_at")
    return 0 if at and time.time() - at < 3 * cfg.register_interval else 1


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print("configuration errors:\n" + "\n".join(f"  - {p}" for p in e.problems), file=sys.stderr)
        return 2
    os.umask(cfg.umask)  # scans are often private: by default not readable by others
    logging.basicConfig(
        level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    if cmd == "doctor":
        return doctor(cfg)
    if cmd == "health":
        return health(cfg)
    if cmd == "scan":
        if cfg.mqtt_host:  # as a press of the button: with what was changed in Home Assistant
            from .ha import Overrides

            Overrides(cfg).load(cfg)
        func = (argv[2] if len(argv) > 2 else "FILE").upper()
        if func not in cfg.profiles:
            print(f"unknown function {func}; one of {', '.join(cfg.profiles)}", file=sys.stderr)
            return 2
        profile = dataclasses.replace(cfg.profiles[func], finish="single")
        page = scanproto.scan_page(cfg.printer, cfg.dpi(profile), profile.color, config.PAPERS[cfg.paper])
        batch = Batch(cfg, subdir="manual")  # never the running service's open document
        batch.add(page, profile, func)
        print(os.path.join(cfg.output_dir, batch.last_written or "?"))
        return 0
    if cmd == "run":
        from .daemon import Daemon

        logging.getLogger(__name__).info("scankeyd %s starting", __version__)
        try:
            daemon = Daemon(cfg)
        except OSError as e:
            print(
                f"cannot use {e.filename or 'a folder'}: {e.strerror}. OUTPUT_DIR and DATA_DIR must be writable "
                f"for uid {os.getuid()} (mount volumes owned by that user, or set `user:` to their owner)",
                file=sys.stderr,
            )
            return 2
        return daemon.run()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
