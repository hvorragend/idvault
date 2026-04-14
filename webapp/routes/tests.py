"""Testdokumentation-Blueprint – Fachliche Testfälle & Technischer Test"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from . import login_required, own_write_required, get_db, can_create
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import (
    get_fachliche_testfaelle, get_fachlicher_testfall,
    create_fachlicher_testfall, update_fachlicher_testfall, delete_fachlicher_testfall,
    get_technischer_test, save_technischer_test,
)
from datetime import date as _date

bp = Blueprint("tests", __name__, url_prefix="/tests")

_BEWERTUNGEN     = ["Offen", "Bestanden", "Nicht bestanden"]
_TECH_ERGEBNISSE = ["Offen", "Bestanden", "Nicht bestanden", "Entfällt"]


def _get_idv_or_404(db, idv_db_id):
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("IDV nicht gefunden.", "error")
    return idv


# ── Fachlicher Testfall: Neu ───────────────────────────────────────────────

@bp.route("/idv/<int:idv_db_id>/fachlich/neu", methods=["GET", "POST"])
@own_write_required
def new_fachlicher_testfall(idv_db_id):
    db  = get_db()
    idv = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("idv.list_idv"))

    if request.method == "POST":
        data = _fachlich_form_to_dict(request.form)
        if not data["beschreibung"]:
            flash("Testfallbeschreibung ist ein Pflichtfeld.", "error")
        else:
            create_fachlicher_testfall(db, idv_db_id, data)
            flash("Testfall gespeichert.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id,
                                    _anchor="testdokumentation"))

    return render_template("tests/fachlich_form.html",
                           idv=idv, testfall=None,
                           bewertungen=_BEWERTUNGEN,
                           today=_date.today().isoformat())


# ── Fachlicher Testfall: Bearbeiten ───────────────────────────────────────

@bp.route("/fachlich/<int:testfall_id>/bearbeiten", methods=["GET", "POST"])
@own_write_required
def edit_fachlicher_testfall(testfall_id):
    db       = get_db()
    testfall = get_fachlicher_testfall(db, testfall_id)
    if not testfall:
        flash("Testfall nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))

    idv = _get_idv_or_404(db, testfall["idv_id"])
    if not idv:
        return redirect(url_for("idv.list_idv"))

    if request.method == "POST":
        data = _fachlich_form_to_dict(request.form)
        if not data["beschreibung"]:
            flash("Testfallbeschreibung ist ein Pflichtfeld.", "error")
        else:
            update_fachlicher_testfall(db, testfall_id, data)
            flash("Testfall aktualisiert.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv["id"],
                                    _anchor="testdokumentation"))

    return render_template("tests/fachlich_form.html",
                           idv=idv, testfall=testfall,
                           bewertungen=_BEWERTUNGEN,
                           today=_date.today().isoformat())


# ── Fachlicher Testfall: Löschen ──────────────────────────────────────────

@bp.route("/fachlich/<int:testfall_id>/loeschen", methods=["POST"])
@own_write_required
def delete_fachlicher_testfall_route(testfall_id):
    db       = get_db()
    testfall = get_fachlicher_testfall(db, testfall_id)
    if not testfall:
        flash("Testfall nicht gefunden.", "error")
        return redirect(url_for("idv.list_idv"))
    idv_db_id = testfall["idv_id"]
    delete_fachlicher_testfall(db, testfall_id)
    flash("Testfall gelöscht.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id,
                            _anchor="testdokumentation"))


# ── Technischer Test: Anlegen / Bearbeiten ────────────────────────────────

@bp.route("/idv/<int:idv_db_id>/technisch", methods=["GET", "POST"])
@own_write_required
def edit_technischer_test(idv_db_id):
    db        = get_db()
    idv       = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("idv.list_idv"))

    tech_test = get_technischer_test(db, idv_db_id)

    if request.method == "POST":
        data = {
            "ergebnis":         request.form.get("ergebnis", "Offen"),
            "kurzbeschreibung": request.form.get("kurzbeschreibung", "").strip() or None,
            "pruefer":          request.form.get("pruefer", "").strip() or None,
            "pruefungsdatum":   request.form.get("pruefungsdatum") or None,
        }
        save_technischer_test(db, idv_db_id, data)
        flash("Technischer Test gespeichert.", "success")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id,
                                _anchor="testdokumentation"))

    return render_template("tests/technisch_form.html",
                           idv=idv, tech_test=tech_test,
                           ergebnisse=_TECH_ERGEBNISSE,
                           today=_date.today().isoformat())


# ── Hilfsfunktion ─────────────────────────────────────────────────────────

def _fachlich_form_to_dict(form) -> dict:
    return {
        "beschreibung":        form.get("beschreibung", "").strip(),
        "parametrisierung":    form.get("parametrisierung", "").strip() or None,
        "testdaten":           form.get("testdaten", "").strip() or None,
        "erwartetes_ergebnis": form.get("erwartetes_ergebnis", "").strip() or None,
        "erzieltes_ergebnis":  form.get("erzieltes_ergebnis", "").strip() or None,
        "bewertung":           form.get("bewertung", "Offen"),
        "massnahmen":          form.get("massnahmen", "").strip() or None,
        "tester":              form.get("tester", "").strip() or None,
        "testdatum":           form.get("testdatum") or None,
    }
