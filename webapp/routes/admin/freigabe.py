"""Admin-Sub-Modul: Konfiguration des Freigabe-Workflows.

Aktuell nur die verschlankte Patch-Variante (#320): der Admin legt fest,
welche der fünf Standard-Schritte bei einer als ``patch`` eingestuften
Version tatsächlich durchlaufen werden. Der Default bleibt konservativ
(Technischer Test + Fachliche Abnahme + Archivierung).
"""

from __future__ import annotations

import json

from flask import render_template, request, redirect, url_for, flash

from .. import admin_required, get_db
from . import bp


_ALL_SCHRITTE = [
    "Fachlicher Test",
    "Technischer Test",
    "Fachliche Abnahme",
    "Technische Abnahme",
    "Archivierung Originaldatei",
]

_DEFAULT = ["Technischer Test", "Fachliche Abnahme", "Archivierung Originaldatei"]


@bp.route("/freigabe-patch", methods=["GET", "POST"])
@admin_required
def freigabe_patch():
    """Admin-UI: aktive Schritte für den Patch-Workflow festlegen."""
    from ... import app_settings as _aps
    db = get_db()

    if request.method == "POST":
        selected = [s for s in request.form.getlist("schritte") if s in _ALL_SCHRITTE]
        if not selected:
            flash(
                "Mindestens ein Schritt muss aktiv bleiben – der Default wurde "
                "wiederhergestellt.",
                "warning",
            )
            selected = list(_DEFAULT)
        _aps.set_json(db, "freigabe_patch_schritte", selected)
        flash("Patch-Workflow-Konfiguration gespeichert.", "success")
        return redirect(url_for("admin.freigabe_patch"))

    aktive = _aps.get_json(db, "freigabe_patch_schritte", _DEFAULT)
    if not isinstance(aktive, list):
        aktive = list(_DEFAULT)
    aktive_set = {s for s in aktive if s in _ALL_SCHRITTE}
    return render_template(
        "admin/freigabe_patch.html",
        alle_schritte=_ALL_SCHRITTE,
        aktive=aktive_set,
        default=_DEFAULT,
    )
