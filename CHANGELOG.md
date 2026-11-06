# Changelog

## 0.1.0 (2026-11-06)

First public release (formerly brscan-free).

- Registration on the printer over HTTPS ("phoenix") or SNMP, chosen at start
  and renewed every minute; the printer's certificate pinned on first use.
- The Scan key's datagram on UDP 54925, accepted from the configured printer
  only.
- Brother's scan protocol on TCP 54921: flatbed, JPEG (gray, colour) and 1-bit
  black-and-white, up to the printer's real maximum resolution.
- Documents per printer function (`FUNC_FILE`, `FUNC_OCR`, `FUNC_IMAGE`,
  `FUNC_EMAIL`): colour mode, resolution, the wish for a text layer, and how a
  document ends; each delivered as a folder with the pages as the printer sent
  them and a `document.json`.
- Optional Home Assistant link over MQTT discovery: every setting as an
  entity, status, the printer's identity and counters for long-term
  statistics; one device per printer. Uses paho-mqtt 2.1.0 (BSD-3-Clause),
  loaded only when MQTT is on.
- The scanner's real capabilities read from the printer itself, once per
  printer: its maximum resolution per colour mode and whether it has a feeder.
- compose.yaml with optional steps as profiles (`pdf`, `tidy`, `drive`) and one
  settings file for all of them.
- `doctor`, `scan` and `health` commands.
- The image (amd64, arm64): ~77 MB, Python from python-build-standalone on an
  empty image, with no shell and no copyleft software.
