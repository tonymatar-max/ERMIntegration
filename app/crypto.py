"""Encryption for per-user Redmine API keys at rest.

Uses Fernet (symmetric) with the app secret from appkey.get_key() — an
app-level key that protects the DB file on disk, NOT a per-user Redmine
API key (those are supplied by each user in the Settings page).
"""

from cryptography.fernet import Fernet

from . import appkey


def _get_fernet():
    return Fernet(appkey.get_key().encode())


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
