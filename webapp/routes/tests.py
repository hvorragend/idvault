"""Testdokumentation-Blueprint – Fachliche Testfälle & Technischer Test"""
import os
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, send_from_directory, current_app)
from . import login_required, own_write_required, get_db, can_create
from werkzeug.utils import secure_filename
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import (
    get_fachliche_testfaelle, get_fachlicher_testfall,
    create_fachlicher_testfall, update_fachlicher_testfall, delete_fachlicher_testfall,
    get_technischer_test, save_technischer_test, delete_technischer_test,
)
from datetime import date as _date, datetime as _datetime

bp = Blueprint("tests", __name__, url_prefix="/tests")

_BEWERTUNGEN     = ["Offen", "Bestanden"]
_TECH_ERGEBNISSE = ["Offen", "Bestanden", "Entfällt"]

_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf", "xlsx", "xls",
                       "docx", "doc", "txt", "csv", "zip"}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in _ALLOWED_EXTENSIONS


def _save_test_upload(file):
    """Speichert eine Testnachweis-Datei. Gibt (dateiname, originalname) zurück."""
    if not file or not file.filename:
        return None, None
    if not _allowed_file(file.filename):
        return None, None
    original_name = file.filename
    safe_name = secure_filename(original_name)
    timestamp = _datetime.now().strftime("%Y%m%d_%H%M%S_")
    save_name = timestamp + safe_name
    folder = os.path.join(current_app.instance_path, "uploads", "tests")
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, save_name))
    return save_name, original_name


def _get_idv_or_404(db, idv_db_id):
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("IDV nicht gefunden.", "error")
    return idv


# ── Nachweis-Datei herunterladen ──────────────────────────────────────────────

@bp.route("/nachweis/<path:filename>")
@login_required
def nachweis_download(filename):
    folder = os.path.join(current_app.instance_path, "uploads", "tests")
    return send_from_directory(folder, filename, as_attachment=True)


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
            # Datei-Upload verarbeiten
            upload_file = request.files.get("nachweis_datei")
            pfad, name = _save_test_upload(upload_file)
            if upload_file and upload_file.filename and not pfad:
                flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
            data["nachweis_datei_pfad"] = pfad
            data["nachweis_datei_name"] = name

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
            # Datei-Upload: neue Datei oder vorhandene behalten
            upload_file = request.files.get("nachweis_datei")
            pfad, name = _save_test_upload(upload_file)
            if upload_file and upload_file.filename and not pfad:
                flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
            data["nachweis_datei_pfad"] = pfad or testfall["nachweis_datei_pfad"]
            data["nachweis_datei_name"] = name or testfall["nachweis_datei_name"]

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

        # Datei-Upload: neue Datei oder vorhandene behalten
        upload_file = request.files.get("nachweis_datei")
        pfad, name = _save_test_upload(upload_file)
        if upload_file and upload_file.filename and not pfad:
            flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
        data["nachweis_datei_pfad"] = pfad or (tech_test["nachweis_datei_pfad"] if tech_test else None)
        data["nachweis_datei_name"] = name or (tech_test["nachweis_datei_name"] if tech_test else None)

        save_technischer_test(db, idv_db_id, data)
        flash("Technischer Test gespeichert.", "success")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id,
                                _anchor="testdokumentation"))

    return render_template("tests/technisch_form.html",
                           idv=idv, tech_test=tech_test,
                           ergebnisse=_TECH_ERGEBNISSE,
                           today=_date.today().isoformat())


# ── Technischer Test: Löschen ─────────────────────────────────────────────

@bp.route("/idv/<int:idv_db_id>/technisch/loeschen", methods=["POST"])
@own_write_required
def delete_technischer_test_route(idv_db_id):
    db  = get_db()
    idv = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("idv.list_idv"))
    delete_technischer_test(db, idv_db_id)
    flash("Technischer Test gelöscht.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id,
                            _anchor="testdokumentation"))


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
