"""Encrypt/decrypt per-tenant secrets (a client's Twilio auth token).

AES-256-GCM, layout: base64( iv[12] + ciphertext + tag[16] ). This matches the
HVR site's Node implementation (lib/crypto.ts) so the admin can ENCRYPT a token
and this agent can DECRYPT it — same ENCRYPTION_KEY on both.

ENCRYPTION_KEY = standard-base64 of 32 random bytes. Generate once:
    python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"

Guarded: no ENCRYPTION_KEY (or no `cryptography`) → returns None and the app
falls back to the shared Twilio account from env (Model A).
"""

import base64
import os
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # falls back to env creds
    AESGCM = None


def _key() -> Optional[bytes]:
    raw = os.getenv("ENCRYPTION_KEY")
    if not raw or AESGCM is None:
        return None
    try:
        key = base64.b64decode(raw)
        return key if len(key) == 32 else None
    except Exception:  # noqa: BLE001
        return None


def encrypt(plaintext: str) -> Optional[str]:
    key = _key()
    if not key or plaintext is None:
        return None
    iv = os.urandom(12)
    ct_with_tag = AESGCM(key).encrypt(iv, plaintext.encode(), None)  # ciphertext||tag
    return base64.b64encode(iv + ct_with_tag).decode()


def decrypt(token: Optional[str]) -> Optional[str]:
    key = _key()
    if not token or not key:
        return None
    try:
        blob = base64.b64decode(token)
        iv, data = blob[:12], blob[12:]  # data = ciphertext||tag
        return AESGCM(key).decrypt(iv, data, None).decode()
    except Exception:  # noqa: BLE001
        return None
