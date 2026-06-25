"""Password hashing: round-trip, wrong password, malformed input."""
from __future__ import annotations

from app.api.passwords import hash_password, verify_password


def test_hash_verify_round_trip():
    encoded = hash_password("s3cret", iterations=1000)
    assert verify_password("s3cret", encoded) is True


def test_wrong_password_fails():
    encoded = hash_password("s3cret", iterations=1000)
    assert verify_password("nope", encoded) is False


def test_salts_are_unique():
    assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)


def test_malformed_encoded_returns_false():
    for bad in ["", "not-a-hash", "pbkdf2_sha256$x$y$z", "a$b$c"]:
        assert verify_password("whatever", bad) is False
