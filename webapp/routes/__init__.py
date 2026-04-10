"""
idvault – Route-Blueprints
==========================
Dashboard, IDV, Prüfungen, Maßnahmen, Auth, Admin.
"""

# ─── Dashboard ────────────────────────────────────────────────────────────────

from flask import Blueprint, render_template, session, redirect, url_for, g
from functools import wraps

# ── Auth-Helper ───────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def get_db():
    from flask import current_app
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from db import init_register_db
    if "db" not in g:
        g.db = init_register_db(current_app.config["DATABASE"])
    return g.db
