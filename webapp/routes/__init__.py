"""
idvault – Route-Blueprints
==========================
Dashboard, IDV, Prüfungen, Maßnahmen, Auth, Admin.
"""

from flask import session, redirect, url_for
from functools import wraps
from ..db_flask import get_db  # noqa: re-export für alle Route-Module

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


