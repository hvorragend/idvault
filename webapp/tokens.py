"""Signed tokens für Magic-Links (Quick-Actions per E-Mail).

Tokens werden mit itsdangerous.URLSafeTimedSerializer signiert.
itsdangerous ist eine direkte Flask-Abhängigkeit und immer verfügbar.

Token-Payload: {"f": freigabe_id}
Salt: isoliert von anderen CSRF-/Session-Tokens.
TTL:  7 Tage (konfigurierbar via MAX_AGE_SECONDS).
"""
MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 Tage

_SALT = "idvault-quick-action-v1"


def _serializer(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret_key, salt=_SALT)


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
