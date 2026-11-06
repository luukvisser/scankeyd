from __future__ import annotations

import importlib.util
import json
import os
import random
import tempfile
import unittest
from typing import TYPE_CHECKING, Any, Never, override
from unittest import mock

from scankeyd import batch, config, datagram, registration, scanproto
from scankeyd.batch import Batch
from scankeyd.config import Profile

from . import fakeprinter

if TYPE_CHECKING:
    from scankeyd.ha import HaLink

REAL = (
    b'\x02\x00\x77\x30TYPE=BR;BUTTON=SCAN;USER="Scan";FUNC=FILE;HOST=192.168.1.10:54925;APPNUM=5;'
    b"P1=0;P2=0;P3=0;P4=0;REGID=17638;SEQ=1;"
)


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def written(c: config.Config, b: Batch) -> dict[str, Any]:
    """The document the batch delivered last: its document.json, with its folder's file names."""
    assert b.last_written, "nothing written"
    folder = os.path.join(c.output_dir, b.last_written)
    doc = json.loads(read(os.path.join(folder, "document.json")))
    doc["files"] = sorted(os.listdir(folder))
    return doc


def press(data: bytes) -> datagram.ButtonPress:
    """A datagram that must parse."""
    p = datagram.parse(data)
    assert p is not None
    return p


def cfg(tmp: str, **env: str) -> config.Config:
    base = {"PRINTER": "127.0.0.1", "OUTPUT_DIR": os.path.join(tmp, "scans"), "DATA_DIR": tmp}
    base.update(env)
    return config.load(base)


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        c = config.load({"PRINTER": "192.168.1.50"})
        self.assertEqual((c.target_name, c.resolution), ("Scan", 300))
        self.assertEqual(
            c.profiles,
            {"FILE": Profile("bw"), "OCR": Profile("bw", ocr=True), "IMAGE": Profile("color"), "EMAIL": Profile("bw")},
        )

    def test_profiles(self) -> None:
        self.assertEqual(config.parse_profile(" Gray , single"), Profile("gray", "single"))
        self.assertEqual(config.parse_profile("bw,ocr,close"), Profile("bw", "close", ocr=True))
        self.assertEqual(config.parse_profile("color,600,single"), Profile("color", "single", resolution=600))
        for bad in ("", "ocr", "bw,ocr,ocr", "bw,color", "bw,close,single", "bw,fast", "bw,450", "bw,300,600"):
            with self.assertRaises(ValueError, msg=bad):
                config.parse_profile(bad)

    def test_settings_of_the_other_steps_are_left_to_them(self) -> None:
        # one settings file for every container: what the tidy step reads is no error here
        c = config.load({"PRINTER": "10.0.0.1", "OCR_LANGUAGES": "nld+eng", "DESKEW": "on", "FUNC_OCR": "bw,ocr"})
        self.assertTrue(c.profiles["OCR"].ocr)

    def test_all_problems_reported_at_once(self) -> None:
        with self.assertRaises(config.ConfigError) as e:
            config.load({"TARGET_NAME": "a;b", "RESOLUTION": "250", "FUNC_IMAGE": "pink", "FILENAME": "../{x}"})
        text = " ".join(e.exception.problems)
        for word in ("PRINTER", "TARGET_NAME", "RESOLUTION", "FUNC_IMAGE", "FILENAME"):
            self.assertIn(word, text)

    def test_target_name_limits(self) -> None:
        config.load({"PRINTER": "10.0.0.1", "TARGET_NAME": "Google Drive"})
        with self.assertRaises(config.ConfigError):
            config.load({"PRINTER": "10.0.0.1", "TARGET_NAME": "x" * 17})


class DatagramTest(unittest.TestCase):
    def test_real_datagram(self) -> None:
        p = press(REAL)
        self.assertEqual((p.user, p.func, p.regid, p.seq), ("Scan", "FILE", "17638", "1"))

    def test_rejects(self) -> None:
        for bad in (
            b"",
            REAL[:3],
            b"\x03" + REAL[1:],
            REAL[:4] + b"\xff\xfe",
            REAL + b"\n",
            REAL[:4] + b"TYPE=BR;BUTTON=SCAN;FUNC=FILE;",
            REAL[:4] + b"noequals;",
            REAL + b"X" * 2000,
        ):
            self.assertIsNone(datagram.parse(bad), bad[:40])

    def test_fuzz_never_raises(self) -> None:
        rnd = random.Random(1)
        for _ in range(20000):
            n = rnd.randrange(0, 300)
            data = bytes(rnd.randrange(256) for _ in range(n))
            if rnd.random() < 0.5:
                data = REAL[:4] + data
            datagram.parse(data)
            mutated = bytearray(REAL)
            for _ in range(rnd.randrange(1, 6)):
                mutated[rnd.randrange(len(mutated))] = rnd.randrange(256)
            datagram.parse(bytes(mutated))

    def test_repeats_dropped(self) -> None:
        t = [0.0]
        d = datagram.Deduplicator(window=60, clock=lambda: t[0])
        p = press(REAL)
        self.assertTrue(d.is_new("p", p))
        t[0] = 10
        self.assertFalse(d.is_new("p", p))
        self.assertTrue(d.is_new("p", press(REAL.replace(b"SEQ=1", b"SEQ=2"))))
        t[0] = 100
        self.assertTrue(d.is_new("p", p))


class ScanProtoTest(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.srv = fakeprinter.start_scanner()
        cls.port = cls.srv.server_address[1]

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_page(self) -> None:
        fakeprinter.ScanHandler.jpeg = fakeprinter.fake_jpeg(size=300_000)
        fakeprinter.ScanHandler.busy = False
        got = scanproto.scan_page("127.0.0.1", 300, "color", (215.9, 297.0), port=self.port)
        self.assertEqual(got, fakeprinter.ScanHandler.jpeg)
        ssp, xsc = fakeprinter.ScanHandler.seen[-1]
        self.assertIn(b"CLR=C24BIT\n", ssp)
        self.assertIn(b"AREA=0,0,2479,3437\n", xsc)  # letter width clamped to the printer's maximum

    def test_page_complete_although_the_closing_bytes_are_held(self) -> None:
        # from the Scan key the printer sent 2 of the 4 closing bytes and held the rest (06-11-2026)
        import time as _time

        fakeprinter.ScanHandler.hold_tail = True
        try:
            started = _time.monotonic()
            got = scanproto.scan_page("127.0.0.1", 300, "gray", port=self.port)
            self.assertLess(_time.monotonic() - started, 4)
        finally:
            fakeprinter.ScanHandler.hold_tail = False
        self.assertEqual(got, fakeprinter.ScanHandler.jpeg)

    def test_black_and_white(self) -> None:
        rows = fakeprinter.fake_rows()
        fakeprinter.ScanHandler.rows = (306, rows)
        try:
            page = scanproto.scan_page("127.0.0.1", 300, "bw", port=self.port)
        finally:
            fakeprinter.ScanHandler.rows = None
        assert isinstance(page, scanproto.Bilevel)
        self.assertEqual((page.width_bytes, page.height, page.rows), (306, 40, rows))
        ssp, _ = fakeprinter.ScanHandler.seen[-1]
        self.assertIn(b"CLR=TEXT\n", ssp)
        self.assertIn(b"COMP=RLENGTH\n", ssp)
        batch.check_page(page.to_pbm(), "pbm")  # a binary PBM of its stated size

    def test_unpackbits(self) -> None:
        data = bytes(range(50)) + b"\x00" * 300 + b"\xff" * 3
        self.assertEqual(scanproto.unpackbits(fakeprinter.packbits(data), 4096), data)
        for bad in (b"\x05ab", b"\xfe"):
            with self.assertRaises(scanproto.ScanError):
                scanproto.unpackbits(bad, 4096)
        with self.assertRaises(scanproto.ScanError):
            scanproto.unpackbits(b"\x81\x00" * 40, 1000)

    def test_busy(self) -> None:
        fakeprinter.ScanHandler.busy = True
        try:
            with self.assertRaises(scanproto.PrinterBusy):
                scanproto.scan_page("127.0.0.1", port=self.port)
        finally:
            fakeprinter.ScanHandler.busy = False


class RegistrationTest(unittest.TestCase):
    def test_phoenix_digest_and_entries(self) -> None:
        srv = fakeprinter.start_phoenix()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                c = cfg(tmp, TARGET_NAME="Google Drive", ADVERTISE_IP="192.168.1.10")
                r = registration.Registration(c)
                reg = registration.PhoenixRegistrar(c, scheme="http", port=srv.server_address[1])
                reg.register(r.entries())
                got = fakeprinter.PhoenixHandler.entries[-1]
                self.assertEqual(len(got), 4)
                self.assertIn("USER=Google Drive;FUNC=FILE;HOST=192.168.1.10:54925;APPNUM=5;DURATION=360;", got[3])
                fakeprinter.PhoenixHandler.status = 1
                with self.assertRaises(registration.RegistrationError):
                    reg.register(r.entries())
        finally:
            fakeprinter.PhoenixHandler.status = 0
            srv.shutdown()
            srv.server_close()

    def test_no_descriptor_leak(self) -> None:
        srv = fakeprinter.start_phoenix()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                c = cfg(tmp, ADVERTISE_IP="192.168.1.10")
                reg = registration.PhoenixRegistrar(c, scheme="http", port=srv.server_address[1])
                entries = registration.Registration(c).entries()
                reg.register(entries)
                before = len(os.listdir("/proc/self/fd"))
                for _ in range(500):
                    reg.register(entries)
                self.assertLessEqual(len(os.listdir("/proc/self/fd")), before + 2)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_snmp(self) -> None:
        s = fakeprinter.SnmpServer()
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, ADVERTISE_IP="192.168.1.10")
            registration.SnmpRegistrar(c, port=s.port).register(registration.Registration(c).entries())
        self.assertEqual(len(s.values), 4)
        self.assertTrue(s.values[0].startswith(b"TYPE=BR;BUTTON=SCAN;USER=Scan;FUNC=IMAGE;"))

    def test_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = registration.Pinner(cfg(tmp))
            p.check(b"cert-one")
            p.check(b"cert-one")
            with self.assertRaises(registration.PinMismatch):
                p.check(b"cert-two")
            with self.assertRaises(registration.PinMismatch):  # no certificate at all
                p.check(None)


class BatchTest(unittest.TestCase):
    def test_page_close_timeout_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = [1_000_000.0]
            c = cfg(tmp, BATCH_TIMEOUT="120")
            b = Batch(c, clock=lambda: t[0], monotonic=lambda: t[0])
            jpeg = fakeprinter.fake_jpeg()
            color, bw = Profile("color"), Profile("bw")
            b.add(jpeg, color)
            b.add(jpeg, color)
            t[0] += 119
            b.tick()
            self.assertEqual(b.open_pages(), 2)
            t[0] += 2
            b.tick()
            self.assertEqual(b.open_pages(), 0)
            first = b.last_written
            b.add(jpeg, Profile("color", finish="close"))
            self.assertNotEqual(b.last_written, first)
            b.add(scanproto.Bilevel(306, fakeprinter.fake_rows()), bw)
            # a restart: a new Batch finds the open page and writes it
            b2 = Batch(c, clock=lambda: t[0], monotonic=lambda: t[0])
            b2.recover()
            self.assertEqual(b2.open_pages(), 0)
            docs = sorted(os.listdir(os.path.join(c.output_dir, "file")))
            self.assertEqual(len(docs), 3, docs)
            self.assertFalse([d for d in docs if d.startswith(".")])  # no .part left
            self.assertEqual(written(c, b2)["files"], ["document.json", "page-0001.pbm"])

    def test_other_kind_starts_a_new_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            b = Batch(c)
            bw = scanproto.Bilevel(306, fakeprinter.fake_rows())
            b.add(bw, Profile("bw"))
            b.add(bw, Profile("bw"))  # FILE and EMAIL: same kind, same document
            self.assertEqual(b.open_pages(), 2)
            b.add(fakeprinter.fake_jpeg(comps=3), Profile("color"))
            self.assertEqual(b.open_pages(), 1)
            doc = written(c, b)
            self.assertEqual(doc["pages"], ["page-0001.pbm", "page-0002.pbm"])
            self.assertIs(doc["ocr"], False)
            self.assertEqual((doc["color"], doc["function"], doc["resolution"], doc["format"]), ("bw", "FILE", 300, 1))

    def test_another_function_starts_a_new_document_in_its_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            b = Batch(c)
            bw = scanproto.Bilevel(306, fakeprinter.fake_rows())
            b.add(bw, Profile("bw"), "FILE")
            b.add(bw, Profile("bw", ocr=True), "OCR")  # same colour, another function: its own document
            self.assertEqual(written(c, b)["function"], "FILE")
            self.assertTrue((b.last_written or "").startswith("file/"))
            b.add(fakeprinter.fake_jpeg(comps=3), Profile("color", finish="close"), "IMAGE")
            self.assertTrue((b.last_written or "").startswith("image/"))
            self.assertIs(
                json.loads(
                    read(
                        os.path.join(
                            c.output_dir, "ocr", os.listdir(os.path.join(c.output_dir, "ocr"))[0], "document.json"
                        )
                    )
                )["ocr"],
                True,
            )
            self.assertEqual(sorted(os.listdir(c.output_dir)), ["file", "image", "ocr"])
            page = os.path.join(c.output_dir, b.last_written or "", "page-0001.jpg")
            self.assertEqual(read(page), fakeprinter.fake_jpeg(comps=3))  # the printer's JPEG, byte for byte

    def test_pages_left_by_scankeyd_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            b = Batch(c)
            with open(os.path.join(b.dir, "page-0001-20261105-101500-r000.jpg"), "wb") as f:
                f.write(fakeprinter.fake_jpeg())
            with open(b.profile_path, "w") as f:
                f.write('{"color": "color", "ocr": false}')
            b.recover()
            self.assertEqual(b.last_written, "unknown/scan_2026-11-05_101500")  # brscan-free kept no function
            doc = written(c, b)
            self.assertEqual(
                (doc["pages"], doc["color"], doc["scanned"]), (["page-0001.jpg"], "color", "2026-11-05T10:15:00")
            )


@unittest.skipUnless(importlib.util.find_spec("paho"), "needs paho-mqtt (the ha extra)")
class HaLinkTest(unittest.TestCase):
    def link(self, tmp: str, **env: str) -> tuple[HaLink, list[tuple[str, str, bool]]]:
        """A link whose publishes land in the returned list (topic, payload, retain), not at a broker."""
        from scankeyd.daemon import Status
        from scankeyd.ha import HaLink

        c = cfg(tmp, MQTT_HOST="127.0.0.1", TARGET_NAME="Google Drive", **env)
        link = HaLink(c, Status(c))
        sent: list[tuple[str, str, bool]] = []
        link.client = mock.Mock()
        link.client.publish.side_effect = lambda topic, payload, retain=False: sent.append((topic, payload, retain))
        link.client.is_connected.return_value = True
        return link, sent

    def test_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link, sent = self.link(tmp)
            link._announce()
            configs = {t: json.loads(p) for t, p, r in sent if t.endswith("/config") and r}
            self.assertEqual(len(configs), 4 * 4 + 8 + 4 + 4 * 2 + 1 + 2)
            pages = configs["homeassistant/sensor/scankeyd_google_drive/pages/config"]
            self.assertEqual(pages["state_class"], "total_increasing")
            self.assertIs(
                configs["homeassistant/sensor/scankeyd_google_drive/ocr_pages/config"]["enabled_by_default"], False
            )
            self.assertNotIn("enabled_by_default", pages)
            ocr = configs["homeassistant/switch/scankeyd_google_drive/ocr_ocr/config"]
            self.assertEqual(ocr["command_topic"], "scankeyd/scankeyd_google_drive/ocr_ocr/set")
            self.assertEqual(ocr["device"]["name"], "Brother Scanner (Google Drive)")  # no eSCL in the test
            self.assertEqual(ocr["unique_id"], "scankeyd_google_drive_ocr_ocr")
            self.assertEqual(ocr["default_entity_id"], "switch.scankeyd_google_drive_ocr_ocr")
            button = configs["homeassistant/button/scankeyd_google_drive/reset/config"]
            self.assertNotIn("state_topic", button)

    def test_changes_apply_at_once_and_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link, _ = self.link(tmp, BATCH_TIMEOUT="120")
            for key, value in (
                ("file_color", "gray"),
                ("file_ocr", "ON"),
                ("image_document", "one page per document"),
                ("image_resolution", "600 × 600 dpi"),
                ("paper", "Letter"),
                ("batch_timeout", "45"),
            ):
                link.apply(key, value)
            self.assertEqual(link.cfg.profiles["FILE"], Profile("gray", ocr=True))
            self.assertEqual(link.cfg.profiles["IMAGE"], Profile("color", "single", resolution=600))
            self.assertEqual(
                (link.cfg.dpi(link.cfg.profiles["IMAGE"]), link.cfg.dpi(link.cfg.profiles["FILE"])), (600, 300)
            )
            state = link.settings_state()
            self.assertEqual((state["image_resolution"], state["paper"]), ("600 × 600 dpi", "Letter"))
            self.assertEqual(link.cfg.batch_timeout, 45)
            state = link.settings_state()
            self.assertEqual((state["file_color"], state["file_ocr"], state["batch_timeout"]), ("gray", "ON", "45"))
            again, _ = self.link(tmp, BATCH_TIMEOUT="120")  # a restart: the settings file says 120, HA said 45
            self.assertEqual((again.cfg.batch_timeout, again.cfg.paper), (45, "letter"))
            self.assertEqual(again.cfg.profiles["FILE"], Profile("gray", ocr=True))
            self.assertEqual(again.cfg.profiles["EMAIL"], Profile("bw"))  # never changed: the settings file
            again.apply("reset", "PRESS")
            self.assertEqual(
                (again.cfg.batch_timeout, again.cfg.profiles["FILE"], again.cfg.paper), (120, Profile("bw"), "a4")
            )
            self.assertFalse(os.path.exists(os.path.join(tmp, "settings.json")))

    def test_only_expected_values_are_taken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link, _ = self.link(tmp)
            before = dict(link.cfg.profiles)
            for key, value in (
                ("file_color", "purple"),
                ("file_ocr", "maybe"),
                ("printer", "10.0.0.9"),
                ("file_resolution", "1200 dpi"),  # the old label
                ("file_resolution", "2400 × 2400 dpi"),  # not a real option
                ("paper", "A3"),
                ("batch_timeout", "5"),
                ("batch_timeout", "x"),
                ("../etc_color", "gray"),
            ):
                with self.assertRaises(ValueError, msg=(key, value)):
                    link.apply(key, value)
            self.assertEqual(link.cfg.profiles, before)
            with self.assertLogs("scankeyd.ha", "WARNING"):
                link._on_message(
                    link.client, None, mock.Mock(topic="scankeyd/scankeyd_google_drive/file_color/set", payload=b"pink")
                )
            self.assertFalse(os.path.exists(os.path.join(tmp, "settings.json")))

    def test_status_reaches_home_assistant(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            link, sent = self.link(tmp)
            link.status.listener = link.on_status
            link.status.update(registered_at=_time.time(), last_document="ocr/scan_x", open_pages=2)
            last = {t.rsplit("/", 1)[1]: p for t, p, r in sent}
            self.assertEqual((last["registered"], last["last_document"], last["open_pages"]), ("ON", "ocr/scan_x", "2"))
            link.status.update(last_error="printer certificate changed")
            self.assertEqual({t.rsplit("/", 1)[1]: p for t, p, r in sent}["registered"], "OFF")


class FilenameTest(unittest.TestCase):
    def test_function_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, FILENAME="scan_{date}_{function}")
            b = Batch(c, clock=lambda: 1_793_955_600.0)
            b.add(fakeprinter.fake_jpeg(comps=3), Profile("color", "close"), "IMAGE")
            self.assertRegex(b.last_written or "", r"^image/scan_\d{4}-\d{2}-\d{2}_image$")
            with self.assertRaises(config.ConfigError):
                cfg(tmp, FILENAME="scan_{function}/{date}")  # still no directories


ESCL = """<?xml version="1.0"?><scan:ScannerCapabilities xmlns:pwg="x" xmlns:scan="y">
<pwg:Version>2.93</pwg:Version><pwg:MakeAndModel>Brother DCP-L2627DWE</pwg:MakeAndModel>
<pwg:SerialNumber>E00000T0N0XXXXX</pwg:SerialNumber><scan:UUID>e3248000-80ce-11db-8000-0011aabbccdd</scan:UUID>
<scan:AdminURI>http://BRW0011AABBCCDD.local./net/net/airprint.html</scan:AdminURI>
<scan:Manufacturer>Brother</scan:Manufacturer></scan:ScannerCapabilities>"""


class PrinterIdentityTest(unittest.TestCase):
    def test_parse(self) -> None:
        from scankeyd import printerinfo

        info = printerinfo.parse(ESCL)
        self.assertEqual(
            (info["model"], info["serial_number"], info["mac"]),
            ("Brother DCP-L2627DWE", "E00000T0N0XXXXX", "00:11:aa:bb:cc:dd"),
        )
        self.assertEqual(printerinfo.parse("<html>no eSCL</html>"), {})

    def test_one_device_per_printer_kept_over_restarts(self) -> None:
        from scankeyd import printerinfo
        from scankeyd.ha import node_id

        one, two = printerinfo.parse(ESCL), printerinfo.parse(ESCL.replace("N0XXXXX", "N0YYYYY"))
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ca, cb = cfg(a, MQTT_HOST="h"), cfg(b, MQTT_HOST="h")  # same model, same target name "Scan"
            self.assertNotEqual(node_id(ca, one), node_id(cb, two))
            first = node_id(ca, one)
            self.assertEqual(first, "scankeyd_e00000t0n0xxxxx")
            self.assertEqual(node_id(ca, {}), first)  # printer away at the next start: the kept id
            with tempfile.TemporaryDirectory() as none:  # a printer without eSCL: the target name
                self.assertEqual(node_id(cfg(none, MQTT_HOST="h"), {}), "scankeyd_scan")
            self.assertEqual(
                node_id(cfg(a, MQTT_HOST="h", MQTT_NODE_ID="scankeyd_google_drive"), one), "scankeyd_google_drive"
            )

    @unittest.skipUnless(importlib.util.find_spec("paho"), "needs paho-mqtt (the ha extra)")
    def test_device_shows_the_printer(self) -> None:
        from scankeyd import printerinfo
        from scankeyd.daemon import Status
        from scankeyd.ha import HaLink

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, MQTT_HOST="127.0.0.1", TARGET_NAME="Google Drive")
            with mock.patch("scankeyd.printerinfo.fetch", return_value=printerinfo.parse(ESCL)):
                link = HaLink(c, Status(c))
            dev = link._device()
            self.assertEqual(
                (dev["name"], dev["manufacturer"], dev["model"], dev["serial_number"]),
                ("Brother DCP-L2627DWE Scanner (Google Drive)", "Brother", "DCP-L2627DWE", "E00000T0N0XXXXX"),
            )
            self.assertEqual(dev["configuration_url"], "https://127.0.0.1/")
            attrs = json.loads(link.printer_state()["printer/attributes"])
            self.assertEqual((attrs["address"], attrs["mac"]), ("127.0.0.1", "00:11:aa:bb:cc:dd"))


class CapabilitiesTest(unittest.TestCase):
    def test_options_are_what_the_printer_really_does(self) -> None:
        from scankeyd.capabilities import Capabilities, label

        caps = Capabilities({"bw": (600, 1200), "gray": (600, 1200), "color": (1200, 1200)}, False)
        self.assertEqual(
            [label(r) for _, r in caps.options("gray")],
            ["100 × 100 dpi", "200 × 200 dpi", "300 × 300 dpi", "600 × 600 dpi", "600 × 1200 dpi"],
        )
        self.assertEqual([s for s, _ in caps.options("color")], [100, 200, 300, 600, 1200])
        self.assertEqual(caps.real("gray", 2400), (600, 1200))
        self.assertEqual((caps.nearest("gray", 1200), caps.nearest("gray", 2400)), (1200, 1200))
        unprobed = Capabilities()  # before a probe: taken as asked up to 1200 (a 1200 setting shows 1200)
        self.assertEqual([label(r) for _, r in unprobed.options("color")][-1], "1200 × 1200 dpi")

    def test_probe_against_the_fake_printer(self) -> None:
        from scankeyd import capabilities

        srv = fakeprinter.start_scanner()
        fakeprinter.ScanHandler.jpeg = fakeprinter.fake_jpeg()
        try:
            port = srv.server_address[1]
            caps = capabilities.probe("127.0.0.1", lambda: False, lambda s: False, port=port, pause=0)
            assert caps is not None
            self.assertEqual(caps.maximum, {"bw": (600, 1200), "gray": (600, 1200), "color": (1200, 1200)})
            self.assertIs(caps.feeder, False)
            # a wanted scan goes first: the probe gives way
            self.assertIsNone(capabilities.probe("127.0.0.1", lambda: True, lambda s: False, port=port, pause=0))
            with tempfile.TemporaryDirectory() as tmp:
                capabilities.save(tmp, "SERIAL1", caps)
                kept = capabilities.load(tmp, "SERIAL1")
                assert kept is not None
                self.assertEqual(kept.maximum["color"], (1200, 1200))
                self.assertIsNone(capabilities.load(tmp, "OTHER"))  # another printer: probed again
            info: dict[str, object] = {}
            scanproto.scan_page("127.0.0.1", 1200, "gray", port=port, info=info)
            self.assertEqual(info["resolution"], (600, 1200))
        finally:
            srv.shutdown()
            srv.server_close()

    @unittest.skipUnless(importlib.util.find_spec("paho"), "needs paho-mqtt (the ha extra)")
    def test_home_assistant_lists_follow_the_colour_mode(self) -> None:
        from scankeyd.capabilities import Capabilities
        from scankeyd.daemon import Status
        from scankeyd.ha import HaLink

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, MQTT_HOST="127.0.0.1")
            caps = Capabilities({"bw": (600, 1200), "gray": (600, 1200), "color": (1200, 1200)}, False)
            link = HaLink(c, Status(c), caps, printer={})
            link.client = mock.Mock()
            link.client.is_connected.return_value = True
            link.apply("image_resolution", "1200 × 1200 dpi")  # Image is colour: 1200 is real
            self.assertEqual(link.cfg.profiles["IMAGE"].resolution, 1200)
            with self.assertRaises(ValueError):
                link.apply("file_resolution", "1200 × 1200 dpi")  # File is black-and-white: not offered
            link.apply("file_resolution", "600 × 1200 dpi")
            self.assertEqual(link.settings_state()["file_resolution"], "600 × 1200 dpi")
            link.apply("image_color", "gray")  # 1200 x 1200 is not a gray option: the nearest is kept
            self.assertEqual(link.settings_state()["image_resolution"], "600 × 1200 dpi")
            configs = {
                c.args[0]: c.args[1] for c in link.client.publish.call_args_list if c.args[0].endswith("/config")
            }
            options = json.loads(configs["homeassistant/select/scankeyd_scan/image_resolution/config"])["options"]
            self.assertEqual(options[-1], "600 × 1200 dpi")  # announced again for the new colour
            link.client.publish.reset_mock()
            link.apply("reset", "PRESS")  # Image back to colour: its list back to the colour one
            configs = {
                c.args[0]: c.args[1] for c in link.client.publish.call_args_list if c.args[0].endswith("/config")
            }
            options = json.loads(configs["homeassistant/select/scankeyd_scan/image_resolution/config"])["options"]
            self.assertEqual(options[-1], "1200 × 1200 dpi")


class ResolutionTest(unittest.TestCase):
    def test_per_function_and_a_new_document_when_it_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, RESOLUTION="300", FUNC_IMAGE="color,600")
            self.assertEqual((c.dpi(c.profiles["IMAGE"]), c.dpi(c.profiles["FILE"])), (600, 300))
            b = Batch(c)
            jpeg = fakeprinter.fake_jpeg(comps=3)
            b.add(jpeg, c.profiles["IMAGE"], "IMAGE")
            b.add(jpeg, Profile("color", resolution=300), "IMAGE")  # changed in HA mid-document
            doc = written(c, b)
            self.assertEqual((doc["resolution"], doc["pages"]), (600, ["page-0001.jpg"]))
            b.add(jpeg, Profile("color", "close", resolution=300), "IMAGE")
            doc = written(c, b)
            self.assertEqual((doc["resolution"], len(doc["pages"])), (300, 2))

    def test_shared_resolution_takes_what_a_function_takes(self) -> None:
        for dpi in config.RESOLUTIONS:
            self.assertEqual(config.load({"PRINTER": "10.0.0.1", "RESOLUTION": str(dpi)}).resolution, dpi)
        with self.assertRaises(config.ConfigError):
            config.load({"PRINTER": "10.0.0.1", "RESOLUTION": "4800"})


class CountersTest(unittest.TestCase):
    def test_pages_documents_failures_and_last(self) -> None:
        from scankeyd import counters

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            b = Batch(c)
            bw = scanproto.Bilevel(306, fakeprinter.fake_rows())
            b.add(bw, Profile("bw"), "FILE")
            b.add(bw, Profile("bw"), "FILE")
            b.add(fakeprinter.fake_jpeg(comps=3), Profile("color", finish="close"), "IMAGE")
            manual = Batch(c, subdir="manual")  # the scan command: counted in the same file
            manual.add(bw, Profile("bw", ocr=True, finish="single"), "OCR")
            with self.assertLogs("scankeyd.batch", "ERROR"):
                b.add(b"not a jpeg", Profile("color", finish="close"), "IMAGE")
            got = counters.read(tmp)
            self.assertEqual(got["pages"], {"FILE": 2, "IMAGE": 2, "OCR": 1})
            self.assertEqual(got["documents"], {"FILE": 1, "IMAGE": 1, "OCR": 1})
            self.assertEqual((got["documents_failed"], got["scan_failures"]), (1, 0))
            self.assertTrue(str(got["last_document"]).startswith("ocr/"))
            self.assertIsNotNone(got["last_scan_at"])

    @unittest.skipUnless(importlib.util.find_spec("paho"), "needs paho-mqtt (the ha extra)")
    def test_what_home_assistant_gets(self) -> None:
        from scankeyd.daemon import Status
        from scankeyd.ha import HaLink

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, MQTT_HOST="127.0.0.1")
            link = HaLink(c, Status(c))
            empty = link.status_state({})
            self.assertEqual(
                (empty["last_error"], empty["last_document"], empty["last_scan"]), ("none", "None", "None")
            )
            full = link.status_state(
                {"counters": {"pages": {"FILE": 2, "OCR": 3}, "documents": {"OCR": 1}, "scan_failures": 1}}
            )
            self.assertEqual((full["pages"], full["documents"], full["scan_failures"]), ("5", "1", "1"))
            self.assertEqual((full["ocr_pages"], full["image_pages"], full["documents_failed"]), ("3", "0", "0"))


class ReviewTest(unittest.TestCase):
    """The fixes of the review of 01-11-2026."""

    def test_bad_page_goes_to_failed_and_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            b = Batch(c)
            with self.assertLogs("scankeyd.batch", "ERROR"):
                b.add(b"not a jpeg at all", Profile("color", finish="close"))
            self.assertEqual(len(os.listdir(os.path.join(tmp, "failed"))), 1)
            b.add(fakeprinter.fake_jpeg(), Profile("color", finish="close"))
            self.assertTrue(b.last_written and os.listdir(os.path.join(tmp, "out")) == [])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
    def test_write_error_retries_with_growing_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = [1000.0]
            c = cfg(tmp)
            b = Batch(c, monotonic=lambda: t[0])
            os.chmod(c.output_dir, 0o500)
            try:
                with self.assertLogs("scankeyd.batch", "ERROR") as logs:
                    b.add(fakeprinter.fake_jpeg(), Profile("color", finish="close"))
                    for _ in range(20):  # the timer calls in every second: no new attempt, no new log
                        b.write_pending()
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(b.retry_at[os.listdir(b.out_dir)[0]][1], 30)
                t[0] += 31
                b.write_pending()  # second attempt, pause doubles, still logged only once
                self.assertEqual(list(b.retry_at.values())[0][1], 60)
            finally:
                os.chmod(c.output_dir, 0o700)
            t[0] += 61
            b.write_pending()
            self.assertTrue(b.last_written)
            self.assertEqual(os.listdir(b.out_dir), [])

    def test_slow_write_does_not_hold_up_the_next_page(self) -> None:
        import threading
        import time as _time

        check = batch.check_page

        def slow_check(data: bytes, ext: str) -> None:
            _time.sleep(2)
            check(data, ext)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("scankeyd.batch.check_page", side_effect=slow_check):
                c = cfg(tmp)
                b = Batch(c, synchronous=False)
                stop = threading.Event()
                w = threading.Thread(target=b.writer, args=(stop,), daemon=True)
                w.start()
                page = scanproto.Bilevel(306, fakeprinter.fake_rows())
                b.add(page, Profile("bw", finish="close"))
                _time.sleep(0.3)  # the writer is now inside the slow write
                started = _time.monotonic()
                b.add(page, Profile("bw"))
                self.assertLess(_time.monotonic() - started, 0.5)
                for _ in range(60):
                    if b.last_written:
                        break
                    _time.sleep(0.1)
                stop.set()
                b.wake.set()
                w.join(5)
            self.assertTrue(b.last_written)

    def test_auto_does_not_switch_to_snmp_on_a_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            r = registration.Registration(cfg(tmp, ADVERTISE_IP="192.168.1.10"))
            calls = []
            with (
                mock.patch.object(
                    registration.PhoenixRegistrar,
                    "register",
                    side_effect=registration.RegistrationError("phoenix: timed out"),
                ),
                mock.patch.object(registration.SnmpRegistrar, "register", side_effect=lambda e: calls.append(e)),
            ):
                with self.assertRaises(registration.RegistrationError):
                    r.renew()
            self.assertEqual((r.method, calls), ("auto", []))
            with (
                mock.patch.object(
                    registration.PhoenixRegistrar,
                    "register",
                    side_effect=registration.PhoenixMissing("phoenix: HTTP 404"),
                ),
                mock.patch.object(registration.SnmpRegistrar, "register", side_effect=lambda e: calls.append(e)),
            ):
                self.assertEqual(r.renew(), "snmp")
            self.assertEqual(r.method, "snmp")

    def test_digest_with_commas_in_quotes(self) -> None:
        h = registration._digest_header(
            'Digest realm="Machine, Inc", nonce="n1", qop="auth,auth-int", algorithm=MD5, opaque="o"',
            "POST",
            "/phoenix/mib",
        )
        self.assertIn('realm="Machine, Inc"', h)
        self.assertIn('opaque="o"', h)
        with self.assertRaises(registration.RegistrationError):
            registration._digest_header('Digest nonce="n1"', "POST", "/")

    def test_dedup_is_bounded(self) -> None:
        d = datagram.Deduplicator(max_entries=50)
        for n in range(500):
            d.is_new("p", press(REAL.replace(b"SEQ=1", b"SEQ=%d" % n)))
        self.assertLessEqual(len(d.seen), 50)

    def test_thread_failure_ends_the_service(self) -> None:
        from scankeyd.daemon import Daemon

        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(cfg(tmp, ADVERTISE_IP="192.168.1.10"))

            def boom() -> Never:
                raise RuntimeError("unexpected")

            with self.assertLogs("scankeyd.daemon", "ERROR"):
                d._guard(boom)()
            self.assertTrue(d.stop.is_set())
            self.assertIsInstance(d.failure, RuntimeError)

    def test_sigterm_stops_cleanly_and_umask_applies(self) -> None:
        import signal
        import subprocess
        import sys
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            env = dict(
                os.environ,
                PRINTER="127.0.0.1",
                ADVERTISE_IP="127.0.0.1",
                LISTEN_PORT=str(port),
                DATA_DIR=tmp,
                OUTPUT_DIR=os.path.join(tmp, "scans"),
                REGISTER_INTERVAL="15",
                PYTHONPATH=os.pathsep.join(
                    filter(
                        None,
                        (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.environ.get("PYTHONPATH")),
                    )
                ),
            )
            proc = subprocess.Popen(
                [sys.executable, "-m", "scankeyd", "run"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _time.sleep(1.5)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=10), 0)
            mode = os.stat(os.path.join(tmp, "status.json")).st_mode & 0o777
            self.assertEqual(mode & 0o007, 0, oct(mode))


class ReviewFixesTest(unittest.TestCase):
    """What the review before the first release found (06-11-2026)."""

    def test_a_filename_with_a_lone_brace_is_refused_at_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("scan_{date", "scan}_{date}"):
                with self.assertRaises(config.ConfigError) as e:
                    cfg(tmp, FILENAME=bad)
                self.assertIn("FILENAME", " ".join(e.exception.problems))
            self.assertEqual(cfg(tmp, FILENAME="scan_{{x}}_{date}").filename, "scan_{{x}}_{date}")

    def test_a_press_during_the_probe_waits_for_the_scan_port(self) -> None:
        import time

        from scankeyd.daemon import Daemon

        with tempfile.TemporaryDirectory() as tmp:
            d = Daemon(cfg(tmp, ADVERTISE_IP="192.168.1.10"))
            profile = d.cfg.profiles["FILE"]
            with (
                mock.patch.object(d.stop, "wait", return_value=False),
                mock.patch("scankeyd.scanproto.scan_page") as scan,
            ):
                d.probe_ended = time.monotonic()  # a capabilities question just now: the port is refused
                scan.side_effect = [ConnectionRefusedError(), scanproto.PrinterBusy("-NG 401"), b"page"]
                self.assertEqual(d._scan(profile), b"page")
                d.probe_ended = time.monotonic() - 3600  # no probe: a refused port fails at once
                scan.reset_mock()
                scan.side_effect = [ConnectionRefusedError()]
                with self.assertLogs("scankeyd.daemon", "ERROR"):
                    self.assertIsNone(d._scan(profile))
                self.assertEqual(scan.call_count, 1)

    def test_one_bad_mqtt_value_does_not_end_the_link(self) -> None:
        from scankeyd.daemon import Status
        from scankeyd.ha import HaLink

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, MQTT_HOST="127.0.0.1")
            link = HaLink(c, Status(c))
            link.client = mock.Mock()
            for value in (b"inf", b"1e400", b"nan"):
                msg = mock.Mock(topic=f"{link.base}/batch_timeout/set", payload=value)
                with self.assertLogs("scankeyd.ha", "WARNING"):
                    link._on_message(link.client, None, msg)  # must not raise into paho's thread
            self.assertEqual(c.batch_timeout, 120)

    def test_settings_that_are_ignored_are_not_pinned(self) -> None:
        from scankeyd.ha import Overrides, profile_text

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            with open(os.path.join(tmp, "settings.json"), "w") as f:
                json.dump({"profiles": {"FILE": "pink", "OCR": "gray,ocr"}, "batch_timeout": True}, f)
            o = Overrides(c)
            with self.assertLogs("scankeyd.ha", "WARNING"):
                o.load(c)
            self.assertEqual(o.data, {"profiles": {"OCR": profile_text(c.profiles["OCR"])}})
            with open(os.path.join(tmp, "settings.json"), "w") as f:
                f.write("[1, 2]")
            with self.assertLogs("scankeyd.ha", "WARNING"):
                Overrides(c).load(c)

    def test_presses_without_regid_and_seq_are_deduplicated_briefly(self) -> None:
        t = [0.0]
        d = datagram.Deduplicator(window=60, clock=lambda: t[0])
        anonymous = press(REAL.replace(b"REGID=17638;SEQ=1;", b""))
        self.assertTrue(d.is_new("p", anonymous))
        t[0] = 5.0
        self.assertFalse(d.is_new("p", anonymous))  # the printer's repeat
        t[0] = 16.0
        self.assertTrue(d.is_new("p", anonymous))  # a second press
        named = press(REAL)
        self.assertTrue(d.is_new("p", named))
        t[0] = 40.0
        self.assertFalse(d.is_new("p", named))

    def test_a_name_taken_meanwhile_gets_a_free_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp, FILENAME="scan_{date}")
            b = Batch(c, clock=lambda: 1_793_955_600.0, synchronous=False)
            b.add(fakeprinter.fake_jpeg(comps=3), Profile("color", "close"), "IMAGE")
            (name,) = os.listdir(b.out_dir)
            os.makedirs(os.path.join(c.output_dir, "image", name, "taken"))  # e.g. the scan command's
            b.write_pending()
            self.assertEqual(b.last_written, f"image/{name}_2")
            self.assertEqual(b.retry_at, {})
            self.assertTrue(os.path.isdir(os.path.join(c.output_dir, "image", name, "taken")))


class SecondReviewTest(unittest.TestCase):
    """What the second review before the first release found (06-11-2026)."""

    def test_a_filename_format_spec_is_refused_at_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("scan_{date:d}", "scan_{seq!z}", "scan_{seq:0>300}"):
                with self.assertRaises(config.ConfigError, msg=bad):
                    cfg(tmp, FILENAME=bad)
            self.assertEqual(cfg(tmp, FILENAME="scan_{seq:04d}").filename, "scan_{seq:04d}")

    def test_settings_json_with_odd_types_and_a_failed_save(self) -> None:
        from scankeyd.ha import Overrides

        with tempfile.TemporaryDirectory() as tmp:
            c = cfg(tmp)
            with open(os.path.join(tmp, "settings.json"), "w") as f:
                json.dump({"paper": ["a4"], "profiles": ["FILE"]}, f)
            o = Overrides(c)
            o.load(c)  # must not raise: the daemon would not start
            self.assertEqual((o.data, c.paper), ({}, "a4"))
            o.path = os.path.join(tmp, "missing", "settings.json")
            with self.assertLogs("scankeyd.ha", "WARNING") as logs:
                o.save(c, paper=True)
            self.assertIn("applied, but not kept", logs.output[0])


if __name__ == "__main__":
    unittest.main()
