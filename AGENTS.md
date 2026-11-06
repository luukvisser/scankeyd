# Instructions for coding agents

scankeyd answers the Scan key of Brother network printers and writes the
scans to a folder: one folder per document with the pages as the printer sent
them and a document.json. It registers with the printer (phoenix over HTTPS
with digest authentication, or SNMP), listens for the button datagram on UDP
54925 and fetches the pages with Brother's scan protocol on TCP 54921. Making
a PDF, OCR and page straightening are not part of it: they are steps of their
own on the folder (`tidyscan watch`, from the separate project
[tidyscan](https://github.com/luukvisser/tidyscan)), as optional
profiles in compose.yaml.

## Layout

- `scankeyd/`: the daemon. `daemon.py` (threads), `__main__.py` (the commands, `health` among them), `registration.py`
  and `snmp.py` (registering with the printer), `datagram.py` (the button
  datagram), `scanproto.py` (the scan protocol), `batch.py` (pages to
  document folders), `config.py` (environment variables, checked at start),
  `ha.py` (Home Assistant over MQTT, optional), `capabilities.py` (what the
  scanner really does), `printerinfo.py` (model and serial number from eSCL),
  `counters.py` (counts for statistics).
- `tests/`: `test_all.py` and `fakeprinter.py`, a fake printer that speaks
  every protocol above; no hardware needed.
- `docs/PROTOCOL.md`: the protocol as observed. `examples/`: compose files per
  network mode.
- `Dockerfile`: the published image, on an empty image: Python, scankeyd and paho-mqtt.

## Commands

Set up as in the README's "Development" section (`uv sync --locked`), then:

    uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
    uv run --no-sync ty check
    uv run --no-sync python -m unittest -v tests.test_all

Run the tests as a normal user (one test is skipped as root). The image:
`docker build -t scankeyd .`.

## Rules

- The daemon uses the Python standard library only, apart from paho-mqtt for
  the optional Home Assistant link (imported only with `MQTT_HOST`). Anything
  else that needs a package (OCR, straightening, uploads) is a separate step
  on the output folder, not part of this repository.
- Everything from the network is untrusted input: check lengths and values
  before use, and keep the existing limits and timeouts. Do not weaken the TLS
  pinning of the printer's certificate.
- Scans are stored as they came from the printer: never decode and encode an
  image again.
- No network access at run time other than to the printer and, when set, the
  MQTT broker.
- Pin what is fetched: base images by digest, actions by commit, downloads by
  sha256. Keep the existing pins unless asked to bump them.
- Every change in behaviour gets a test; anything that touches the printer
  protocol is tested against `tests/fakeprinter.py`. Never send test traffic to
  a real printer without the user's explicit consent.
- Describe protocol findings from observation only; never add Brother's code,
  binaries or decompiled material.
- Never commit real scans, logs or captures with personal data, printer serial
  numbers, MAC or IP addresses of a real network, or secrets.
- Do not change LICENSE, LICENSES/, REUSE.toml, THIRD-PARTY-NOTICES.md or the
  release workflow unless asked to.
- Code reads like the code around it: comment density, naming, idiom.

## Commits and pull requests

State AI assistance with an `Assisted-by:` trailer naming the tool and model,
for example `Assisted-by: Claude Code (claude-opus-5-5)`. The person who opens
the pull request must understand, have tested and be able to explain every
change; see [CONTRIBUTING.md](CONTRIBUTING.md).
