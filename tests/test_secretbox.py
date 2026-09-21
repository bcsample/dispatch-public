"""Tests for encrypt-at-rest (brief.secretbox), Dispatch's own copy for the
P1-C Google Calendar token -- uses its own Keychain entry ("dispatch-tokens")
distinct from the voice assistant project's ("jarvis-tokens").

Uses an explicit key so the Keychain is never touched. Requires openssl
(present on macOS); skipped if it isn't.
"""

import shutil
from unittest.mock import MagicMock, patch

import pytest
from cryptography.exceptions import InvalidTag

from brief import secretbox

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not available"
)

_KEY = "deadbeef" * 8  # fixed test passphrase


def test_roundtrip():
    blob = secretbox.encrypt(b'{"refresh_token": "secret"}', key=_KEY)
    assert blob != b'{"refresh_token": "secret"}'  # actually encrypted
    assert secretbox.decrypt(blob, key=_KEY) == b'{"refresh_token": "secret"}'


def test_is_encrypted_detects_format():
    blob = secretbox.encrypt(b"hello", key=_KEY)
    assert secretbox.is_encrypted(blob) is True
    assert secretbox.is_encrypted(b'{"plain": "json"}') is False
    assert secretbox.is_encrypted(b"") is False


def test_wrong_key_fails_to_decrypt():
    # InvalidTag specifically, not bare Exception: the point of the test is that
    # AUTHENTICATION failed. A bare Exception would also pass if decrypt() blew
    # up with a TypeError on a malformed call -- green for the wrong reason,
    # while the guarantee it claims to check was broken.
    blob = secretbox.encrypt(b"hello", key=_KEY)
    with pytest.raises(InvalidTag):
        secretbox.decrypt(blob, key="0" * 64)


def test_new_format_is_gcm():
    blob = secretbox.encrypt(b"hello", key=_KEY)
    assert blob[:4] == b"JBX1"  # sealed with AES-256-GCM, not legacy CBC


def test_tamper_is_detected():
    # Authenticated encryption: a flipped ciphertext bit must fail to decrypt
    # (the old CBC scheme would have silently returned corrupted plaintext).
    blob = bytearray(secretbox.encrypt(b"sensitive token", key=_KEY))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        secretbox.decrypt(bytes(blob), key=_KEY)


def test_decrypts_legacy_cbc_blob():
    # An old AES-256-CBC file must still read so existing tokens migrate.
    import os
    import subprocess

    plaintext = b'{"refresh_token": "legacy"}'
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-pass", "env:K"],
        input=plaintext,
        capture_output=True,
        env={**os.environ, "K": _KEY},
    )
    legacy = proc.stdout
    assert legacy[:8] == b"Salted__"  # it's the old format
    assert secretbox.is_encrypted(legacy) is True
    assert secretbox.decrypt(legacy, key=_KEY) == plaintext


def test_locked_keychain_raises_instead_of_rotating_key():
    # A Keychain lookup can fail for reasons OTHER than "no key exists yet" (locked,
    # no session, transient error). Treating that as "missing" would generate a new
    # key and overwrite the real one via -U, permanently orphaning every file already
    # encrypted with it. Only a confirmed errSecItemNotFound (exit 44) may create one.
    locked = MagicMock(
        returncode=51, stdout="", stderr="User interaction is not allowed."
    )
    with patch("subprocess.run", return_value=locked) as m:
        with pytest.raises(RuntimeError, match="failed unexpectedly"):
            secretbox._get_or_create_key()
        assert m.call_count == 1  # must not proceed to add-generic-password


def test_genuinely_missing_key_is_created():
    not_found = MagicMock(returncode=44, stdout="", stderr="item not found")
    created = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=[not_found, created]) as m:
        key = secretbox._get_or_create_key()
        assert len(key) == 64  # 256-bit hex
        assert m.call_count == 2  # find (not found), then add — no -U in the add call
        add_args = m.call_args_list[1].args[0]
        assert "-U" not in add_args


def test_create_race_falls_back_to_reread_not_overwrite():
    # Two processes both see "not found" and both try to create; the loser must
    # re-read the winner's key rather than retry with -U (which would overwrite it).
    not_found = MagicMock(returncode=44, stdout="", stderr="")
    add_fails = MagicMock(returncode=45, stdout="", stderr="item already exists")
    winner_key = MagicMock(returncode=0, stdout="winnerkey123", stderr="")
    with patch("subprocess.run", side_effect=[not_found, add_fails, winner_key]):
        assert secretbox._get_or_create_key() == "winnerkey123"
