"""Paired devices: the client certificates that open Apport's public address.

Apport is reachable from outside only through a separate public host in Nginx Proxy Manager. That
host demands a client certificate signed by Apport's own CA (ssl_verify_client) and turns away every
connection without one before it reaches this server. It then passes three headers on, which it
always overwrites, so a client cannot set them itself:

    X-Apport-Public: 1
    X-Apport-Client-Verify: $ssl_client_verify   (SUCCESS for a valid certificate)
    X-Apport-Client-Serial: $ssl_client_serial    (hex)
    X-Apport-Client-Fingerprint: $ssl_client_fingerprint   (SHA-1, hex)

The home host sends X-Apport-Internal with the value of APPORT_INTERNAL_KEY instead. When that key
is set, a request without it counts as public: a proxy host that lost its settings, or a caller that
reaches this server past the proxy, is then refused instead of being taken for the home network.

A certificate alone is not enough: its serial must still be in devices.json, so removing a device
here locks it out at once, without touching Nginx. The password login stays on top of that.

A device gets its certificate at home: the app makes a key that never leaves the device and sends
its public half to POST /api/devices/pair. That route refuses requests that came through the
public host, so a stolen password alone can never pair a new device from outside.

The CA key lives in APPORT_STATE_DIR/device-ca/ and is created on first use. Only ca.pem goes to
Nginx Proxy Manager.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import users

router = APIRouter()

CERT_DAYS = 2 * 365
_LOCK = threading.Lock()


def _state() -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data"))


def _ca_dir() -> Path:
    folder = _state() / "device-ca"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _devices_path() -> Path:
    return _state() / "devices.json"


def ca() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Apport's device CA, created on first use."""
    folder = _ca_dir()
    key_path, cert_path = folder / "ca.key", folder / "ca.pem"
    with _LOCK:
        if not key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apport device CA")])
            now = datetime.now(timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=20 * 365))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=False, content_commitment=False, key_encipherment=False,
                        data_encipherment=False, key_agreement=False, key_cert_sign=True,
                        crl_sign=True, encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
                .sign(key, hashes.SHA256())
            )
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ))
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    return cert, key


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_devices_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(devices: dict[str, dict[str, Any]]) -> None:
    path = _devices_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(devices, indent=1))
    temporary.replace(path)


def _serial_key(serial: str | None) -> str:
    """Nginx sends the serial as hex; normalise case and leading zeros."""
    return (serial or "").strip().lower().lstrip("0")


def is_public(request: Request) -> bool:
    """Whether a request counts as coming from outside. With APPORT_INTERNAL_KEY set, everything
    that does not carry that key does."""
    if request.headers.get("x-apport-public") == "1":
        return True
    key = os.environ.get("APPORT_INTERNAL_KEY", "")
    if not key:
        return False
    return not secrets.compare_digest(request.headers.get("x-apport-internal", "").encode(), key.encode())


def _fingerprint(request: Request) -> str:
    return (request.headers.get("x-apport-client-fingerprint") or "").strip().lower()


def device_for(request: Request) -> dict[str, Any] | None:
    """The paired device a public request comes from, or None when it has no valid one."""
    if request.headers.get("x-apport-client-verify") != "SUCCESS":
        return None
    key = _serial_key(request.headers.get("x-apport-client-serial"))
    if not key:
        return None
    with _LOCK:
        device = _load().get(key)
    # The serial names the device, the fingerprint pins the one certificate issued for it.
    if device is not None and device.get("fingerprint") and not secrets.compare_digest(
        str(device["fingerprint"]).encode(), _fingerprint(request).encode()
    ):
        return None
    return device


def check_public(request: Request, user: str | None) -> str | None:
    """Why a request through the public host is refused, or None when it may pass."""
    device = device_for(request)
    if device is None:
        return "Unknown device"
    # A session from one user on another user's device is refused; the login checks the same.
    if user is not None and device.get("user") != user:
        return "Device belongs to another user"
    return None


def note_seen(request: Request) -> None:
    """Remember when a device was last used, at most once a minute per device."""
    key = _serial_key(request.headers.get("x-apport-client-serial"))
    now = time.time()
    with _LOCK:
        devices = _load()
        device = devices.get(key)
        if device is None:
            return
        # Devices paired before fingerprints were stored learn theirs on the first request.
        learned = not device.get("fingerprint") and len(_fingerprint(request)) == 40
        if not learned and now - float(device.get("lastSeen") or 0) < 60:
            return
        if learned:
            device["fingerprint"] = _fingerprint(request)
        device["lastSeen"] = now
        _save(devices)


class PairRequest(BaseModel):
    # P-256 public key, uncompressed point (X9.63, 65 bytes) as base64: what iOS exports.
    publicKey: str = Field(..., max_length=200)
    name: str = Field(..., min_length=1, max_length=80)


@router.post("/api/devices/pair")
def pair(body: PairRequest, request: Request) -> dict[str, Any]:
    """Certify the device's public key. Only from home: never through the public host."""
    if is_public(request):
        raise HTTPException(status_code=403, detail="Pairing works only on the home network")
    user = users.CURRENT.get()
    if user is None and users.multi_user():
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        point = base64.b64decode(body.publicKey, validate=True)
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid public key") from None

    ca_cert, ca_key = ca()
    serial = int.from_bytes(secrets.token_bytes(16), "big") >> 1
    name = body.name.strip()
    owner = user or users.OWNER
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, name),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, owner),
        ]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = format(serial, "x")
    with _LOCK:
        devices = _load()
        devices[key] = {
            "user": owner,
            "name": name,
            "pairedAt": time.time(),
            "expiresAt": cert.not_valid_after_utc.timestamp(),
            "lastSeen": None,
            "fingerprint": cert.fingerprint(hashes.SHA1()).hex(),
        }
        _save(devices)
    print(f"device paired: user={owner} name={name!r} serial={key}", flush=True)
    return {
        "certificate": cert.public_bytes(serialization.Encoding.PEM).decode(),
        "serial": key,
        "expiresAt": cert.not_valid_after_utc.timestamp() * 1000,
    }


@router.get("/api/devices")
def list_devices(request: Request) -> dict[str, Any]:
    user = users.CURRENT.get() or users.OWNER
    current = _serial_key(request.headers.get("x-apport-client-serial")) if is_public(request) else ""
    with _LOCK:
        devices = _load()
    items = [
        {
            "serial": serial,
            "name": device.get("name", ""),
            "pairedAt": float(device.get("pairedAt") or 0) * 1000,
            "lastSeen": float(device["lastSeen"]) * 1000 if device.get("lastSeen") else None,
            "expiresAt": float(device.get("expiresAt") or 0) * 1000,
            "current": serial == current,
        }
        for serial, device in devices.items()
        if device.get("user") == user
    ]
    items.sort(key=lambda item: item["pairedAt"], reverse=True)
    return {"items": items}


@router.delete("/api/devices/{serial}")
def remove_device(serial: str) -> dict[str, Any]:
    """Lock a device out at once; Nginx still accepts its certificate, this server no longer."""
    user = users.CURRENT.get() or users.OWNER
    key = _serial_key(serial)
    with _LOCK:
        devices = _load()
        device = devices.get(key)
        if device is None or device.get("user") != user:
            raise HTTPException(status_code=404, detail="Unknown device")
        devices.pop(key)
        _save(devices)
    print(f"device removed: user={user} name={device.get('name')!r} serial={key}", flush=True)
    return {"success": True}
