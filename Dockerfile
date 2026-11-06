# scankeyd: docker build -t scankeyd .   (no copyleft software in the image)

# Python from python-build-standalone (its musl build: libpython links only musl's libc), musl's
# dynamic loader (MIT), the IANA time zone data from the tzdata wheel and scankeyd, on an empty
# image: no shell, no package manager, no GNU tools. Left out of that Python: _dbm
# (Berkeley DB, Sleepycat licence), Tk and the X11 libraries, pip (it vendors certifi, MPL-2.0),
# the test suite. The licence texts of everything that ships go along in /usr/share/licenses.
# Every RUN below runs on the build machine and only fetches, checks and unpacks, so an arm64
# image is built without emulating arm64.

# python-build-standalone release 20261013, CPython 3.13.16; the sha256 values are the release's
# SHA256SUMS. The runtime comes from the install_only build; the licence texts of its components
# only ship in the "full" build of the same release, so that is fetched too, for licenses/ alone.
FROM --platform=$BUILDPLATFORM alpine@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS python
ARG TARGETARCH
# DL3018: apk packages are not pinned by version (only zstd, for unpacking; nothing of it ships).
# hadolint ignore=DL3018
RUN apk add --no-cache zstd
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN set -eu; \
    pbs=https://github.com/astral-sh/python-build-standalone/releases/download/20261013; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64; \
             run_sha=a177ce931ddc616c0e76eebc6a583196739dc704d47af853f5958cb95b2b1c96; \
             full_sha=cfbe69fc57b40fa93b459aff97066d8c516c07ce6e4938eb5001640e542e7a54 ;; \
      arm64) arch=aarch64; \
             run_sha=9f503edbbfe384c9bf770d99ce787f7aea60bbc6a96ffb3b5933f72c9e71fb6f; \
             full_sha=cdfb095bb5f532bba2dbb17500fa60509d38e15678c72d227ae4e32ce5a6e74b ;; \
      *) echo "unsupported platform: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    name="cpython-3.13.16%2B20261013-$arch-unknown-linux-musl"; \
    wget -q -O /tmp/run.tgz "$pbs/$name-install_only_stripped.tar.gz"; \
    echo "$run_sha  /tmp/run.tgz" | sha256sum -c -; \
    wget -q -O /tmp/full.tar.zst "$pbs/$name-lto-full.tar.zst"; \
    echo "$full_sha  /tmp/full.tar.zst" | sha256sum -c -; \
    mkdir -p /opt /out/licenses/python; \
    tar -xzf /tmp/run.tgz -C /opt; \
    mkdir -p /tmp/full; zstd -dc /tmp/full.tar.zst | tar -x -C /tmp/full; \
    cp /tmp/full/python/licenses/* /out/licenses/python/; \
    cp /opt/python/lib/python3.13/LICENSE.txt /out/licenses/python/LICENSE.cpython-distribution.txt; \
    l=/out/licenses/python; \
    rm -f $l/LICENSE.bdb.txt $l/LICENSE.tcl.txt $l/LICENSE.tix.txt $l/LICENSE.libX11.txt \
          $l/LICENSE.libXau.txt $l/LICENSE.libxcb.txt; \
    p=/opt/python; y=$p/lib/python3.13; \
    rm -rf $p/lib/libtcl* $p/lib/itcl* $p/lib/tcl* $p/lib/tk* $p/lib/thread* $p/lib/pkgconfig \
      $p/include $p/share $y/lib-dynload/_dbm.* $y/lib-dynload/_tkinter.* $y/tkinter $y/idlelib \
      $y/turtledemo $y/turtle.py $y/dbm/ndbm.py $y/ensurepip $y/test $y/lib2to3 \
      $y/site-packages/pip $y/site-packages/pip-* $y/config-3.13-*-linux-musl \
      $p/bin/idle3* $p/bin/pip* $p/bin/2to3*; \
    find /opt/python -name __pycache__ -prune -exec rm -rf {} +; \
    rm -rf /tmp/run.tgz /tmp/full.tar.zst /tmp/full

# musl's licence text, from the source release of the version in the Alpine image below (1.2.5;
# checked against musl's signing key when pinned).
FROM --platform=$BUILDPLATFORM alpine@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS musl-licence
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
RUN wget -q -O /tmp/musl.tgz https://musl.libc.org/releases/musl-1.2.5.tar.gz \
 && echo "a9a118bbe84d8764da0ea0d28b3ab3fae8477fc7e4085d90102b8596fc7c75e4  /tmp/musl.tgz" | sha256sum -c - \
 && mkdir -p /out/licenses/musl \
 && tar -xzf /tmp/musl.tgz -O musl-1.2.5/COPYRIGHT > /out/licenses/musl/COPYRIGHT \
 && rm /tmp/musl.tgz

# The target platform's Alpine, for musl's dynamic loader only (no RUN: nothing is executed in it).
FROM alpine@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f AS musl-loader

# The time zone data, paho-mqtt (the optional Home Assistant link; BSD-3-Clause of its two licences),
# the users and the folders. Both wheels only with their sha256.
FROM --platform=$BUILDPLATFORM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS site
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
RUN pip download --no-deps --no-cache-dir --dest /tzwheel "tzdata==2026.5" \
 && echo "b683bd1b6659ddcd810ff02ad09ba821d4bf1065072805063eb35c49617905ac  /tzwheel/tzdata-2026.5-py2.py3-none-any.whl" | sha256sum -c - \
 && pip download --no-deps --no-cache-dir --only-binary :all: --dest /pahowheel "paho-mqtt==2.1.0" \
 && echo "6db9ba9b34ed5bc6b6e3812718c7e06e2fd7444540df2455d2c51bd58808feee  /pahowheel/paho_mqtt-2.1.0-py3-none-any.whl" | sha256sum -c - \
 && pip install --no-deps --no-cache-dir --no-compile --target /site /pahowheel/paho_mqtt-2.1.0-py3-none-any.whl \
 && mkdir -p /root-fs/usr/share/licenses/paho-mqtt \
 && cp /site/paho_mqtt-2.1.0.dist-info/licenses/LICENSE.txt /root-fs/usr/share/licenses/paho-mqtt/LICENSE.txt \
 && python3 -c "import zipfile; zipfile.ZipFile('/tzwheel/tzdata-2026.5-py2.py3-none-any.whl').extractall('/tz')" \
 && mkdir -p /root-fs/usr/share/licenses/tzdata /root-fs/etc /root-fs/scans /root-fs/data /root-fs/tmp \
 && cp -r /tz/tzdata/zoneinfo /root-fs/usr/share/zoneinfo \
 && cp /tz/tzdata-2026.5.dist-info/licenses/LICENSE /root-fs/usr/share/licenses/tzdata/LICENSE \
 && cp /tz/tzdata-2026.5.dist-info/licenses/licenses/LICENSE_APACHE /root-fs/usr/share/licenses/tzdata/LICENSE_APACHE \
 && printf 'root:x:0:0::/:/nonexistent\nscankeyd:x:1000:1000::/data:/nonexistent\n' > /root-fs/etc/passwd \
 && printf 'root:x:0:\nscankeyd:x:1000:\n' > /root-fs/etc/group \
 && chown 1000:1000 /root-fs/scans /root-fs/data && chmod 1777 /root-fs/tmp

FROM scratch AS base
COPY --from=musl-loader /lib/ld-musl-*.so.1 /lib/
COPY --from=python /opt/python /opt/python
COPY --from=python /out/ /usr/share/
COPY --from=musl-licence /out/ /usr/share/
COPY --from=site /root-fs/ /
COPY --from=site /site /opt/site
COPY LICENSE THIRD-PARTY-NOTICES.md /usr/share/licenses/scankeyd/
COPY scankeyd /app/scankeyd
ENV PATH=/opt/python/bin PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/opt/site \
    OUTPUT_DIR=/scans DATA_DIR=/data
# Any uid works (set `user:` in compose); this one exists so a plain `docker run` is not root.
USER 1000:1000
EXPOSE 54925/udp
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD ["/opt/python/bin/python3", "-m", "scankeyd", "health"]
ENTRYPOINT ["/opt/python/bin/python3", "-m", "scankeyd"]
CMD ["run"]
