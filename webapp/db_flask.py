"""
Flask-SQLite Datenbankschicht für idvault.
Wrapped die db.py Funktionen für den Flask-Request-Context.
"""

import atexit
import sqlite3
import sys
import os
from flask import current_app, g
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import init_register_db, get_connection, get_dashboard_stats, search_idv  # noqa
from webapp.db_writer import start_writer, stop_writer, get_writer  # noqa


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = get_connection(current_app.config["DATABASE"])
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app_db(app):
    app.teardown_appcontext(close_db)

    # Upload-Zielverzeichnisse schon beim App-Start anlegen, damit Betriebs-
    # und Sicherungsseite (NTFS-ACL, Backup, WORM) von Anfang an vorhanden
    # sind – auch bevor die erste Datei tatsächlich abgelegt wird.
    # - freigaben/  : Nachweis-Uploads (Phase 1/2)
    # - tests/      : Test-Nachweise (Fachlicher / Technischer Test)
    # - archiv/     : revisionssichere Archivierung der Originaldatei (Phase 3)
    for sub in ("freigaben", "tests", "archiv"):
        try:
            os.makedirs(os.path.join(app.instance_path, "uploads", sub),
                        exist_ok=True)
        except OSError as exc:
            app.logger.warning(
                "Upload-Verzeichnis '%s' konnte nicht angelegt werden: %s",
                sub, exc,
            )

    with app.app_context():
        init_register_db(app.config["DATABASE"])

    # Writer-Thread starten und atexit drainen. Idempotent — beliebig oft
    # aufrufbar. Wird im Service-Modus zusaetzlich in run.py:SvcStop
    # explizit angestossen, damit die Queue vor Prozessende leerlaeuft.
    start_writer(app.config["DATABASE"])
    atexit.register(stop_writer)
