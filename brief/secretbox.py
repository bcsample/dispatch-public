"""Keychain-backed authenticated encrypt-at-rest for local secrets (OAuth tokens).

Ported from the voice assistant project's jarvis.secretbox for Dispatch's own P1-C Google
Calendar credential — same primitives (AES-256-GCM, key in the macOS login
Keychain) but its OWN Keychain entry ("dispatch-tokens", not "jarvis-tokens"),
so a leaked token file from either project can't be used to decrypt the
other's (least privilege). See the voice assistant project's secretbox.py for the design
rationale (legacy CBC migration, atomic writes, Keychain error handling) --
unchanged here.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SERVICE = "dispatch-tokens"
_ACCOUNT = "default"

_MAGIC_GCM = b"JBX1"
_MAGIC_CBC = b"Salted__"
_NONCE_LEN = 12

_ERR_ITEM_NOT_FOUND = 44


def _get_or_create_key() -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", _SERVICE, "-a", _ACCOUNT, "-w"],
        capture_output=True,
        text=True,
    )
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    if out.returncode != _ERR_ITEM_NOT_FOUND:
        raise RuntimeError(
            f"Keychain lookup for '{_SERVICE}' failed unexpectedly "
            f"(exit {out.returncode}): {out.stderr.strip()}. Refusing to "
            f"generate a replacement key, which would silently make every "
            f"existing encrypted file unreadable."
        )
    key = secrets.token_hex(32)
    add = subprocess.run(
        ["security", "add-generic-password", "-s", _SERVICE, "-a", _ACCOUNT, "-w", key],
        capture_output=True,
        text=True,
    )
    if add.returncode == 0:
        return key
    out2 = subprocess.run(
        ["security", "find-generic-password", "-s", _SERVICE, "-a", _ACCOUNT, "-w"],
        capture_output=True,
        text=True,
    )
    if out2.returncode == 0 and out2.stdout.strip():
        return out2.stdout.strip()
    raise RuntimeError(
        f"could not create or find a Keychain key for '{_SERVICE}' "
        f"(add exit {add.returncode}): {add.stderr.strip()}"
    )


def _aes_key(passphrase: str) -> bytes:
    return hashlib.sha256(passphrase.encode()).digest()


def _decrypt_legacy_cbc(blob: bytes, key: str) -> bytes:
    env = {**os.environ, "DISPATCH_SECRET_KEY": key}
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-d",
            "-pbkdf2",
            "-pass",
            "env:DISPATCH_SECRET_KEY",
        ],
        input=blob,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.decode("utf-8", "replace").strip() or "openssl failed"
        )
    return proc.stdout


def encrypt(data: bytes, key: str | None = None) -> bytes:
    nonce = secrets.token_bytes(_NONCE_LEN)
    sealed = AESGCM(_aes_key(key or _get_or_create_key())).encrypt(nonce, data, None)
    return _MAGIC_GCM + nonce + sealed


def decrypt(blob: bytes, key: str | None = None) -> bytes:
    k = key or _get_or_create_key()
    if blob[:4] == _MAGIC_GCM:
        nonce = blob[4 : 4 + _NONCE_LEN]
        return AESGCM(_aes_key(k)).decrypt(nonce, blob[4 + _NONCE_LEN :], None)
    if blob[:8] == _MAGIC_CBC:
        return _decrypt_legacy_cbc(blob, k)
    raise RuntimeError("unrecognized ciphertext format")


def is_encrypted(blob: bytes) -> bool:
    return blob[:4] == _MAGIC_GCM or blob[:8] == _MAGIC_CBC


def atomic_write_bytes(path: os.PathLike | str, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
