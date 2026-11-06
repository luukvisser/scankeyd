# The protocol

How the Scan key of a Brother network printer reaches a computer, as
scankeyd implements it. Written from observed network traffic and the
printer's behaviour, on a DCP-L2627DWE in 2026, for interoperability. Other
models may differ in details; the older form of the scan protocol is
implemented by [rumpeltux/brother-scand](https://github.com/rumpeltux/brother-scand).

There are three parts: the computer **registers** a scan target on the
printer, the printer **sends a datagram** when someone picks that target at
the panel, and the computer then **scans** over a TCP connection.

## 1. Registration

The printer keeps a list of scan targets per function. A target is one string:

    TYPE=BR;BUTTON=SCAN;USER=<name>;FUNC=<function>;HOST=<address>:<port>;APPNUM=<n>;DURATION=360;BRID=;

| Field | Meaning |
|---|---|
| `USER` | the name on the printer's display; scankeyd allows at most 16 characters |
| `FUNC`, `APPNUM` | the function: `IMAGE` = 1, `EMAIL` = 2, `OCR` = 3, `FILE` = 5 |
| `HOST` | where the printer sends the button datagram (UDP); 54925 by convention |
| `DURATION` | seconds until the printer forgets the target |

One string per function is written to the object
`1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0`. The printer drops a target after
`DURATION` seconds, so it has to be written again well before that;
scankeyd does so every 60 seconds.

**Newer models ("phoenix")**: HTTPS `POST /phoenix/mib` with HTTP digest
authentication as user `Public`, password `0000`. That account is fixed and
the same on every printer, so anyone on the network can register targets;
that is the printer's design. Body:

```json
{"request": [{"key": "1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0", "string_value": "TYPE=BR;BUTTON=SCAN;..."}, ...]}
```

The answer has one entry per request, each with `"status": 0` when accepted.
The certificate is self-signed; scankeyd pins it on first contact.

**Older models**: the same string by SNMPv1 SET to the same object, with
community `internal`.

A printer without `/phoenix/mib` (connection refused, or HTTP 404) is an older
model. A timeout says nothing about that: a sleeping printer also does not
answer.

## 2. The button datagram

When someone picks the target at the panel, the printer sends one UDP
datagram to `HOST`:

    02 00 xx 30  TYPE=BR;BUTTON=SCAN;USER="Scan";FUNC=FILE;HOST=<address>:54925;APPNUM=5;P1=0;P2=0;P3=0;P4=0;REGID=17638;SEQ=1;

Four header bytes (`02 00`, a byte that varies, `30`), then ASCII
`KEY=value;` pairs. `USER` comes back in quotes. No answer is expected. The
printer repeats the datagram after a few seconds; `REGID` with `SEQ` identifies
a press.

The panel then shows that it waits for the computer, and the printer accepts
a scan over its own scan protocol. An eSCL scan job is refused (HTTP 500)
while the panel waits, so the button path needs the protocol below. When idle,
the same printer does scan over eSCL.

## 3. The scan protocol (TCP 54921)

On connecting, the printer greets with `+OK 200\r\n`, or `-NG 401\r\n` when it
is busy. Commands are `ESC`, a command name, `\n`, `KEY=value\n` lines, and the
byte `0x80`.

1. **Scan parameters**: `ESC SSP` with, among others, `PSRC=FB` (flatbed),
   `RESO=300,300`, `CLR=` (`TEXT` for black-and-white, `GRAY256`, `C24BIT`),
   `COMP=` (`RLENGTH` for black-and-white, `JPEG` otherwise), `BRIT=50`,
   `CONT=50`, `MRGN=0,0,0,0`. The printer answers `00 'SSP'`, a 16-bit length
   and a payload of TLVs (16-bit id, 16-bit length, value), after a two-byte
   prefix. Id `0x2001` is the paper source (2 flatbed, 1 feeder), `0x2002`
   the resolution the printer will really scan at (two 16-bit numbers, x and
   y: its own limits applied, so asking 9600 dpi shows the maximum), `0x2003`
   the largest scan area in pixels at that resolution, as two 32-bit numbers
   (width, height; height 0 for a feeder the printer does not have). Closing
   the connection here, without the next command, makes the printer refuse
   its scan port for about 35 seconds.
2. **Start**: `ESC XSC` with `RESO=300,300`, `AREA=0,0,<width>,<height>` and
   `MODE=NORMAL`. The printer acknowledges with `00 00`.
3. **Page data**, in blocks with a 14-byte header (two type bytes, then
   twelve bytes of which bytes 6-7 are the data length, little-endian, and
   bytes 10-11 the lines done):
   - type `00 02`: a piece of JPEG. The page is the concatenation of the
     pieces.
   - type `00 01`: one black-and-white scan line, PackBits-compressed, with
     1 = black. Every line decodes to the same number of bytes.
   - type `00 21` plus four bytes: the end of the page. From the panel, the
     DCP-L2627DWE sends only two of those four bytes and holds the rest while
     it asks "scan another page?"; scankeyd waits 2 seconds for them, then
     takes the page as complete.

The printer asks at the panel whether to scan another page. After "No" it
sends nothing more over the connection, so the end of a document is a matter
of time: scankeyd finishes a document after `BATCH_TIMEOUT` seconds without
a new page.

## The panel's settings (ESC GKP)

`ESC GKP\n` and `0x80`, on a connection of its own, ask what was chosen on
the panel for the press at hand. The answer is a status byte, `GKP`, a 32-bit
length (little-endian) and fields (16-bit id, 16-bit length, value) up to
`0xf0f0`; per field the raw bytes, `0xff` where the panel left something
unset. Field `0x1002` holds the resolution (x and y as 16-bit numbers),
colour type, paper, direction, brightness and contrast. Without a press the
status is `0x81` and there are no fields. Unlike scan settings without a
scan, this question leaves the scan port free. scankeyd asks it on every
press and only logs the answer for now (the layout follows Brother's brscan5
and go-brother-scan-to-network-server).

## What scankeyd does not use

- Brother's other commands on 54921 (`ESC Q` status, `ESC QDI` device
  information, `ESC CKD` feeder check), which its own driver sends first.
- Scanning from the automatic document feeder and duplex (`PSRC=ADF`); the
  printer this was built on has neither. The capabilities probe only asks
  whether there is a feeder (an `ESC SSP` with `PSRC=ADF`, without a scan).
- Port 54921's older form (`ESC I`, `ESC X`) used by older models.
