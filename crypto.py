"""Encrypt/decrypt secrets stored per tenant (e.g. a client's Twilio auth token).

Uses Fernet (AES) with the ENCRYPTION_KEY env var. Generate a key once:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Guarded: with no ENCRYPTION_KEY (or no `cryptography`), these return None and the
app falls back to the shared Twilio account from env (Model A).
"""

import os
from typing import Optional

try:
    from cryptography.fernet import Fernet
except ImportError:  # falls back to env creds
    Fernet = None


def _fernet():
    key = os.getenv("ENCRYPTION_KEY")
    if not key or Fernet is None:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:  # noqa: BLE001 — bad key → fall back
        return None


def encrypt(plaintext: str) -> Optional[str]:
    f = _fernet()
    if not f or plaintext is None:
        return None
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    f = _fernet()
    if not f:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001
        return None
