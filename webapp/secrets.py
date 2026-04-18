"""
Gemeinsame Fernet-Verschlüsselung (SECRET_KEY-basiert)
=======================================================
Wird von LDAP-Bind-Passwort, SMTP-Passwort, Scanner-Run-As-Passwort und
Teams-Client-Secret gemeinsam genutzt. Key wird lazy aus dem aktuellen
``current_app.config["SECRET_KEY"]`` abgeleitet – bei SECRET_KEY-Rotation
müssen betroffene Secrets neu gesetzt werden.
"""

from __future__ import annotations

import base64
import hashlib
import logging

log = logging.getLogger(__name__)

_ENC_PREFIX = "enc:"


def _fernet_for_key(secret_key: str):
    from cryptography.fernet import Fernet
    raw = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _fernet_from_app():
    from flask import current_app
    return _fernet_for_key(current_app.config.get("SECRET_KEY", ""))


def encrypt(plain: str) -> str:
    """Verschlüsselt einen Klartext-String (mit ``enc:``-Präfix für Alt-Bestands-
    Erkennung). Leerstring → Leerstring."""
    if not plain:
        return ""
    token = _fernet_from_app().encrypt(plain.encode()).decode()
    return _ENC_PREFIX + token


def decrypt(stored: str) -> str:
    """Entschlüsselt einen gespeicherten Secret-Wert. Werte ohne ``enc:``-
    Präfix werden als leer behandelt (kein Klartext-Fallback)."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return ""
    try:
        return _fernet_from_app().decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except Exception as exc:
        log.warning("Secret kann nicht entschlüsselt werden: %s", exc)
        return ""


def encrypt_with(secret_key: str, plain: str) -> str:
    """Wie ``encrypt``, aber mit explizitem SECRET_KEY (für CLI-Kontext ohne
    Flask-App, z. B. Scanner-Subprozess)."""
    if not plain:
        return ""
    return _ENC_PREFIX + _fernet_for_key(secret_key).encrypt(plain.encode()).decode()


def decrypt_with(secret_key: str, stored: str) -> str:
    """Wie ``decrypt``, aber mit explizitem SECRET_KEY."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return ""
    try:
        return _fernet_for_key(secret_key).decrypt(
            stored[len(_ENC_PREFIX):].encode()
        ).decode()
    except Exception as exc:
        log.warning("Secret kann nicht entschlüsselt werden: %s", exc)
        return ""
