"""Signed tokens für Magic-Links (Quick-Actions per E-Mail).

Tokens werden mit itsdangerous.URLSafeTimedSerializer signiert.
itsdangerous ist eine direkte Flask-Abhängigkeit und immer verfügbar.

Token-Payload: {"f": freigabe_id}
Salt: isoliert von anderen CSRF-/Session-Tokens.
TTL:  7 Tage (konfigurierbar via MAX_AGE_SECONDS).
"""
MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 Tage

_SALT = "idvault-quick-action-v1"

# Self-Service-Owner-Digest (Issue #315): eigener Salt, damit Tokens aus
# verschiedenen Kontexten nicht vertauscht werden können.
_SALT_SELF_SERVICE = "idvault-self-service-v1"


def _serializer(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret_key, salt=_SALT)


def _serializer_self_service(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret_key, salt=_SALT_SELF_SERVICE)


def make_freigabe_token(secret_key: str, freigabe_id: int) -> str:
    """Erstellt einen signierten Token für einen Freigabe-Schritt."""
    return _serializer(secret_key).dumps({"f": freigabe_id})


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
