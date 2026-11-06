# Security

## Reporting

Please report a vulnerability through GitHub's private vulnerability reporting
(the repository's Security tab), not in a public issue.

## Threat model

scankeyd runs on a home or office network, receives datagrams anyone on
that network can send, and talks to a printer whose answers it cannot fully
trust.

| Threat | Measure |
|---|---|
| Spoofed button datagrams | accepted only from the configured printer's address; repeats are dropped. The worst case is a scan of whatever lies on the glass, into your own folder |
| Malformed or hostile datagrams | a strict parser: a length limit, the expected header, ASCII `KEY=value;` only, a limit on fields; it never raises. Fuzz-tested |
| Datagram text reaching a shell or a path | never: no hooks run on datagram content; file names come only from `FILENAME` and the daemon's own counters |
| A fake printer on registration (ARP spoofing) | the printer's TLS certificate is pinned (trust on first use by default); a changed certificate stops registration until you confirm it |
| Hostile scan data | a size limit per page, a deadline per page and an idle timeout; a page that cannot be read moves to `/data/failed/` instead of blocking later scans |
| Resource leaks | every connection is opened and closed within one call; a test runs many registration cycles against a fake printer and checks that open file descriptors stay flat |
| Reach of the container | runs as a non-root user; works with `read_only`, `cap_drop: [ALL]` and `no-new-privileges`; only `/scans` and `/data` need to be writable; no outbound connections other than to the printer (and the MQTT broker, when `MQTT_HOST` is set) |
| Commands over MQTT (with `MQTT_HOST`) | whoever may publish to scankeyd's topics can change its settings (colour, resolution, paper, timeout) and start a capabilities probe, nothing more; only values of the expected form are taken, and the settings go to `/data/settings.json` only. Give scankeyd an MQTT user of its own and an ACL for its topics (README, "Home Assistant") |
| Scans readable by others on the host | Scans are written with umask `027` by default |
| Anyone on the network registering targets on the printer | the printer's fixed `Public:0000` account; not something scankeyd can change |

No telemetry, no update checks.

## Supply chain

- Base images are pinned by digest; the Python runtime, musl's licence source
  and the time zone data by sha256.
- The published image has no shell, package manager or other tools: only
  Python, musl's loader, the time zone data and this code.
- The release image is built by GitHub Actions, with an SBOM and build
  provenance, and signed with cosign (keyless). See the README for how to
  verify a signature.
- Actions in the workflows are pinned by commit.
