# Third-party software in the image

scankeyd's own code is BSD-3-Clause-licensed (see [LICENSE](LICENSE)). The image
also contains other people's software, under their own licences.

## The published image (`ghcr.io/luukvisser/scankeyd:<version>`)

Built from an empty image; it contains only what is listed here, with no copyleft
software. The licence texts are in the image under `/usr/share/licenses`:
`python/` (CPython and the libraries built into it), `musl/`, `tzdata/` and
`scankeyd/` (this project's LICENSE and this file).

| Component | Licence |
|---|---|
| CPython 3.13, from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (musl build; without `_dbm`, Tk, pip and the test suite) | PSF-2.0 |
| built into that Python: OpenSSL 3 | Apache-2.0 |
| libffi, expat | MIT |
| zlib | Zlib |
| bzip2 | bzip2-1.0.6 |
| xz (liblzma) | 0BSD |
| mpdecimal | BSD-2-Clause |
| libuuid (util-linux), libedit | BSD-3-Clause |
| ncurses | X11 |
| SQLite | public domain |
| musl's dynamic loader (from Alpine's musl 1.2.5) | MIT |
| the IANA time zone database, from the `tzdata` wheel | public domain (data), Apache-2.0 (packaging) |
| [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) 2.1.0, for the optional Home Assistant link | EPL-2.0 OR BSD-3-Clause; used under BSD-3-Clause |
