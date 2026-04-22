"""Sicherheits-Routen: Login-Log."""
from flask import redirect, url_for

from . import bp
from .. import admin_required


@bp.route("/login-log")
@admin_required
def login_log():
    """Leitet auf den einheitlichen Log-Viewer weiter (Login-Log als Quelle)."""
    return redirect(url_for("admin.scanner_log", which="login"))
