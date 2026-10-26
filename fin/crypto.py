"""Access-token encryption with a Fernet key held only in the OS keyring."""
from threading import Lock

import keyring
from cryptography.fernet import Fernet

_SERVICE = "fin"
_KEY_NAME = "fernet-key"
_LOCK = Lock()


def _fernet(*, create: bool) -> Fernet:
    try:
        with _LOCK:
            backend = keyring.get_keyring()
            if backend.priority <= 0 or type(backend).__module__.startswith("keyrings.alt"):
                raise RuntimeError("Unsafe keyring")
            key = keyring.get_password(_SERVICE, _KEY_NAME)
            if key is None and create:
                key = Fernet.generate_key().decode("ascii")
                keyring.set_password(_SERVICE, _KEY_NAME, key)
                if keyring.get_password(_SERVICE, _KEY_NAME) != key:
                    raise RuntimeError("Keyring did not persist key")
            if key is None:
                raise RuntimeError("Missing encryption key")
            return Fernet(key.encode("ascii"))
    except Exception:
        raise RuntimeError("Token encryption key is unavailable") from None


def encrypt_token(token: str) -> bytes:
    """Encrypt a nonempty token; never fall back to a file or plaintext key."""
    if not isinstance(token, str) or not token:
        raise ValueError("A nonempty token is required")
    return _fernet(create=True).encrypt(token.encode("utf-8"))


def decrypt_token(blob) -> str:
    """Decrypt using an existing key only, failing closed on any error."""
    try:
        return _fernet(create=False).decrypt(bytes(blob)).decode("utf-8")
    except Exception:
        raise RuntimeError("Access token could not be decrypted") from None
