import pytest

from app.crypto import Cipher, DecryptionError


def test_round_trip_and_ciphertext_hides_the_secret():
    cipher = Cipher("a-long-enough-test-key")
    token = cipher.encrypt({"refresh_token": "1//secret-refresh-token"})
    assert b"secret-refresh-token" not in token
    assert cipher.decrypt(token) == {"refresh_token": "1//secret-refresh-token"}


def test_wrong_key_is_reported_clearly():
    token = Cipher("a-long-enough-test-key").encrypt({"refresh_token": "x"})
    with pytest.raises(DecryptionError):
        Cipher("another-long-enough-key").decrypt(token)


def test_short_keys_are_rejected():
    with pytest.raises(ValueError):
        Cipher("short")
