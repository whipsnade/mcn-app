from base64 import b64encode

import pytest
from pydantic import SecretStr

from app.runtime_config.crypto import EncryptedSecretValue, RuntimeConfigError, SecretCipher


def _cipher(*, active: str = "v1") -> SecretCipher:
    return SecretCipher(
        master_keys={
            "v1": b"1" * 32,
            "v2": b"2" * 32,
        },
        active_key_version=active,
    )


def test_encrypt_uses_random_nonce_and_round_trips_without_exposing_secret() -> None:
    cipher = _cipher()
    aad = b"tenant-1:secret-1:model_api_key:v1"
    first = cipher.encrypt(SecretStr("sk-test-only-secret"), aad=aad)
    second = cipher.encrypt(SecretStr("sk-test-only-secret"), aad=aad)

    assert first.nonce != second.nonce
    assert cipher.decrypt(first, aad=aad).get_secret_value() == "sk-test-only-secret"
    assert "sk-test-only-secret" not in repr(first)
    assert "sk-test-only-secret" not in str(first.model_dump(mode="json"))


def test_secret_cipher_fails_closed_for_wrong_aad_and_tampered_ciphertext() -> None:
    cipher = _cipher()
    aad = b"tenant-1:secret-1:model_api_key:v1"
    encrypted = cipher.encrypt(SecretStr("opaque-value"), aad=aad)

    with pytest.raises(RuntimeConfigError, match="runtime_secret_decrypt_failed"):
        cipher.decrypt(encrypted, aad=b"tenant-2:secret-1:model_api_key:v1")

    tampered = encrypted.model_copy(
        update={"ciphertext": b64encode(b"tampered").decode("ascii")}
    )
    with pytest.raises(RuntimeConfigError, match="runtime_secret_decrypt_failed"):
        cipher.decrypt(tampered, aad=aad)


def test_key_rotation_keeps_old_reads_and_uses_new_key_for_writes() -> None:
    old = _cipher(active="v1")
    rotated = _cipher(active="v2")
    old_aad = b"tenant-1:secret-1:datatap_token:v1"
    new_aad = b"tenant-1:secret-2:datatap_token:v2"
    old_value = old.encrypt(SecretStr("old-value"), aad=old_aad)
    new_value = rotated.encrypt(SecretStr("new-value"), aad=new_aad)

    assert rotated.decrypt(old_value, aad=old_aad).get_secret_value() == "old-value"
    assert new_value.key_version == "v2"
    assert rotated.decrypt(new_value, aad=new_aad).get_secret_value() == "new-value"


def test_encrypted_secret_schema_rejects_invalid_key_material() -> None:
    with pytest.raises(RuntimeConfigError, match="runtime_secret_key_invalid"):
        SecretCipher(master_keys={"v1": b"short"}, active_key_version="v1")

    with pytest.raises(RuntimeConfigError, match="runtime_secret_key_invalid"):
        SecretCipher(master_keys={"v1": b"1" * 32}, active_key_version="missing")

    with pytest.raises(ValueError):
        EncryptedSecretValue(
            algorithm="not-aes-gcm",
            nonce="AA==",
            ciphertext="AA==",
            key_version="v1",
            fingerprint="x",
            masked_value="***",
        )
