"""
Flask-SQLite Datenbankschicht für idvault.
Wrapped die db.py Funktionen für den Flask-Request-Context.
"""

import sqlite3
from flask import current_app, g
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import init_register_db, get_dashboard_stats, search_idv  # noqa


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = init_register_db(current_app.config["DATABASE"])
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app_db(app):
    app.teardown_appcontext(close_db)

    with app.app_context():
        init_register_db(app.config["DATABASE"])
