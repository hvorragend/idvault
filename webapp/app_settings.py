"""
DB-basierte Anwendungseinstellungen (``app_settings``-Tabelle)
=============================================================
Zentrale Helfer, damit Admin-Routen, Scanner-Subprozesse und die Webapp
die gleichen Keys konsistent lesen/schreiben.

Konventionen für Keys (siehe schema.sql und db._seed_default_app_settings):
  * ``login_rate_limit``           – Flask-Limiter-Syntax
  * ``upload_rate_limit``          – Flask-Limiter-Syntax
  * ``allow_sidecar_updates``      – ``"1"`` / ``"0"``
  * ``path_mappings``              – JSON-Array (UNC→Laufwerksmapping)
  * ``scanner_config``             – JSON-Objekt
  * ``teams_config``               – JSON-Objekt (ohne client_secret)
  * ``teams_client_secret_enc``    – Fernet-verschlüsselter Client-Secret-Wert
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


# Default-Werte für seed + Fallback-Lesen. Single source of truth.
DEFAULTS = {
    "login_rate_limit":       "5 per minute;30 per hour",
    "upload_rate_limit":      "10 per minute;60 per hour",
    "allow_sidecar_updates":  "1",
    "suggestions_enabled":    "1",
    "path_mappings":          "[]",
    "scanner_config":         "{}",
    "teams_config":           "{}",
    "teams_client_secret_enc": "",
}


def get_setting(db, key: str, default: str | None = None) -> str | None:
    """Liest einen einzelnen Wert aus ``app_settings``. Fehlt der Eintrag,
    wird ``default`` (bzw. der in ``DEFAULTS`` hinterlegte Wert) zurückgegeben."""
    if default is None:
        default = DEFAULTS.get(key)
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
    except Exception as exc:
        log.warning("app_settings[%s] nicht lesbar: %s", key, exc)
        return default
    if row is None:
        return default
    value = row["value"]
    return value if value is not None else default


def set_setting(db, key: str, value: str) -> None:
    """Schreibt einen Wert nach ``app_settings`` (INSERT OR REPLACE).

    Writes werden ueber den globalen Writer-Thread serialisiert, damit die
    Web-App keine konkurrierenden BEGIN IMMEDIATE-Transaktionen gegen die
    SQLite-Datei faehrt. Der ``db``-Parameter bleibt als Teil der API
    erhalten, wird beim Write aber ignoriert (der Writer-Thread nutzt
    seine eigene Connection).
    """
    from .db_writer import get_writer
    from db_write_tx import write_tx

    val = value if value is not None else ""

    def _apply(c):
        with write_tx(c):
            c.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                (key, val),
            )

    get_writer().submit(_apply, wait=True)


def get_json(db, key: str, default: Any = None) -> Any:
    """Liest einen JSON-serialisierten Wert. Bei Parsing-Fehler → ``default``."""
    raw = get_setting(db, key)
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        log.warning("app_settings[%s] kein gültiges JSON: %s", key, exc)
        return default


def set_json(db, key: str, value: Any) -> None:
    """Schreibt einen Wert als JSON."""
    set_setting(db, key, json.dumps(value, ensure_ascii=False))


def get_bool(db, key: str, default: bool = False) -> bool:
    raw = get_setting(db, key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def set_bool(db, key: str, value: bool) -> None:
    set_setting(db, key, "1" if value else "0")


# ---------------------------------------------------------------------------
# Getter/Setter für strukturierte Werte, die aus mehreren Stellen gelesen werden
# ---------------------------------------------------------------------------

def get_path_mappings(db) -> list:
    data = get_json(db, "path_mappings", [])
    return data if isinstance(data, list) else []


def set_path_mappings(db, mappings: list) -> None:
    set_json(db, "path_mappings", list(mappings or []))


def get_scanner_config(db) -> dict:
    data = get_json(db, "scanner_config", {})
    return data if isinstance(data, dict) else {}


def set_scanner_config(db, cfg: dict) -> None:
    set_json(db, "scanner_config", dict(cfg or {}))


def get_teams_config(db) -> dict:
    data = get_json(db, "teams_config", {})
    return data if isinstance(data, dict) else {}


def set_teams_config(db, cfg: dict) -> None:
    """Speichert Teams-Config ohne client_secret (das liegt separat
    Fernet-verschlüsselt unter ``teams_client_secret_enc``)."""
    clean = {k: v for k, v in (cfg or {}).items() if k != "client_secret"}
    set_json(db, "teams_config", clean)


def get_teams_client_secret(db) -> str:
    from . import secrets as idv_secrets
    enc = get_setting(db, "teams_client_secret_enc", "") or ""
    return idv_secrets.decrypt(enc) if enc else ""


def set_teams_client_secret(db, plain: str) -> None:
    from . import secrets as idv_secrets
    set_setting(
        db, "teams_client_secret_enc",
        idv_secrets.encrypt(plain) if plain else "",
    )


def get_login_rate_limit(db) -> str:
    return get_setting(db, "login_rate_limit") or DEFAULTS["login_rate_limit"]


def get_upload_rate_limit(db) -> str:
    return get_setting(db, "upload_rate_limit") or DEFAULTS["upload_rate_limit"]


def allow_sidecar_updates(db) -> bool:
    return get_bool(db, "allow_sidecar_updates", True)
