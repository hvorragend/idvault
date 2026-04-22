"""Admin-Sub-Modul: Rate-Limits und Login-Log."""
from flask import render_template, request, redirect, url_for, flash

from .. import admin_required, get_db
from . import bp


@bp.route("/rate-limits", methods=["GET", "POST"])
@admin_required
def rate_limits():
    """Admin-UI für Login-/Upload-Rate-Limits (flask_limiter-Syntax)."""
    from ... import app_settings as _aps
    db = get_db()
    if request.method == "POST":
        login_val  = request.form.get("login_rate_limit",  "").strip() or _aps.DEFAULTS["login_rate_limit"]
        upload_val = request.form.get("upload_rate_limit", "").strip() or _aps.DEFAULTS["upload_rate_limit"]
        _aps.set_setting(db, "login_rate_limit",  login_val)
        _aps.set_setting(db, "upload_rate_limit", upload_val)
        flash("Rate-Limits gespeichert.", "success")
        return redirect(url_for("admin.rate_limits"))
    return render_template(
        "admin/rate_limits.html",
        login_rate_limit  = _aps.get_login_rate_limit(db),
        upload_rate_limit = _aps.get_upload_rate_limit(db),
        defaults          = _aps.DEFAULTS,
    )


@bp.route("/login-log")
@admin_required
def login_log():
    """Leitet auf den einheitlichen Log-Viewer weiter (Login-Log als Quelle)."""
    return redirect(url_for("admin.scanner_log", which="login"))
