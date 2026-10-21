from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import settings


def _fernet() -> Fernet:
    secret = (settings.jwt_signing_key or settings.jwt_secret).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(b"grace-otp-delivery-v1:" + secret).digest())
    return Fernet(key)


def encrypt_code(code: str) -> str:
    return _fernet().encrypt(code.encode("utf-8")).decode("ascii")


def decrypt_code(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("ascii"), ttl=None).decode("utf-8")


def encrypt_download_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_download_token(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("ascii"), ttl=None).decode("utf-8")
