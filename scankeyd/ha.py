"""Home Assistant over MQTT (optional: on when MQTT_HOST is set), with paho-mqtt.

scankeyd announces itself with MQTT discovery, so Home Assistant makes the device and its
entities by itself, one device per printer (its serial number; see docs/HOME-ASSISTANT.md):

  per printer function   a select for the colour, one for the resolution (what the printer really
                         scans at), a switch for the text layer (the ,ocr wish), a select for how
                         a document ends (collect pages, close, single); counters per function
                         (off until enabled in Home Assistant)
  shared                 the document timeout (BATCH_TIMEOUT), the paper size, a button that drops
                         every change made here and goes back to the settings file, and one that
                         reads the scanner's capabilities again
  status                 last document, open pages, last scan, registered with the printer, last
                         error, the real resolution of the last scan, the printer (model, serial
                         number, addresses); counts of pages, documents, failed scans and
                         documents not delivered, for long-term statistics

A change from Home Assistant applies at once, to the next press, and is kept in
DATA_DIR/settings.json over restarts; the settings file (scan.env) stays the default for what was
never changed here. Only values of the expected form are taken; anything else is logged and
dropped. Without MQTT_HOST none of this runs and settings.json is not read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from . import __version__, capabilities, printerinfo
from .config import COLORS, FUNCTIONS, PAPERS, Config, Profile

if TYPE_CHECKING:
    from collections.abc import Callable

    import paho.mqtt.client as mqtt
    from paho.mqtt.properties import Properties
    from paho.mqtt.reasoncodes import ReasonCode

    from .daemon import Status

log = logging.getLogger(__name__)

LABELS = {"FILE": "File", "OCR": "OCR", "IMAGE": "Image", "EMAIL": "E-mail"}
# A count that only goes up: Home Assistant keeps long-term statistics of it (sums per hour).
COUNT = {"state_class": "total_increasing", "suggested_display_precision": 0}
COLOR_OPTIONS = {"black and white": "bw", "gray": "gray", "colour": "color"}
PAPER_OPTIONS = {"A4": "a4", "Letter": "letter"}
assert set(PAPER_OPTIONS.values()) == set(PAPERS)
FINISH_OPTIONS = {"collect pages": "", "close after each page": "close", "one page per document": "single"}
assert set(COLOR_OPTIONS.values()) == set(COLORS)


def profile_text(p: Profile) -> str:
    return ",".join(
        [p.color]
        + ([str(p.resolution)] if p.resolution else [])
        + (["ocr"] if p.ocr else [])
        + ([p.finish] if p.finish else [])
    )


class Overrides:
    """The changes made from Home Assistant, in DATA_DIR/settings.json."""

    def __init__(self, cfg: Config) -> None:
        self.path = os.path.join(cfg.data_dir, "settings.json")
        self.base_profiles = dict(cfg.profiles)  # from the settings file, for the reset button
        self.base_timeout = cfg.batch_timeout
        self.base_paper = cfg.paper
        self.data: dict[str, Any] = {}

    def load(self, cfg: Config) -> None:
        from .config import parse_profile

        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("%s not read (%s); the settings file applies", self.path, e)
            return
        if not isinstance(data, dict):
            log.warning("%s is not an object; the settings file applies", self.path)
            return
        # Only what is taken is kept: a value that is ignored must not come back on the next save as
        # the settings file's value, pinned over later changes to scan.env.
        profiles = data.get("profiles")
        kept: dict[str, str] = {}
        for func, text in profiles.items() if isinstance(profiles, dict) else ():
            if func in FUNCTIONS:
                try:
                    cfg.profiles[func] = parse_profile(str(text))
                    kept[func] = profile_text(cfg.profiles[func])
                except ValueError:
                    log.warning("settings.json: %s=%r ignored", func, text)
        timeout = data.get("batch_timeout")
        timeout_ok = isinstance(timeout, int) and not isinstance(timeout, bool) and 10 <= timeout <= 3600
        if timeout_ok:
            cfg.batch_timeout = timeout
        paper = data.get("paper")
        paper_ok = isinstance(paper, str) and paper in PAPERS
        if paper_ok:
            cfg.paper = paper
        self.data = {
            **({"profiles": kept} if kept else {}),
            **({"batch_timeout": cfg.batch_timeout} if timeout_ok else {}),
            **({"paper": cfg.paper} if paper_ok else {}),
        }
        if self.data:
            log.info("settings changed from Home Assistant apply: %s", self.data)

    def save(self, cfg: Config, func: str | None = None, timeout: bool = False, paper: bool = False) -> None:
        if func:
            self.data.setdefault("profiles", {})[func] = profile_text(cfg.profiles[func])
        if timeout:
            self.data["batch_timeout"] = cfg.batch_timeout
        if paper:
            self.data["paper"] = cfg.paper
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("change applied, but not kept over a restart (%s not written: %s)", self.path, e)

    def reset(self, cfg: Config) -> None:
        cfg.profiles.update(self.base_profiles)
        cfg.batch_timeout = self.base_timeout
        cfg.paper = self.base_paper
        self.data = {}
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def node_id(cfg: Config, printer: dict[str, str]) -> str:
    """The device's id in Home Assistant, and the level of its MQTT topics: MQTT_NODE_ID, else the one
    chosen at the first start (DATA_DIR/ha-node.json), else chosen now and kept: scankeyd_ plus the
    printer's serial number (unique, also between two printers of one model), or plus TARGET_NAME
    for a printer that gives none. Kept because a new id would make a new device in Home Assistant
    and leave the old entities and their history behind."""
    if cfg.mqtt_node_id:
        return cfg.mqtt_node_id
    path = os.path.join(cfg.data_dir, "ha-node.json")
    try:
        with open(path) as f:
            stored = str(json.load(f)["node_id"])
        if re.fullmatch(r"[a-z0-9_]{1,64}", stored):
            return stored
    except (OSError, ValueError, KeyError, TypeError):
        pass
    serial = _slug(printer.get("serial_number", ""))
    node = "scankeyd_" + (serial or _slug(cfg.target_name) or "scan")
    try:
        with open(path, "w") as f:
            json.dump({"node_id": node, "chosen_from": "serial number" if serial else "target name"}, f)
        log.info("Home Assistant device id %s (kept in %s)", node, path)
    except OSError as e:
        log.warning("device id %s not kept (%s); it may change", node, e)
    return node


class HaLink:
    def __init__(
        self,
        cfg: Config,
        status: Status,
        caps: capabilities.Capabilities | None = None,
        on_probe: Callable[[], None] | None = None,
        printer: dict[str, str] | None = None,
    ) -> None:
        """caps: what the scanner really does (the resolution lists); on_probe: the "Read scanner
        capabilities" button; printer: its eSCL description, if already read."""
        import paho.mqtt.client as mqtt

        self.cfg, self.status = cfg, status
        self.caps = caps or capabilities.Capabilities()
        self.on_probe = on_probe
        self.printer: dict[str, str] = printer if printer is not None else printerinfo.fetch(cfg.printer)
        self.node = node_id(cfg, self.printer)
        self.base = f"{cfg.mqtt_prefix}/{self.node}"
        self.overrides = Overrides(cfg)
        self.overrides.load(cfg)
        self.lock = threading.Lock()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.node)
        if cfg.mqtt_username:
            client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password or None)
        if cfg.mqtt_tls:
            client.tls_set_context(ssl.create_default_context(cafile=cfg.mqtt_ca or None))
        client.will_set(f"{self.base}/availability", "offline", retain=True)
        client.reconnect_delay_set(5, 300)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self.client = client

    # ---- the entities ----------------------------------------------------------------------------

    def _device(self) -> dict[str, Any]:
        model = self.printer.get("model", "")
        maker = self.printer.get("manufacturer") or "Brother"
        device: dict[str, Any] = {
            "identifiers": [self.node],
            # the scanner only (not the printer as a whole, which another integration may show), model
            # plus target: two printers of one model differ by their target (and can be renamed)
            "name": f"{model or 'Brother'} Scanner ({self.cfg.target_name})",
            "manufacturer": maker,
            "model": model.removeprefix(maker).strip() if model else "scan target",
            "sw_version": f"scankeyd {__version__}",
            "configuration_url": f"https://{self.cfg.printer}/",  # the printer's own web page
        }
        if self.printer.get("serial_number"):
            device["serial_number"] = self.printer["serial_number"]
        return device

    def printer_attributes(self) -> dict[str, str]:
        return {
            "address": self.cfg.printer,
            "scankeyd_address": self.cfg.advertise_ip or "",
            **{k: v for k, v in self.printer.items() if k in ("serial_number", "mac", "uuid", "escl_version")},
        }

    def refresh_printer(self) -> None:
        """Reads the printer's description again (after it was unreachable): another printer on the
        address shows in Home Assistant by itself. The device id stays."""
        info = printerinfo.fetch(self.cfg.printer)
        if info and info != self.printer:
            self.printer = info
            if self.client.is_connected():
                self._announce()
                self.publish(self.printer_state())

    def printer_state(self) -> dict[str, str]:
        return {
            "printer": self.printer.get("model") or "unknown",
            "printer/attributes": json.dumps(self.printer_attributes()),
        }

    def entities(self) -> list[tuple[str, str, dict[str, Any]]]:
        """(component, key, discovery config without the common fields)."""
        out: list[tuple[str, str, dict[str, Any]]] = []
        for func in FUNCTIONS:
            f, label = func.lower(), LABELS[func]
            out += [
                (
                    "select",
                    f"{f}_color",
                    {"name": f"{label} colour", "options": list(COLOR_OPTIONS), "icon": "mdi:palette"},
                ),
                ("switch", f"{f}_ocr", {"name": f"{label} text layer", "icon": "mdi:text-recognition"}),
                (
                    "select",
                    f"{f}_resolution",
                    {
                        "name": f"{label} resolution",
                        # what the printer really scans at in this function's colour mode (x × y)
                        "options": [capabilities.label(r) for _, r in self.caps.options(self.cfg.profiles[func].color)],
                        "icon": "mdi:image-size-select-large",
                    },
                ),
                (
                    "select",
                    f"{f}_document",
                    {"name": f"{label} document", "options": list(FINISH_OPTIONS), "icon": "mdi:file-multiple"},
                ),
            ]
        out += [
            (
                "number",
                "batch_timeout",
                {
                    "name": "Document timeout",
                    "min": 10,
                    "max": 3600,
                    "step": 1,
                    "mode": "box",
                    "unit_of_measurement": "s",
                    "icon": "mdi:timer-outline",
                },
            ),
            ("select", "paper", {"name": "Paper size", "options": list(PAPER_OPTIONS), "icon": "mdi:file-outline"}),
            ("button", "reset", {"name": "Back to the settings file", "icon": "mdi:restore"}),
            (
                "button",
                "probe",
                {"name": "Read scanner capabilities", "icon": "mdi:magnify-scan", "entity_category": "diagnostic"},
            ),
            (
                "sensor",
                "last_resolution",
                {"name": "Real resolution", "icon": "mdi:image-size-select-large", "entity_category": "diagnostic"},
            ),
            ("sensor", "last_document", {"name": "Last document", "icon": "mdi:file-document"}),
            ("sensor", "open_pages", {"name": "Open pages", "state_class": "measurement", "icon": "mdi:file-clock"}),
            ("sensor", "last_scan", {"name": "Last scan", "device_class": "timestamp"}),
            (
                "binary_sensor",
                "registered",
                {
                    "name": "Registered with the printer",
                    "device_class": "connectivity",
                    "entity_category": "diagnostic",
                },
            ),
            ("sensor", "last_error", {"name": "Last error", "entity_category": "diagnostic", "icon": "mdi:alert"}),
            (
                "sensor",
                "printer",
                {
                    "name": "Printer",
                    "entity_category": "diagnostic",
                    "icon": "mdi:printer",
                    "json_attributes_topic": f"{self.base}/printer/attributes",
                },
            ),
            # Counts for long-term statistics (counters.py): totals, and per function (off by default)
            ("sensor", "pages", {"name": "Pages scanned", **COUNT, "icon": "mdi:file-document-multiple"}),
            ("sensor", "documents", {"name": "Documents scanned", **COUNT, "icon": "mdi:folder-multiple"}),
            ("sensor", "scan_failures", {"name": "Failed scans", **COUNT, "icon": "mdi:alert-circle"}),
            ("sensor", "documents_failed", {"name": "Documents not delivered", **COUNT, "icon": "mdi:folder-alert"}),
        ]
        for func in FUNCTIONS:
            f, label = func.lower(), LABELS[func]
            off = {**COUNT, "enabled_by_default": False}
            out += [
                (
                    "sensor",
                    f"{f}_pages",
                    {"name": f"{label} pages scanned", **off, "icon": "mdi:file-document-multiple"},
                ),
                (
                    "sensor",
                    f"{f}_documents",
                    {"name": f"{label} documents scanned", **off, "icon": "mdi:folder-multiple"},
                ),
            ]
        for component, _key, conf in out:
            if component in ("select", "switch", "number"):
                conf.setdefault("entity_category", "config")
        return out

    def _announce(self) -> None:
        for component, key, conf in self.entities():
            payload = {
                **conf,
                "unique_id": f"{self.node}_{key}",
                # the entity id from the device id, not from the device's name (the printer's model):
                # predictable, and the same pattern for every scankeyd (HA 2025.10 and later)
                "default_entity_id": f"{component}.{self.node}_{key}",
                "availability_topic": f"{self.base}/availability",
                "device": self._device(),
            }
            if component != "button":
                payload["state_topic"] = f"{self.base}/{key}"
            if component in ("select", "switch", "number", "button"):
                payload["command_topic"] = f"{self.base}/{key}/set"
            self.client.publish(
                f"{self.cfg.discovery_prefix}/{component}/{self.node}/{key}/config", json.dumps(payload), retain=True
            )

    # ---- state out -------------------------------------------------------------------------------

    def settings_state(self) -> dict[str, str]:
        out = {}
        for func in FUNCTIONS:
            p, f = self.cfg.profiles[func], func.lower()
            out[f"{f}_color"] = next(k for k, v in COLOR_OPTIONS.items() if v == p.color)
            out[f"{f}_ocr"] = "ON" if p.ocr else "OFF"
            out[f"{f}_document"] = next(k for k, v in FINISH_OPTIONS.items() if v == p.finish)
            out[f"{f}_resolution"] = capabilities.label(self.caps.real(p.color, self.cfg.dpi(p)))
        out["batch_timeout"] = str(self.cfg.batch_timeout)
        out["paper"] = next(k for k, v in PAPER_OPTIONS.items() if v == self.cfg.paper)
        return out

    def status_state(self, data: dict[str, object]) -> dict[str, str]:
        reg = data.get("registered_at")
        fresh = isinstance(reg, (int, float)) and time.time() - reg < 3 * self.cfg.register_interval + 30
        last = data.get("last_scan_at")
        c = data.get("counters") if isinstance(data.get("counters"), dict) else {}
        assert isinstance(c, dict)
        pages = c.get("pages") or {}
        docs = c.get("documents") or {}
        # "None" is MQTT discovery's word for "no value": the entity shows as unknown, not as ""
        out = {
            "last_document": str(data.get("last_document") or "None"),
            "open_pages": str(data.get("open_pages") or 0),
            "last_scan": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(last))
            if isinstance(last, (int, float))
            else "None",
            "registered": "ON" if fresh and not data.get("last_error") else "OFF",
            "last_error": str(data.get("last_error") or "none")[:250],
            "last_resolution": str(data.get("last_resolution") or "None"),
            "pages": str(sum(int(v) for v in pages.values())),
            "documents": str(sum(int(v) for v in docs.values())),
            "scan_failures": str(int(c.get("scan_failures") or 0)),
            "documents_failed": str(int(c.get("documents_failed") or 0)),
        }
        for func in FUNCTIONS:
            out[f"{func.lower()}_pages"] = str(int(pages.get(func, 0)))
            out[f"{func.lower()}_documents"] = str(int(docs.get(func, 0)))
        return out

    def publish(self, state: dict[str, str]) -> None:
        for key, value in state.items():
            self.client.publish(f"{self.base}/{key}", value, retain=True)

    def on_status(self, data: dict[str, object]) -> None:
        """The daemon's status changed (Status listener)."""
        if self.client.is_connected():
            self.publish(self.status_state(data))

    # ---- commands in -----------------------------------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.ConnectFlags,
        reason: ReasonCode,
        _props: Properties | None = None,
    ) -> None:
        if reason.is_failure:
            log.warning("MQTT: the broker refused the connection (%s)", reason)
            return
        log.info("MQTT connected to %s:%d", self.cfg.mqtt_host, self.cfg.mqtt_port)
        client.subscribe(f"{self.base}/+/set")
        self._announce()
        client.publish(f"{self.base}/availability", "online", retain=True)
        self.publish(self.settings_state())
        self.publish(self.status_state(self.status.snapshot()))
        self.publish(self.printer_state())

    def _on_message(self, _client: mqtt.Client, _userdata: object, msg: mqtt.MQTTMessage) -> None:
        key = msg.topic[len(self.base) + 1 : -len("/set")] if msg.topic.startswith(self.base + "/") else ""
        value = msg.payload.decode("utf-8", "replace").strip()[:64]
        with self.lock:
            try:
                self.apply(key, value)
            except Exception as e:  # noqa: BLE001  (paho ends its network thread on an exception here)
                log.warning("from Home Assistant: %s=%r not taken (%s)", key, value, e)
                return
        self.publish(self.settings_state())

    def apply(self, key: str, value: str) -> None:
        """One command: changes cfg and saves it; ValueError for anything not of the expected form."""
        if key == "probe":
            if self.on_probe:
                self.on_probe()
            log.info("Home Assistant: read the scanner's capabilities")
            return
        if key == "reset":
            self.overrides.reset(self.cfg)
            log.info("Home Assistant: back to the settings file")
            if self.client.is_connected():
                self._announce()  # colour modes may be back to others: their resolution lists too
            return
        if key == "paper":
            if value not in PAPER_OPTIONS:
                raise ValueError("unknown paper size")
            self.cfg.paper = PAPER_OPTIONS[value]
            self.overrides.save(self.cfg, paper=True)
            log.info("Home Assistant: paper %s", self.cfg.paper)
            return
        if key == "batch_timeout":
            seconds = int(float(value))
            if not 10 <= seconds <= 3600:
                raise ValueError("outside 10-3600")
            self.cfg.batch_timeout = seconds
            self.overrides.save(self.cfg, timeout=True)
            log.info("Home Assistant: document timeout %d s", seconds)
            return
        func, _, what = key.partition("_")
        func = func.upper()
        if func not in FUNCTIONS:
            raise ValueError("unknown setting")
        p = self.cfg.profiles[func]
        if what == "color" and value in COLOR_OPTIONS:
            p = replace(p, color=COLOR_OPTIONS[value])
            # another colour mode can have other real resolutions: keep the nearest one it has
            if p.resolution:
                p = replace(p, resolution=self.caps.nearest(p.color, p.resolution))
        elif what == "ocr" and value in ("ON", "OFF"):
            p = replace(p, ocr=value == "ON")
        elif what == "document" and value in FINISH_OPTIONS:
            p = replace(p, finish=FINISH_OPTIONS[value])
        elif what == "resolution" and value in (
            opts := {capabilities.label(r): s for s, r in self.caps.options(p.color)}
        ):
            p = replace(p, resolution=opts[value])
        else:
            raise ValueError("unknown setting or value")
        colour_changed = p.color != self.cfg.profiles[func].color
        self.cfg.profiles[func] = p
        self.overrides.save(self.cfg, func=func)
        log.info("Home Assistant: %s -> %s", func, profile_text(p))
        if colour_changed and self.client.is_connected():
            self._announce()  # the resolution list follows the colour mode

    def capabilities_changed(self) -> None:
        """After a probe: the resolution lists as the printer really is."""
        if self.client.is_connected():
            self._announce()
            self.publish(self.settings_state())

    # ---- life ------------------------------------------------------------------------------------

    def start(self) -> None:
        self.client.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.publish(f"{self.base}/availability", "offline", retain=True)
        self.client.disconnect()
        self.client.loop_stop()
