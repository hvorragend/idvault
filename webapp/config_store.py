"""
Zentraler Zugriff auf config.json (Bootstrap-Konfiguration)
============================================================
Nach der Konsolidierung 2026-04 enthält ``config.json`` **nur noch**
Bootstrap-Belange – Werte, deren Fehlkonfiguration Start oder Login
blockieren würde:

    SECRET_KEY, PORT, DEBUG, IDV_HTTPS, IDV_SSL_CERT, IDV_SSL_KEY,
    IDV_SSL_AUTOGEN, IDV_DB_PATH, IDV_INSTANCE_PATH, IDV_LOCAL_USERS,
    IDV_SERVICE_NAME

Alle anderen Einstellungen (Scanner, Teams, SMTP, LDAP, Rate-Limits,
Pfad-Mappings, Sidecar-Update-Schalter) liegen in der SQLite-Datenbank
(``app_settings`` bzw. ``ldap_config``) und werden über die Web-UI
konfiguriert.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

_cache_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": None, "path": None}


def _project_root() -> str:
    """Projekt-Root wie in run.py (neben EXE / run.py)."""
    env = os.environ.get("IDV_PROJECT_ROOT")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # webapp/config_store.py → zwei Ebenen hoch
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_config_path() -> str:
    """Absoluter Pfad zur config.json neben run.py/EXE."""
    return os.path.join(_project_root(), "config.json")


def load_config_json(force: bool = False) -> dict:
    """Lädt config.json und cached das Ergebnis (mtime-invalidiert).

    Rückgabe ist ein frisches dict – der Aufrufer darf es mutieren ohne den
    Cache zu beschädigen. Fehlt die Datei oder ist sie kaputt, kommt ``{}``
    zurück.
    """
    path = get_config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    with _cache_lock:
        if (
            not force
            and _cache["data"] is not None
            and _cache["mtime"] == mtime
            and _cache["path"] == path
        ):
            return dict(_cache["data"])

        data: dict = {}
        if mtime is not None:
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    data = raw
            except (OSError, ValueError):
                data = {}

        _cache["data"] = data
        _cache["mtime"] = mtime
        _cache["path"] = path
        return dict(data)


def get_bootstrap(key: str, default: Any = None) -> Any:
    """Liest einen Top-Level-Bootstrap-Wert aus config.json.

    Ersetzt die früheren ``os.environ.get("IDV_…")``-Aufrufe. Der Rückgabewert
    behält den JSON-Typ (Bool bleibt Bool, Integer bleibt Integer) – Aufrufer
    können also auf ``cfg.get_bootstrap("IDV_HTTPS") == True`` direkt prüfen,
    ohne String-Coercion.
    """
    cfg = load_config_json()
    if key not in cfg:
        return default
    return cfg[key]


def get_bool(key: str, default: bool = False) -> bool:
    """Bootstrap-Wert als Bool (tolerant: akzeptiert 0/1, true/false, yes/no)."""
    value = get_bootstrap(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, default: int) -> int:
    value = get_bootstrap(key, None)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_str(key: str, default: str = "") -> str:
    value = get_bootstrap(key, None)
    if value is None:
        return default
    return str(value)


def write_top_level_key(key: str, value: Any) -> None:
    """Schreibt ``config.json[key] = value`` atomar.

    Andere Top-Level-Schlüssel bleiben unverändert. Wird in erster Linie
    beim initialen Schreiben eines SECRET_KEY in ``run.py`` genutzt.
    """
    path = get_config_path()
    full: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            full = raw
    except (OSError, ValueError):
        full = {}

    full[key] = value

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(full, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

    with _cache_lock:
        _cache["data"] = full
        _cache["mtime"] = os.path.getmtime(path)
        _cache["path"] = path


def invalidate_cache() -> None:
    """Erzwingt ein Neuladen beim nächsten Aufruf – hauptsächlich für Tests."""
    with _cache_lock:
        _cache["data"] = None
        _cache["mtime"] = None
        _cache["path"] = None
