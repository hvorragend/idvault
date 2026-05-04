"""Signed tokens für Magic-Links (Quick-Actions per E-Mail).

Tokens werden mit itsdangerous.URLSafeTimedSerializer signiert.
itsdangerous ist eine direkte Flask-Abhängigkeit und immer verfügbar.

Token-Payload: {"f": freigabe_id}
Salt: isoliert von anderen CSRF-/Session-Tokens.
TTL:  7 Tage (konfigurierbar via MAX_AGE_SECONDS).
"""
MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 Tage

_SALT = "idvscope-quick-action-v1"

# Self-Service-Owner-Digest (Issue #315): eigener Salt, damit Tokens aus
# verschiedenen Kontexten nicht vertauscht werden können.
_SALT_SELF_SERVICE = "idvscope-self-service-v1"

# Stille Freigabe (Issue #351): eigener Salt fuer den Sicht-Freigabe-Link
# an den Fachverantwortlichen / Fachbereichsleiter.
_SALT_SILENT_RELEASE = "idvscope-silent-release-v1"


def _serializer(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret_key, salt=_SALT)


def _serializer_self_service(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret_key, salt=_SALT_SELF_SERVICE)


def make_freigabe_token(secret_key: str, freigabe_id: int,
                        person_id: int | None = None) -> str:
    """Erstellt einen signierten Token für einen Freigabe-Schritt.

    ``person_id`` bindet den Token zusätzlich an eine bestimmte Person —
    erforderlich für die anonyme Quick-Action-Seite (Issue #352). Bleibt das
    Feld leer (Default), ist der Token nur als Login-Shortcut verwendbar.
    """
    payload: dict = {"f": int(freigabe_id)}
    if person_id is not None:
        payload["p"] = int(person_id)
    return _serializer(secret_key).dumps(payload)


def verify_freigabe_token(secret_key: str, token: str) -> dict | None:
    """Verifiziert Token und gibt Payload zurück, oder None bei Fehler/Ablauf."""
    try:
        from itsdangerous import BadData
        return _serializer(secret_key).loads(token, max_age=MAX_AGE_SECONDS)
    except Exception:
        return None


def make_self_service_token(secret_key: str, person_id: int, jti: str) -> str:
    """Signierter Magic-Link-Token für den Self-Service-Owner-Digest.

    Payload: ``{"p": person_id, "j": jti}``. Der jti ist ein serverseitig
    verfolgter One-Time-Identifier (siehe ``self_service_tokens``).
    """
    return _serializer_self_service(secret_key).dumps({"p": int(person_id), "j": jti})


def verify_self_service_token(secret_key: str, token: str) -> dict | None:
    """Verifiziert den Magic-Link-Token und gibt ``{"p": …, "j": …}`` zurück,
    oder None bei Fehler/Ablauf."""
    try:
        return _serializer_self_service(secret_key).loads(
            token, max_age=MAX_AGE_SECONDS
        )
    except Exception:
        return None


def _serializer_silent_release(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret_key, salt=_SALT_SILENT_RELEASE)


def make_silent_release_token(secret_key: str, idv_db_id: int,
                              person_id: int, jti: str) -> str:
    """Magic-Link fuer den Fachverantwortlichen zur Sicht-Freigabe (Issue #351).

    Payload: ``{"i": idv_db_id, "p": person_id, "j": jti}``. Der jti wird
    serverseitig in ``silent_release_tokens`` getrackt (#401) und nach der
    ersten erfolgreichen Einlösung als ``revoked_at`` markiert – damit
    läuft kein zweiter POST mehr durch, auch wenn der Link über einen
    Referer-Leak in fremde Hände gelangt.
    """
    return _serializer_silent_release(secret_key).dumps(
        {"i": int(idv_db_id), "p": int(person_id), "j": str(jti)}
    )


def verify_silent_release_token(secret_key: str, token: str) -> dict | None:
    """Verifiziert den Sicht-Freigabe-Token (Issue #351 + #401)."""
    try:
        return _serializer_silent_release(secret_key).loads(
            token, max_age=MAX_AGE_SECONDS
        )
    except Exception:
        return None
