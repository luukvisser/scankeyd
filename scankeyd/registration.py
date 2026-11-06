"""Registering this host as a scan target on the printer.

Newer models ("phoenix" firmware): HTTPS POST /phoenix/mib with HTTP digest
authentication as Public:0000, a fixed account every Brother driver uses.
Older models: SNMPv1 SET with community "internal". Both write the same
string to OID 1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0, once per printer function.
The printer forgets a registration after DURATION seconds, so it is renewed
well before that (every REGISTER_INTERVAL seconds).

Every connection is opened and closed within one call (the leak class found
in Brother's own tool cannot come back), and the printer's TLS certificate
is pinned: trust on first use by default, stored in DATA_DIR.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import socket
import ssl
from typing import TYPE_CHECKING

from . import snmp

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

OID = "1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0"
APPNUM = {"IMAGE": 1, "EMAIL": 2, "OCR": 3, "FILE": 5}
DURATION = 360
DIGEST_USER, DIGEST_PASSWORD = "Public", "0000"


class RegistrationError(Exception):
    pass


class PinMismatch(RegistrationError):
    pass


class PhoenixMissing(RegistrationError):
    """The printer has no phoenix endpoint (no HTTPS port, or 404): an SNMP-only model."""


def entry(name: str, func: str, host: str, port: int) -> str:
    return (
        f"TYPE=BR;BUTTON=SCAN;USER={name};FUNC={func};HOST={host}:{port};"
        f"APPNUM={APPNUM[func]};DURATION={DURATION};BRID=;"
    )


def local_address_for(printer: str) -> str:
    """The address this host uses to reach the printer (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((printer, 9))
        return s.getsockname()[0]


class Pinner:
    def __init__(self, cfg: Config) -> None:
        self.mode = cfg.tls_pin
        self.path = os.path.join(cfg.data_dir, f"tls-pin-{cfg.printer}.sha256")

    def check(self, der: bytes | None) -> None:
        if self.mode == "off":
            return
        if der is None:  # getpeercert gives None when the server sent no certificate
            raise PinMismatch("the printer sent no TLS certificate, so it cannot be checked against the pin")
        fp = hashlib.sha256(der).hexdigest()
        if self.mode != "tofu":
            if fp != self.mode:
                raise PinMismatch(f"printer certificate {fp} does not match TLS_PIN")
            return
        try:
            with open(self.path) as f:
                pinned = f.read().strip()
        except FileNotFoundError:
            tmp = f"{self.path}.{secrets.token_hex(4)}.tmp"
            with open(tmp, "w") as f:
                f.write(fp + "\n")
            os.replace(tmp, self.path)
            log.info("pinned the printer's certificate on first contact: sha256 %s", fp)
            return
        if fp != pinned:
            raise PinMismatch(
                f"printer certificate changed: {fp}, pinned {pinned}. If the printer's "
                f"firmware was updated, delete {self.path} or set TLS_PIN to the new value"
            )


def _digest_header(challenge: str, method: str, uri: str) -> str:
    # key=value or key="value, with commas"
    params = {
        k.lower(): (quoted if quoted or not bare else bare)
        for k, quoted, bare in re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]*))', challenge[len("Digest ") :])
    }
    if "realm" not in params or "nonce" not in params:
        raise RegistrationError(f"incomplete digest challenge: {challenge[:100]}")
    qops = [q.strip() for q in params.get("qop", "auth").split(",")]
    if params.get("algorithm", "MD5").upper() != "MD5" or "auth" not in qops:
        raise RegistrationError(f"unsupported digest challenge: {challenge[:100]}")
    realm, nonce = params["realm"], params["nonce"]
    cnonce, nc = secrets.token_hex(8), "00000001"
    ha1 = hashlib.md5(f"{DIGEST_USER}:{realm}:{DIGEST_PASSWORD}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
    header = (
        f'Digest username="{DIGEST_USER}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
        f'algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"'
    )
    if "opaque" in params:
        header += f', opaque="{params["opaque"]}"'
    return header


class PhoenixRegistrar:
    def __init__(self, cfg: Config, scheme: str = "https", port: int | None = None) -> None:
        self.cfg = cfg
        self.scheme = scheme
        self.port = port
        self.pinner = Pinner(cfg)

    def _request(self, body: bytes, auth: str | None) -> tuple[int, str, bytes]:
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed; pinned instead
            conn = http.client.HTTPSConnection(self.cfg.printer, self.port or 443, timeout=10, context=ctx)
        else:
            conn = http.client.HTTPConnection(self.cfg.printer, self.port or 80, timeout=10)
        try:
            conn.connect()
            if self.scheme == "https":
                sock = conn.sock
                assert isinstance(sock, ssl.SSLSocket)  # HTTPSConnection wraps it after connect()
                self.pinner.check(sock.getpeercert(binary_form=True))
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = auth
            conn.request("POST", "/phoenix/mib", body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.getheader("WWW-Authenticate", ""), resp.read(4096)
        finally:
            conn.close()

    def register(self, entries: list[str]) -> None:
        body = json.dumps({"request": [{"key": OID, "string_value": e} for e in entries]}).encode()
        try:
            status, challenge, data = self._request(body, None)
            if status == 401 and challenge.startswith("Digest "):
                status, _, data = self._request(body, _digest_header(challenge, "POST", "/phoenix/mib"))
        except ConnectionRefusedError as e:
            raise PhoenixMissing(f"phoenix: no HTTPS on the printer ({e})") from e
        except (OSError, http.client.HTTPException) as e:
            raise RegistrationError(f"phoenix: {e}") from e
        if status == 404:
            raise PhoenixMissing("phoenix: no /phoenix/mib on the printer (HTTP 404)")
        if status != 200:
            raise RegistrationError(f"phoenix: HTTP {status}")
        try:
            answers = json.loads(data)["response"]
            bad = [a for a in answers if a.get("status") != 0]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise RegistrationError(f"phoenix: unexpected answer {data[:120]!r}") from e
        if len(answers) != len(entries) or bad:
            raise RegistrationError(f"phoenix: printer refused {len(bad) or 'some'} entries: {data[:200]!r}")


class SnmpRegistrar:
    def __init__(self, cfg: Config, port: int = 161) -> None:
        self.cfg = cfg
        self.port = port

    def register(self, entries: list[str]) -> None:
        for e in entries:
            try:
                err = snmp.set_string(self.cfg.printer, OID, e.encode(), community=b"internal", port=self.port)
            except OSError as ex:
                raise RegistrationError(f"snmp: {ex}") from ex
            if err != 0:
                raise RegistrationError(f"snmp: printer answered error-status {err}")


class Registration:
    """Picks the method (auto: phoenix; SNMP only when the printer has no phoenix endpoint) and
    renews the entries. A timeout or a sleeping printer never switches the method."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.method = cfg.register_method

    @property
    def host(self) -> str:
        """The advertised address; detected anew each time unless ADVERTISE_IP is set (DHCP may move it)."""
        if self.cfg.advertise_ip:
            return self.cfg.advertise_ip
        try:
            return local_address_for(self.cfg.printer)
        except OSError as e:
            raise RegistrationError(f"no route to the printer: {e}") from e

    def entries(self) -> list[str]:
        host = self.host
        return [entry(self.cfg.target_name, f, host, self.cfg.listen_port) for f in ("IMAGE", "OCR", "EMAIL", "FILE")]

    def renew(self) -> str:
        entries = self.entries()
        if self.method in ("auto", "phoenix"):
            try:
                PhoenixRegistrar(self.cfg).register(entries)
                if self.method == "auto":
                    log.info("printer uses HTTPS (phoenix) registration")
                    self.method = "phoenix"
                return "phoenix"
            except PhoenixMissing as e:
                if self.method == "phoenix":
                    raise
                log.info("%s; trying SNMP", e)
        SnmpRegistrar(self.cfg).register(entries)
        if self.method == "auto":
            log.info("printer uses SNMP registration")
            self.method = "snmp"
        return "snmp"
