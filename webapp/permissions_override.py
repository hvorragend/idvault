"""Sidecar-Override für Erfass-/Schreib-Berechtigungen.

Hintergrund (Issues #474 / #476 / #478): Der ``_SidecarFinder`` in
``run.py`` lädt ausschließlich freistehende ``.py``-Dateien aus dem
Updates-Ordner, **keine Package-``__init__.py``**. Der eigentliche Fix
in ``webapp/routes/__init__.py`` greift daher in Sidecar-aktualisierten
Deployments nicht — dort läuft weiterhin der gebundelte Pre-99096ed-
Decorator, der eine Rolle aus ``_OWN_WRITE_ROLES`` (Admin, Koordinator,
Fachverantwortlicher, IDV-Entwickler) verlangt und einen rollenlosen
LDAP-User mit 403 ablehnt — auch bei gesetztem ``person_id``.

Dieses Modul liegt freistehend im ``webapp``-Paket und wird daher vom
Sidecar geladen. Blueprint-Module importieren ``own_write_required`` /
``can_create`` / ``current_person_id`` von hier statt aus dem gebundelten
``webapp.routes``-Paket.

Verhalten gegenüber dem gebundelten Stand:
* Admin/Koordinator dürfen immer (volle Schreibrechte) — auch ohne
  Person-Binding.
* Sonstige eingeloggte User dürfen anlegen/eigene bearbeiten, sobald
  ihrem Konto eine ``persons``-Zeile zugeordnet ist (Ownership-Check
  in ``security.user_can_write_idv`` bleibt unverändert).
* ``current_person_id`` löst das Binding lazy aus ``persons`` (per
  ``user_id``/``ad_name``) auf und schreibt es in die Session zurück,
  falls der Login-Pfad das nicht ohnehin getan hat.

Sobald ein EXE-Neubau den Fix in ``webapp/routes/__init__.py``
ausliefert, ist dieses Override-Modul redundant und kann ohne
Verhaltensänderung entfernt werden.
"""

from __future__ import annotations

import logging
from functools import wraps

from flask import session, redirect, url_for, flash, abort, request

from .db_flask import get_db


_log = logging.getLogger(__name__)

_FULL_ACCESS_ROLES = {"IDV-Administrator", "IDV-Koordinator"}


def current_person_id():
    """``persons.id`` aus Session, sonst Lazy-Lookup per
    ``user_id``/``ad_name`` und Persistenz in der Session."""
    pid = session.get("person_id")
    if pid:
        return pid
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        db = get_db()
    except Exception:
        return None
    try:
        row = db.execute(
            "SELECT id FROM persons WHERE (user_id = ? OR ad_name = ?) AND aktiv = 1 "
            "ORDER BY (user_id = ?) DESC LIMIT 1",
            (uid, uid, uid),
        ).fetchone()
    except Exception as exc:
        _log.warning("current_person_id: persons-Lookup fehlgeschlagen für uid=%r: %s",
                     uid, exc)
        return None
    if row:
        session["person_id"] = row["id"]
        return row["id"]
    return None


def can_create() -> bool:
    """True wenn der eingeloggte Benutzer eigene IDVs anlegen darf.

    Admin/Koordinator dürfen immer; sonst ist ein Person-Binding nötig
    (notfalls lazy aus ``persons`` aufgelöst)."""
    if not session.get("user_id"):
        return False
    if session.get("user_role", "") in _FULL_ACCESS_ROLES:
        return True
    return bool(current_person_id())


def own_write_required(f):
    """Decorator: erlaubt das Anlegen/Bearbeiten eigener IDVs für jeden
    eingeloggten User mit Person-Binding sowie Admin/Koordinator. Edit
    bleibt zusätzlich durch den Ownership-Check in
    ``security.user_can_write_idv`` abgesichert."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth.login"))
        if not can_create():
            _log.warning(
                "own_write_required: 403 für user_id=%r role=%r person_id=%r path=%s",
                session.get("user_id"),
                session.get("user_role"),
                session.get("person_id"),
                request.path,
            )
            flash("Zugriff verweigert – Ihrem Konto ist keine Person zugeordnet.", "error")
            abort(403)
        return f(*args, **kwargs)
    return decorated
