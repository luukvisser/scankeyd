"""Configuration from environment variables, validated as a whole at start.

A daemon that half-works is worse than one that refuses to start: Brother's
own tool registered an empty address for days without a word. So every
value is checked here, and errors are collected and reported together.
"""

from __future__ import annotations

import ipaddress
import os
import re
import string
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

FUNCTIONS = ("FILE", "IMAGE", "OCR", "EMAIL")
COLORS = ("bw", "gray", "color")
# FUNC_<function> = "<colour>[,<dpi>][,ocr][,close|single]": what a press of that printer function does.
#   colour  bw, gray or color
#   dpi     100, 200, 300, 600, 1200 or 2400: this function's resolution (else RESOLUTION); the
#           printer scans at most at its real maximum (Home Assistant shows what that is)
#   ocr     a wish for the steps after the folder: document.json says "ocr": true, and the
#           tidy step adds a searchable text layer (scankeyd itself reads no text)
#   close   this page finishes the document; single: this page is a document of its own
# Without close/single a document finishes BATCH_TIMEOUT seconds after its last page, or when a
# press of another function starts a new document.
DEFAULT_PROFILES = {"FILE": "bw", "OCR": "bw,ocr", "IMAGE": "color", "EMAIL": "bw"}
# What may be asked for; the printer scans at its own maximum when that is lower (capabilities.py).
RESOLUTIONS = (100, 200, 300, 600, 1200, 2400)
PAPERS = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}
FILENAME_FIELDS = ("date", "time", "target", "seq", "function")
# Printable ASCII without the characters that separate the registration string.
TARGET_RE = re.compile(r"^[A-Za-z0-9 ._+\-()]{1,16}$")


@dataclass(frozen=True)
class Profile:
    color: str  # pages of the same colour go into the same document
    finish: str = ""  # "", "close" or "single"
    ocr: bool = False  # passed on in document.json, for the tidy step
    resolution: int = 0  # dpi; 0: the shared RESOLUTION


def parse_profile(text: str) -> Profile:
    tokens = [t.strip().lower() for t in text.split(",") if t.strip()]
    colors = [t for t in tokens if t in COLORS]
    finish = [t for t in tokens if t in ("close", "single")]
    dpis = [t for t in tokens if t in {str(r) for r in RESOLUTIONS}]
    unknown = [t for t in tokens if t not in COLORS and t not in ("close", "single", "ocr") and t not in dpis]
    if unknown or len(colors) != 1 or len(finish) > 1 or len(dpis) > 1 or tokens.count("ocr") > 1:
        raise ValueError(text)
    return Profile(colors[0], finish[0] if finish else "", "ocr" in tokens, int(dpis[0]) if dpis else 0)


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Config:
    printer: str
    target_name: str = "Scan"
    advertise_ip: str | None = None
    resolution: int = 300
    paper: str = "a4"
    batch_timeout: int = 120
    profiles: dict[str, Profile] = field(
        default_factory=lambda: {f: parse_profile(p) for f, p in DEFAULT_PROFILES.items()}
    )
    umask: int = 0o027
    filename: str = "scan_{date}_{time}"
    output_dir: str = "/scans"
    data_dir: str = "/data"
    tls_pin: str = "tofu"
    register_method: str = "auto"
    register_interval: int = 60
    listen_port: int = 54925
    log_level: str = "info"
    # Home Assistant over MQTT (ha.py); off while mqtt_host is empty
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_ca: str = ""
    mqtt_prefix: str = "scankeyd"
    mqtt_node_id: str = ""  # the device's id in Home Assistant; "" chooses one at the first start (ha.py)
    discovery_prefix: str = "homeassistant"

    def dpi(self, profile: Profile) -> int:
        """The resolution a press with this profile scans at."""
        return profile.resolution or self.resolution


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int, problems: list[str]) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name}={raw!r} is not a whole number")
        return default
    if not lo <= value <= hi:
        problems.append(f"{name}={value} is outside {lo}-{hi}")
    return value


def _ip(raw: str, name: str, problems: list[str]) -> str | None:
    try:
        return str(ipaddress.IPv4Address(raw))
    except ValueError:
        problems.append(f"{name}={raw!r} is not an IPv4 address")
        return None


def load(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    problems: list[str] = []

    printer = env.get("PRINTER", "").strip()
    if not printer:
        problems.append("PRINTER is required: the printer's IPv4 address")
    else:
        printer = _ip(printer, "PRINTER", problems) or printer

    cfg = Config(printer=printer)

    name = env.get("TARGET_NAME", cfg.target_name)
    if not TARGET_RE.match(name):
        problems.append(f"TARGET_NAME={name!r}: 1-16 characters, letters, digits, space and ._+-() only")
    cfg.target_name = name

    adv = env.get("ADVERTISE_IP", "").strip()
    cfg.advertise_ip = _ip(adv, "ADVERTISE_IP", problems) if adv else None

    cfg.resolution = _int(env, "RESOLUTION", cfg.resolution, 100, 2400, problems)
    if cfg.resolution not in RESOLUTIONS:
        problems.append(f"RESOLUTION={cfg.resolution}: one of {', '.join(map(str, RESOLUTIONS))}")

    if "COLOR" in env:
        problems.append("COLOR is replaced by FUNC_FILE, FUNC_IMAGE, FUNC_OCR and FUNC_EMAIL (e.g. FUNC_FILE=bw)")

    cfg.paper = env.get("PAPER", cfg.paper).lower()
    if cfg.paper not in PAPERS:
        problems.append(f"PAPER={cfg.paper!r}: one of {', '.join(PAPERS)}")

    cfg.batch_timeout = _int(env, "BATCH_TIMEOUT", cfg.batch_timeout, 10, 3600, problems)
    cfg.register_interval = _int(env, "REGISTER_INTERVAL", cfg.register_interval, 15, 300, problems)
    cfg.listen_port = _int(env, "LISTEN_PORT", cfg.listen_port, 1, 65535, problems)

    for func in FUNCTIONS:
        raw = env.get(f"FUNC_{func}", DEFAULT_PROFILES[func])
        try:
            cfg.profiles[func] = parse_profile(raw)
        except ValueError:
            problems.append(
                f"FUNC_{func}={raw!r}: one of bw, gray, color, optionally with a resolution (,600), "
                ",ocr and ,close or ,single"
            )

    cfg.filename = env.get("FILENAME", cfg.filename)
    try:  # Python's own parser: {{ and }} are literal braces, a lone { or } is an error
        fields = {f for _, f, _, _ in string.Formatter().parse(cfg.filename) if f is not None}
    except ValueError as e:
        problems.append(f"FILENAME: {e} (a literal brace is written {{{{ or }}}})")
    else:
        unknown = fields - set(FILENAME_FIELDS)
        if unknown:
            problems.append(f"FILENAME: unknown fields {sorted(unknown)}; allowed: {', '.join(FILENAME_FIELDS)}")
        else:
            try:  # a format spec or conversion ({date:d}, {seq!z}) fails here, not at every document
                sample = cfg.filename.format(
                    date="2026-01-01", time="000000", target=cfg.target_name, seq=1, function="email"
                )
            except (ValueError, IndexError, KeyError, TypeError) as e:
                problems.append(f"FILENAME: {e}")
            else:
                if len(sample) > 120:
                    problems.append(f"FILENAME: names of {len(sample)} characters; at most 120")
    if "/" in cfg.filename or "\\" in cfg.filename or cfg.filename.startswith("."):
        problems.append("FILENAME must be a plain name, without directories")

    cfg.output_dir = env.get("OUTPUT_DIR", cfg.output_dir)
    cfg.data_dir = env.get("DATA_DIR", cfg.data_dir)

    pin = env.get("TLS_PIN", cfg.tls_pin).strip().lower().replace(":", "")
    if pin not in ("tofu", "off") and not re.fullmatch(r"[0-9a-f]{64}", pin):
        problems.append("TLS_PIN: 'tofu', 'off' or the certificate's SHA-256 fingerprint (64 hex digits)")
    cfg.tls_pin = pin

    cfg.register_method = env.get("REGISTER_METHOD", cfg.register_method).lower()
    if cfg.register_method not in ("auto", "phoenix", "snmp"):
        problems.append("REGISTER_METHOD: one of auto, phoenix, snmp")

    raw = env.get("UMASK", "")
    if raw:
        if re.fullmatch(r"0?[0-7]{3}", raw):
            cfg.umask = int(raw, 8)
        else:
            problems.append(f"UMASK={raw!r}: three octal digits, e.g. 027 (owner and group may read the scans)")

    cfg.mqtt_host = env.get("MQTT_HOST", "").strip()
    if cfg.mqtt_host:
        if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}", cfg.mqtt_host):
            problems.append(f"MQTT_HOST={cfg.mqtt_host!r}: a host name or IP address")
        cfg.mqtt_node_id = env.get("MQTT_NODE_ID", "").strip()
        if cfg.mqtt_node_id and not re.fullmatch(r"[a-z0-9_]{1,64}", cfg.mqtt_node_id):
            problems.append(f"MQTT_NODE_ID={cfg.mqtt_node_id!r}: lower-case letters, digits and _ only")
        cfg.mqtt_tls = env.get("MQTT_TLS", "off").strip().lower() in ("on", "true", "1", "yes")
        cfg.mqtt_port = _int(env, "MQTT_PORT", 8883 if cfg.mqtt_tls else 1883, 1, 65535, problems)
        cfg.mqtt_username = env.get("MQTT_USERNAME", "")
        cfg.mqtt_password = env.get("MQTT_PASSWORD", "")
        pw_file = env.get("MQTT_PASSWORD_FILE", "")
        if pw_file:
            try:
                with open(pw_file) as f:
                    cfg.mqtt_password = f.read().strip()
            except OSError as e:
                problems.append(f"MQTT_PASSWORD_FILE: {e.strerror}")
        cfg.mqtt_ca = env.get("MQTT_CA", "")
        if cfg.mqtt_ca and not os.path.isfile(cfg.mqtt_ca):
            problems.append(f"MQTT_CA={cfg.mqtt_ca!r}: no such file")
        for name, attr in (("MQTT_TOPIC_PREFIX", "mqtt_prefix"), ("MQTT_DISCOVERY_PREFIX", "discovery_prefix")):
            value = env.get(name, getattr(cfg, attr)).strip().strip("/")
            if not re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*", value):
                problems.append(f"{name}={value!r}: topic levels of letters, digits, _ and - only")
            setattr(cfg, attr, value)
        try:
            import paho.mqtt.client  # noqa: F401
        except ImportError:
            problems.append("MQTT_HOST is set, but paho-mqtt is not installed (pip install 'scankeyd[ha]')")

    cfg.log_level = env.get("LOG_LEVEL", cfg.log_level).lower()
    if cfg.log_level not in ("debug", "info", "warning", "error"):
        problems.append("LOG_LEVEL: one of debug, info, warning, error")

    if problems:
        raise ConfigError(problems)
    return cfg
