# Home Assistant

With `MQTT_HOST` set, scankeyd announces itself over MQTT discovery, so Home
Assistant makes the device and its entities by itself. Without `MQTT_HOST`
none of this runs and `data/settings.json` is not read.

## Entities

- **Per printer function** (File, OCR, Image, E-mail): a select for the colour,
  one for the resolution, a switch for the text layer (the `,ocr` wish) and a
  select for how a document ends (collect pages, close after each page, one
  page per document). Counters per function exist too, off until you enable
  them.
- **Shared**: the document timeout (`BATCH_TIMEOUT`), the paper size, a button
  "Back to the settings file" (drops every change made in Home Assistant) and
  a button "Read scanner capabilities".
- **Status**: last document, open pages, last scan, registered with the
  printer, last error, the real resolution of the last scan, and "Printer"
  (its address, MAC address and the scanner's own address as attributes).
- **Counts** for long-term statistics: pages, documents, failed scans and
  documents not delivered.

A change made in Home Assistant applies to the next press, without a restart,
and is kept in `data/settings.json`; what was never changed there comes from
`scan.env`. Only values of the expected form are taken; anything else is
logged and dropped.

## Resolution lists

The lists show what the printer really scans at, width × height, per colour
mode. scankeyd asks the printer once (about 3 minutes, only while nothing is
scanned; a press goes first), keeps the answer per printer in
`data/capabilities.json`, and asks again for another printer or with "Read
scanner capabilities". Until then the lists offer up to 1200 × 1200. A
DCP-L2627DWE, for example, scans black-and-white and gray at most at
600 × 1200 dpi and colour at 1200 × 1200, and has no document feeder; asked
for more, a printer delivers more pixels without more detail, so those are not
offered.

## One device per printer

The device id is chosen at the first start: `scankeyd_` plus the printer's
serial number (from its eSCL page; the target name when the printer has none
or is not reachable then), and kept in `data/ha-node.json`, as a new id would
leave the old entities behind. Two printers of one model are two devices. The
device shows the printer, e.g. "Brother DCP-L2627DWE Scanner (Scan)", its
serial number and a link to its web page.

## Broker settings and access

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_HOST` | (off) | the broker; scankeyd must reach it (with host networking it does) |
| `MQTT_PORT` | `1883` (`8883` with TLS) | |
| `MQTT_TLS` | `off` | `on` for TLS; `MQTT_CA` a CA certificate file to check the broker with |
| `MQTT_USERNAME`, `MQTT_PASSWORD_FILE` | | the login; `MQTT_PASSWORD` also works, but every step reads `scan.env`, so keep the password in a mounted file |
| `MQTT_NODE_ID` | chosen at first start | the device id, e.g. `scankeyd_office`, to know it in advance for the ACL |
| `MQTT_TOPIC_PREFIX` | `scankeyd` | |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | |

Give scankeyd an MQTT user of its own that may only touch its own topics. For
Mosquitto, with `MQTT_NODE_ID=scankeyd_office`:

```
user scankeyd
topic readwrite scankeyd/scankeyd_office/#
topic write homeassistant/+/scankeyd_office/#
```

Whoever may publish to these topics can change scankeyd's settings and start a
capabilities probe, nothing more.

The link uses [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python)
(BSD-3-Clause), the one dependency outside the standard library, in the image
and only loaded when `MQTT_HOST` is set.
