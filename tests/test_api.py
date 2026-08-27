"""Tests for the pure-Python parts of api.py - no network, no credentials.

Runs against a synthetic sample frame shaped like a real capture (see
docs/sample_telemetry_frame.json), not the author's actual device data.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)

from .loader import load_module

api = load_module("api")

SAMPLE_FRAME_PATH = (
    Path(__file__).parent.parent / "docs" / "sample_telemetry_frame.json"
)


def test_parses_real_shaped_telemetry_frame() -> None:
    raw = SAMPLE_FRAME_PATH.read_text(encoding="utf-8")
    parsed = api.parse_telemetry_frame(raw)

    assert parsed is not None
    assert parsed["battery_percentage"] == 76
    assert parsed["switch_ac"] == 1
    assert parsed["ac_discharge_pow"] == 320


def test_ignores_ack_frames() -> None:
    assert api.parse_telemetry_frame(json.dumps({"message": "success"})) is None


def test_ignores_non_json() -> None:
    assert api.parse_telemetry_frame("not json") is None


def test_ignores_json_that_is_not_an_object() -> None:
    assert api.parse_telemetry_frame(json.dumps([1, 2, 3])) is None


def test_rsa_encrypt_round_trips_with_pkcs1v15() -> None:
    """Confirms _rsa_encrypt uses the same padding as the server expects.

    Uses a throwaway keypair generated here, not the real server key - this
    only proves our encrypt call is internally consistent PKCS1v1.5, which is
    what a captured login request showed the app itself sending.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")

    ciphertext_b64 = api._rsa_encrypt(der_b64, "hunter2@example.com")

    decrypted = private_key.decrypt(
        base64.b64decode(ciphertext_b64), padding.PKCS1v15()
    )
    assert decrypted == b"hunter2@example.com"


def test_encrypt_key_round_trips_via_load_der_public_key() -> None:
    """Sanity check that our base64/DER handling matches what load_der_public_key expects."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = private_key.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    der_b64 = base64.b64encode(der).decode("ascii")

    loaded = load_der_public_key(base64.b64decode(der_b64))
    assert loaded.public_numbers() == private_key.public_key().public_numbers()
