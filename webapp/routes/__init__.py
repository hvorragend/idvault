"""
idvault – Route-Blueprints
==========================
Dashboard, IDV, Prüfungen, Maßnahmen, Auth, Admin.
"""

from flask import session, redirect, url_for, flash, abort
from functools import wraps
from ..db_flask import get_db  # noqa: re-export für alle Route-Module

# ---------------------------------------------------------------------------
# Rollen-Konstanten
# ---------------------------------------------------------------------------
ROLE_ADMIN       = "IDV-Administrator"
ROLE_KOORDINATOR = "IDV-Koordinator"
ROLE_REVISION    = "Revision"
ROLE_IT_SEC      = "IT-Sicherheit"
ROLE_FACHVERW    = "Fachverantwortlicher"

# Rollen mit vollständigem Schreibzugriff auf alle IDVs
_FULL_ACCESS_ROLES = {ROLE_ADMIN, ROLE_KOORDINATOR}
# Rollen mit Lesezugriff auf alle IDVs. Wer hier nicht aufgeführt ist
# (Fachverantwortliche, eingeloggte AD-User ohne Rolle) sieht nur IDVs,
# an denen die Person als Fachverantwortlicher, Entwickler, Koordinator
# oder Stellvertreter eingetragen ist (Row-Level-Filter in
# webapp/security.py::user_can_read_idv sowie im IDV-Listen-SQL).
_READ_ALL_ROLES    = {ROLE_ADMIN, ROLE_KOORDINATOR, ROLE_REVISION, ROLE_IT_SEC}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Nur IDV-Administrator darf diese Route aufrufen."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        if session.get("user_role") != ROLE_ADMIN:
            flash("Zugriff verweigert – nur für Administratoren.", "error")
            abort(403)
        return f(*args, **kwargs)
    return decorated


def write_access_required(f):
    """Koordinatoren und Admins dürfen schreiben; Revision/Fachverantwortliche nicht."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        role = session.get("user_role", "")
        if role not in _FULL_ACCESS_ROLES:
            flash("Zugriff verweigert – keine Schreibberechtigung.", "error")
            abort(403)
        return f(*args, **kwargs)
    return decorated


def current_user_role() -> str:
    return session.get("user_role", "")


def current_person_id():
    return session.get("person_id")


def own_write_required(f):
    """Wer IDVs anlegen/eigene bearbeiten darf:

    * Admin/Koordinator (volle Schreibrechte) — auch ohne Person-Binding,
      damit technische Service-Accounts aus ``IDV_LOCAL_USERS`` nicht aus
      dem Erfass-Pfad fallen.
    * Sonstige eingeloggte User nur mit ``person_id`` (für Ownership).

    Edit/Abschluss eines bestehenden IDV ist zusätzlich durch
    ``ensure_can_write_idv()`` (Ownership-Check) abgesichert."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        if not can_create():
            flash("Zugriff verweigert – Ihrem Konto ist keine Person zugeordnet.", "error")
            abort(403)
        return f(*args, **kwargs)
    return decorated


def can_write() -> bool:
    """True wenn der eingeloggte Benutzer Schreibrechte auf alle IDVs hat."""
    return current_user_role() in _FULL_ACCESS_ROLES


def can_create() -> bool:
    """True wenn der Benutzer eigene IDVs anlegen darf.

    Admin/Koordinator dürfen immer (auch ohne Person-Binding); sonst ist
    ein Person-Binding nötig, damit der Ersteller als Entwickler
    eingetragen werden kann und nach dem Speichern den Schreibzugriff
    behält."""
    if not session.get("user_id"):
        return False
    if can_write():
        return True
    return bool(session.get("person_id"))


def can_read_all() -> bool:
    """True wenn der Benutzer alle IDVs sehen darf (nicht nur eigene)."""
    return current_user_role() in _READ_ALL_ROLES
