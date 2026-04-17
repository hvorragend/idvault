"""
Zentraler Zugriff auf config.json
==================================
Liefert die Roh-Struktur der zusammengeführten ``config.json`` (neben ``run.py``
bzw. der EXE) zur Laufzeit. Wird von Admin-Routen genutzt, die Sektionen wie
``config.json["teams"]`` oder ``config.json["ldap"]`` lesen/schreiben bzw. als
Override über DB-Settings legen müssen.

Unterschied zu ``run.py``:
  * ``run.py`` liest die Datei **einmal** beim Start und verteilt Top-Level-
    Werte über ``os.environ``. Sub-Sektionen wie ``"scanner"``/``"teams"``
    werden bewusst NICHT in Env-Variablen überführt – für die brauchen wir
    hier Laufzeit-Zugriff.
  * Ergebnis wird per mtime-Check gecached, damit eine Hand-Edits an der
    Datei ohne Neustart wirken ("billiger Hot-Reload").
  * Schreiben erfolgt atomar (tmp + replace), damit konkurrierende Leser
    keine halbgeschriebene Datei sehen.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Optional

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


def get_section(name: str) -> Optional[dict]:
    """Liefert ``config.json[name]`` falls als Dict vorhanden, sonst ``None``."""
    cfg = load_config_json()
    section = cfg.get(name)
    return dict(section) if isinstance(section, dict) else None


def get_override(section: str, key: str, default: Any = None) -> Any:
    """Convenience: ``config.json[section][key]`` oder ``default``."""
    sec = get_section(section)
    if sec is None or key not in sec:
        return default
    return sec[key]


def write_section(name: str, data: dict) -> None:
    """Schreibt ``config.json[name] = data`` atomar und invalidiert den Cache.

    Andere Top-Level-Schlüssel bleiben unverändert. Wird von den Admin-Routen
    genutzt, die einzelne Bereiche (z.B. ``"teams"``, ``"scanner"``) speichern.
    """
    write_top_level_key(name, data)


def write_top_level_key(key: str, value: Any) -> None:
    """Schreibt ``config.json[key] = value`` atomar (value darf dict oder list sein).

    Verwendet von Admin-Routen für Top-Level-Schlüssel wie ``"path_mappings"``
    (Liste) und Sektionen wie ``"scanner"`` / ``"teams"`` (dict).
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
