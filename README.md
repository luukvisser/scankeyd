# scankeyd

The **Scan** key of a Brother network printer, straight to a folder, without
Brother's drivers. A small daemon in a container: it registers itself on the
printer as a scan target, waits for the button, scans, and drops each document
in a folder as the printer sent it. Optional steps, each a container of its
own in [compose.yaml](compose.yaml), make PDFs of them and upload them:

```
printer → scankeyd → scans/new/<function>/<document>/
                       ├─ pdf    one PDF per document                     → scans/pdf/
                       ├─ tidy   the same, upright, straight, searchable  → scans/pdf/
                       └─ drive  scans/pdf/ → Google Drive (or any rclone destination)
```

- **No Brother software.** Registration, the button and the scan are
  implemented from observed behaviour ([docs/PROTOCOL.md](docs/PROTOCOL.md)):
  over HTTPS ("phoenix") on newer models, SNMP on older ones.
- **Documents, not pages.** The pages of one printer function (File, OCR,
  Image, E-mail on the display) form one document, finished after a quiet
  time, by a press of another function, or by the page itself.
- **Per function**: black-and-white, gray or colour, a resolution, and the
  wish for a text layer.
- **The scan as it came**: the printer's JPEG byte for byte, black-and-white
  as its 1-bit lines (PBM).
- **Home Assistant** (optional, MQTT): every setting as an entity, status and
  counters.
- **Small and closed**: a ~77 MB image without a shell, non-root, read-only;
  it talks only to the printer (and your MQTT broker, if set). No telemetry.

Not affiliated with or endorsed by Brother Industries.

## Status

Used daily with a **DCP-L2627DWE** (phoenix registration, flatbed). SNMP
registration and other models are tested only against the fake printer in the
test suite; reports are welcome ([Other printers](#other-printers)). No
document feeder or duplex support yet: that printer has neither.

## Quick start

On a Linux host with Docker Compose, in the printer's network, take
[compose.yaml](compose.yaml) and [scan.env.example](scan.env.example) from this
repository:

```sh
cp scan.env.example scan.env        # set PRINTER to the printer's address
mkdir -p scans/new scans/pdf data && sudo chown 1000:1000 scans/new scans/pdf data
docker compose --profile pdf up -d
docker compose exec scankeyd python3 -m scankeyd doctor
```

Let the printer reach the host on **UDP 54925** (e.g. `sudo ufw allow from
<printer> to any port 54925 proto udp`). Then at the printer: **Scan → to PC**
(or "to computer"), pick a function, pick **Scan** from the list, Start. The
PDF appears in `scans/pdf`; without `--profile pdf` you get only the raw
folders in `scans/new`. A printer that just woke up may take a minute to list
the target.

## What scankeyd writes

One folder per document, under the function that was pressed, written as a
hidden `.<name>.part` first and renamed when complete, so whatever watches
`scans/new` never sees half a document:

```
scans/new/ocr/scan_2026-11-06_101500/
    page-0001.jpg        gray or colour: the printer's JPEG, byte for byte
    page-0002.pbm        black-and-white: the printer's 1-bit lines, as binary PBM
    document.json
```

`document.json` (format 1): `format`, `function` (`FILE`, `OCR`, `IMAGE`,
`EMAIL`), `color` (`bw`, `gray`, `color`), `ocr` (the function's wish for a
text layer; scankeyd reads no text), `resolution` (dpi asked), `pages` (in
order), `scanned` (local time of the first page), `target`, `scankeyd`
(version).

## Steps

Each step is a profile in [compose.yaml](compose.yaml), with only the access it
needs:

| Profile | Image | What it does | Network |
|---|---|---|---|
| (none) | scankeyd, ~77 MB | the raw document folders in `scans/new` | the printer (and the MQTT broker, if set) |
| `pdf` | tidyscan `-base`, ~77 MB | one PDF per document in `scans/pdf` | none |
| `tidy` | tidyscan, ~480 MB | the same, pages turned upright and straightened (Tesseract), a text layer for the functions with `,ocr` (OCRmyPDF) | none |
| `drive` | rclone | moves each PDF to Google Drive, never overwriting one there | the default Docker network (internet and LAN) |

The `pdf` and `tidy` images are built from tidyscan's repository at a fixed
tag on the first `up` (a few minutes). Pick `pdf` or `tidy`, not both. Both
keep the scan lossless: it is drawn
turned and straightened inside the PDF, not re-encoded
([tidyscan](https://github.com/luukvisser/tidyscan), the companion project,
also usable on its own). Anything that reads a folder can take the PDFs from
`scans/pdf` instead of `drive`: Paperless-ngx (point its consumption folder
there; it reads the text itself, so `pdf` is enough), Syncthing, Nextcloud.

**Google Drive, once.** rclone needs a login of its own; with
`scope = drive.file` it sees only the files it made:

```sh
mkdir -p rclone && sudo chown 1000:1000 rclone
docker compose run --rm --entrypoint rclone drive config
#   n (new remote), name: drive, storage: drive, client_id/secret: empty,
#   scope: drive.file, root_folder_id: empty; without a browser on this machine
#   answer "use web browser" with n and run the `rclone authorize "drive" ...`
#   it shows on a computer with one
docker compose --profile tidy --profile drive up -d
```

`DRIVE_REMOTE=drive:Scans` in `scan.env` puts them in a folder Scans. The login
lives in `rclone/`: keep it private. Nextcloud, SMB, S3, SFTP, OneDrive and
others work the same way: [docs/DESTINATIONS.md](docs/DESTINATIONS.md).

## Home Assistant

With `MQTT_HOST` set, scankeyd appears in Home Assistant by itself (MQTT
discovery), one device per printer: per function its colour, resolution, text
layer and how a document ends; the document timeout and paper size; status,
the printer's identity and counters for long-term statistics. A change applies
to the next press and is kept in `data/settings.json`; what was never changed
there comes from `scan.env`. The resolution lists show what the printer really
scans at, read from the printer once.

Give scankeyd an MQTT user of its own, limited to its topics, and its password
in a file (`MQTT_PASSWORD_FILE`), not in `scan.env`: every step reads that
file, `drive` included. Entities, settings and the ACL:
[docs/HOME-ASSISTANT.md](docs/HOME-ASSISTANT.md).

## Networking

The printer sends the button press to **UDP 54925**, at the address scankeyd
registered, so that address must be reachable from the printer. Three ways,
in [examples/](examples/):

| Mode | When | What to set |
|---|---|---|
| **Host network** ([compose.host.yaml](examples/compose.host.yaml)) | Linux, simplest (compose.yaml uses it) | nothing |
| **Published port** ([compose.bridge.yaml](examples/compose.bridge.yaml)) | e.g. NAS boxes | `ports: ["54925:54925/udp"]` and `ADVERTISE_IP` = the host's LAN address |
| **macvlan** ([compose.macvlan.yaml](examples/compose.macvlan.yaml)) | the container needs its own LAN address | a free address on the printer's subnet |

scankeyd accepts button presses only from the printer's own address. Docker
Desktop (Mac, Windows) and rootless Docker hide that address on a published
port, so every press would be dropped: use a Linux host with host networking
or macvlan.

**Several printers**: one scankeyd per printer, each with its own `data/`,
`TARGET_NAME` and, with host networking, its own `LISTEN_PORT`.

## Configuration

All settings are environment variables, for every container in one file,
`scan.env` ([scan.env.example](scan.env.example) has the steps' settings too).
The whole configuration is checked at start; on any error scankeyd lists every
problem and does not start. After a change: the same `docker compose ... up -d`.

| Variable | Default | Meaning |
|---|---|---|
| `PRINTER` | (required) | the printer's IPv4 address |
| `TARGET_NAME` | `Scan` | name on the printer's display: 1-16 characters, letters, digits, space and `._+-()` |
| `ADVERTISE_IP` | detected | the address the printer sends the press to (see Networking) |
| `RESOLUTION` | `300` | 100, 200, 300, 600, 1200 or 2400 dpi; the printer scans at most at its real maximum |
| `PAPER` | `a4` | `a4` or `letter` |
| `BATCH_TIMEOUT` | `120` | seconds after the last page before a document is finished (10-3600) |
| `FUNC_FILE` | `bw` | what a press of that function does: `bw`, `gray` or `color`, optionally a resolution of its own (`,600`), `,ocr` (a text layer in the tidy step), and `,close` (this page finishes the document) or `,single` (a document of its own) |
| `FUNC_OCR` | `bw,ocr` | as above |
| `FUNC_IMAGE` | `color` | as above |
| `FUNC_EMAIL` | `bw` | as above |
| `FILENAME` | `scan_{date}_{time}` | document name: fields `{date}`, `{time}`, `{target}`, `{seq}`, `{function}`, with Python format specs (`{seq:04d}`); never taken from the printer; a name that exists gets a number |
| `TLS_PIN` | `tofu` | the printer's HTTPS certificate: `tofu` (pinned on first contact), its SHA-256 fingerprint, or `off` |
| `REGISTER_METHOD` | `auto` | `auto`, `phoenix` (HTTPS) or `snmp` |
| `REGISTER_INTERVAL` | `60` | seconds between registrations (15-300); the printer forgets one after 6 minutes |
| `LISTEN_PORT` | `54925` | the UDP port for button presses |
| `UMASK` | `027` | file mode mask for the scans: by default not readable by others |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |

## Commands

    python3 -m scankeyd run       the service (the default)
    python3 -m scankeyd doctor    check the configuration, the printer and the network (changes
                                  nothing; writes and removes one test file in the output folder)
    python3 -m scankeyd scan [FILE|OCR|IMAGE|EMAIL]   scan one page now, as a document of its own
    python3 -m scankeyd health    exit 0 when the last registration is recent (the healthcheck)

In the container: `docker compose exec scankeyd python3 -m scankeyd doctor`.

## Troubleshooting

`docker compose logs -f scankeyd` shows what happens.

- **The target is not on the printer's list**: wait for the next registration
  (a minute); `doctor` checks the route and the ports; the log says
  "registration failed" with the reason.
- **A press does nothing**: the press does not arrive (a firewall, or the
  advertised address is not reachable: `doctor`), or the log says "datagram
  from ... ignored (not the printer)" (a published port that hides the sender,
  see Networking) or "press for ... is not ours" (another target was chosen).
- **"printer certificate changed"**: after a firmware update the printer may
  have a new certificate; once you trust it, delete `data/tls-pin-*.sha256`
  (or set `TLS_PIN`).
- **Failures**: a document's pages stay in `data/` until delivered, also over a
  restart; a page that is not a complete JPEG or PBM moves its document to
  `data/failed/`; a write error is retried with a growing pause. In the steps,
  a document that cannot become a PDF moves to `scans/new/.failed/`.

To stop: `docker compose down`; the printer forgets the target within 6
minutes.

## The image

`ghcr.io/luukvisser/scankeyd:0.1.0`, amd64 and arm64 (a Raspberry Pi works):
Python 3.13 from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
(musl), scankeyd and paho-mqtt on an empty image, with no shell, no package
manager and no copyleft software. It runs only as a container (it is not a
PyPI package). Use a fixed version tag; there is no `latest`. Each release
image has an SBOM and build provenance and is signed with cosign (keyless):

```sh
cosign verify ghcr.io/luukvisser/scankeyd:0.1.0 \
  --certificate-identity-regexp '^https://github.com/luukvisser/scankeyd/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Licence texts of everything in it are under `/usr/share/licenses`
([THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)). To build it yourself:
`docker build -t scankeyd .`.

## Other printers

If you try another model, please open an issue with the model, its firmware
version and the output of `doctor` (replace the addresses with `x` if you
prefer). Most likely to work: Brother models of the last years with "Scan to
PC" on their display.

## Development

With [uv](https://docs.astral.sh/uv/):

    uv sync --locked
    uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
    uv run --no-sync ty check
    uv run --no-sync python -m unittest -v tests.test_all

Run the tests as a normal user (one is skipped as root). A fake printer in the
tests speaks every protocol above, so they need no hardware.

## Licence

BSD-3-Clause, see [LICENSE](LICENSE). Written by Luuk Visser with Claude Code
(Claude Opus 5.5); Luuk reviewed the changes and tested them on his own
printer. Formerly brscan-free.

Related projects, all using SNMP registration and/or Brother's own drivers:
[rumpeltux/brother-scand](https://github.com/rumpeltux/brother-scand),
[asmw/brother-scan](https://github.com/asmw/brother-scan),
[FrankAbelbeck/brotherscankeyd](https://github.com/FrankAbelbeck/brotherscankeyd);
for HP printers, [node-hp-scan-to](https://github.com/manuc66/node-hp-scan-to).
