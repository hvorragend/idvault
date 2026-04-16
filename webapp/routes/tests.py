"""Testdokumentation-Blueprint – Fachliche Testfälle & Technischer Test"""
import os
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, abort, send_from_directory, current_app)
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
from ..security import validate_upload_mime, ensure_can_read_idv, ensure_can_write_idv

bp = Blueprint("tests", __name__, url_prefix="/tests")

_BEWERTUNGEN     = ["Offen", "Erledigt"]
_TECH_ERGEBNISSE = ["Offen", "Erledigt"]

_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf", "xlsx", "xls",
                       "docx", "doc", "txt", "csv", "zip"}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in _ALLOWED_EXTENSIONS


def _save_test_upload(file):
    """Speichert eine Testnachweis-Datei. Gibt (dateiname, originalname) zurück.

    VULN-I: Prüft zusätzlich die Magic-Bytes der Datei gegen die deklarierte
    Extension, um polyglot-Uploads (``evil.svg`` als ``.png``) auszuschließen.
    """
    if not file or not file.filename:
        return None, None
    if not _allowed_file(file.filename):
        return None, None
    ext = file.filename.rsplit(".", 1)[1].lower()
    if not validate_upload_mime(file.stream, ext):
        current_app.logger.warning(
            "Test-Upload abgelehnt: Magic-Bytes passen nicht zur Extension '%s' (Datei: %s)",
            ext, file.filename,
        )
        return None, None
    original_name = file.filename
    safe_name = secure_filename(original_name) or f"upload.{ext}"
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


def _reset_freigabe_schritt(db, idv_db_id: int, schritt: str) -> None:
    """Setzt einen Freigabe-Schritt auf 'Ausstehend' zurück, wenn der Test gelöscht wird."""
    db.execute(
        "UPDATE idv_freigaben SET status='Ausstehend', durchgefuehrt_von_id=NULL, "
        "durchgefuehrt_am=NULL WHERE idv_id=? AND schritt=? AND status='Erledigt'",
        (idv_db_id, schritt)
    )
    db.commit()


def _phase1_schritt_aktiv(db, idv_db_id: int, schritt: str) -> bool:
    """True wenn für die IDV ein Phase-1-Freigabe-Schritt mit dem gegebenen
    Namen existiert. Wird verwendet, um den Zugriff auf die Test-Formulare
    nur bei aktivem Verfahren zu erlauben."""
    return db.execute(
        "SELECT 1 FROM idv_freigaben WHERE idv_id=? AND schritt=? LIMIT 1",
        (idv_db_id, schritt)
    ).fetchone() is not None


def _require_phase1_schritt(db, idv_db_id: int, schritt: str):
    """Redirect+Flash wenn Phase-1-Schritt nicht aktiv ist. Gibt None zurück
    wenn alles ok, sonst ein Redirect-Response."""
    if not _phase1_schritt_aktiv(db, idv_db_id, schritt):
        flash(
            f"'{schritt}' ist nicht aktiv. Bitte zuerst das Freigabeverfahren starten "
            f"oder den Schritt wieder anlegen.",
            "warning"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))
    return None


# ── Nachweis-Datei herunterladen (VULN-D) ────────────────────────────────────

@bp.route("/nachweis/fachlich/<int:testfall_id>")
@login_required
def nachweis_download_fachlich(testfall_id):
    """Liefert den Nachweis eines fachlichen Testfalls – an Ownership gebunden."""
    db  = get_db()
    row = db.execute(
        """SELECT idv_id             AS idv_db_id,
                  nachweis_datei_pfad AS pfad,
                  nachweis_datei_name AS name
             FROM fachliche_testfaelle WHERE id = ?""",
        (testfall_id,),
    ).fetchone()
    return _serve_test_nachweis(db, row)


@bp.route("/nachweis/technisch/<int:idv_db_id>")
@login_required
def nachweis_download_technisch(idv_db_id):
    """Liefert den Nachweis des technischen Tests – an Ownership gebunden."""
    db  = get_db()
    row = db.execute(
        """SELECT idv_id             AS idv_db_id,
                  nachweis_datei_pfad AS pfad,
                  nachweis_datei_name AS name
             FROM technischer_test WHERE idv_id = ?""",
        (idv_db_id,),
    ).fetchone()
    return _serve_test_nachweis(db, row)


def _serve_test_nachweis(db, row):
    if not row or not row["pfad"]:
        abort(404)
    ensure_can_read_idv(db, row["idv_db_id"])
    if os.sep in row["pfad"] or "/" in row["pfad"] or "\\" in row["pfad"] \
            or row["pfad"].startswith("."):
        abort(404)
    folder = os.path.join(current_app.instance_path, "uploads", "tests")
    return send_from_directory(
        folder, row["pfad"],
        as_attachment=True,
        download_name=row["name"] or row["pfad"],
    )


# ── Fachlicher Testfall: Neu ───────────────────────────────────────────────

@bp.route("/idv/<int:idv_db_id>/fachlich/neu", methods=["GET", "POST"])
@own_write_required
def new_fachlicher_testfall(idv_db_id):
    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("idv.list_idv"))

    # Zugriff nur, wenn der Phase-1-Schritt aktiv ist
    gate = _require_phase1_schritt(db, idv_db_id, "Fachlicher Test")
    if gate is not None:
        return gate

    # Optionaler Freigabe-Kontext
    try:
        freigabe_id = int(request.args.get("freigabe_id") or request.form.get("freigabe_id") or 0) or None
    except (ValueError, TypeError):
        freigabe_id = None

    # Nur ein fachlicher Test pro IDV – wenn schon vorhanden, direkt zum Bearbeiten
    existing = get_fachliche_testfaelle(db, idv_db_id)
    if existing and request.method == "GET":
        kwargs = {"testfall_id": existing[0]["id"]}
        if freigabe_id:
            kwargs["freigabe_id"] = freigabe_id
        return redirect(url_for("tests.edit_fachlicher_testfall", **kwargs))

    if request.method == "POST":
        data = _fachlich_form_to_dict(request.form)
        if not data["beschreibung"]:
            flash("Testbeschreibung ist ein Pflichtfeld.", "error")
        else:
            upload_file = request.files.get("nachweis_datei")
            pfad, name = _save_test_upload(upload_file)
            if upload_file and upload_file.filename and not pfad:
                flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
            data["nachweis_datei_pfad"] = pfad
            data["nachweis_datei_name"] = name

            create_fachlicher_testfall(db, idv_db_id, data)

            if data["bewertung"] == "Erledigt":
                if freigabe_id:
                    from . import current_person_id
                    from .freigaben import complete_freigabe_schritt
                    complete_freigabe_schritt(db, freigabe_id, current_person_id())
                    flash("Test gespeichert und Freigabe-Schritt abgeschlossen.", "success")
                else:
                    flash("Test gespeichert.", "success")
            else:
                # Freigabe-Schritt zurücksetzen, falls er zuvor auf "Erledigt" gesetzt war
                _reset_freigabe_schritt(db, idv_db_id, "Fachlicher Test")
                flash("Test gespeichert.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    return render_template("tests/fachlich_form.html",
                           idv=idv, testfall=None,
                           freigabe_id=freigabe_id,
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

    ensure_can_write_idv(db, testfall["idv_id"])
    idv = _get_idv_or_404(db, testfall["idv_id"])
    if not idv:
        return redirect(url_for("idv.list_idv"))

    # Zugriff nur, wenn der Phase-1-Schritt aktiv ist
    gate = _require_phase1_schritt(db, idv["id"], "Fachlicher Test")
    if gate is not None:
        return gate

    try:
        freigabe_id = int(request.args.get("freigabe_id") or request.form.get("freigabe_id") or 0) or None
    except (ValueError, TypeError):
        freigabe_id = None

    if request.method == "POST":
        data = _fachlich_form_to_dict(request.form)
        if not data["beschreibung"]:
            flash("Testbeschreibung ist ein Pflichtfeld.", "error")
        else:
            upload_file = request.files.get("nachweis_datei")
            pfad, name = _save_test_upload(upload_file)
            if upload_file and upload_file.filename and not pfad:
                flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
            data["nachweis_datei_pfad"] = pfad or testfall["nachweis_datei_pfad"]
            data["nachweis_datei_name"] = name or testfall["nachweis_datei_name"]

            update_fachlicher_testfall(db, testfall_id, data)

            if data["bewertung"] == "Erledigt":
                if freigabe_id:
                    from . import current_person_id
                    from .freigaben import complete_freigabe_schritt
                    complete_freigabe_schritt(db, freigabe_id, current_person_id())
                    flash("Test gespeichert und Freigabe-Schritt abgeschlossen.", "success")
                else:
                    flash("Test gespeichert.", "success")
            else:
                # Freigabe-Schritt zurücksetzen, falls er zuvor auf "Erledigt" gesetzt war
                _reset_freigabe_schritt(db, idv["id"], "Fachlicher Test")
                flash("Test gespeichert.", "success")
            return redirect(url_for("idv.detail_idv", idv_db_id=idv["id"]))

    return render_template("tests/fachlich_form.html",
                           idv=idv, testfall=testfall,
                           freigabe_id=freigabe_id,
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
    ensure_can_write_idv(db, idv_db_id)
    delete_fachlicher_testfall(db, testfall_id)
    # Freigabe-Schritt zurücksetzen, damit ein neuer Test angelegt werden kann
    _reset_freigabe_schritt(db, idv_db_id, "Fachlicher Test")
    flash("Test gelöscht.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ── Technischer Test: Anlegen / Bearbeiten ────────────────────────────────

@bp.route("/idv/<int:idv_db_id>/technisch", methods=["GET", "POST"])
@own_write_required
def edit_technischer_test(idv_db_id):
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv       = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("idv.list_idv"))

    # Zugriff nur, wenn der Phase-1-Schritt aktiv ist
    gate = _require_phase1_schritt(db, idv_db_id, "Technischer Test")
    if gate is not None:
        return gate

    tech_test = get_technischer_test(db, idv_db_id)

    try:
        freigabe_id = int(request.args.get("freigabe_id") or request.form.get("freigabe_id") or 0) or None
    except (ValueError, TypeError):
        freigabe_id = None

    if request.method == "POST":
        data = {
            "ergebnis":         request.form.get("ergebnis", "Offen"),
            "kurzbeschreibung": request.form.get("kurzbeschreibung", "").strip() or None,
            "pruefer":          request.form.get("pruefer", "").strip() or None,
            "pruefungsdatum":   request.form.get("pruefungsdatum") or None,
        }

        upload_file = request.files.get("nachweis_datei")
        pfad, name = _save_test_upload(upload_file)
        if upload_file and upload_file.filename and not pfad:
            flash("Ungültiges Dateiformat für Nachweis-Upload.", "warning")
        data["nachweis_datei_pfad"] = pfad or (tech_test["nachweis_datei_pfad"] if tech_test else None)
        data["nachweis_datei_name"] = name or (tech_test["nachweis_datei_name"] if tech_test else None)

        save_technischer_test(db, idv_db_id, data)

        if data["ergebnis"] == "Erledigt":
            if freigabe_id:
                from . import current_person_id
                from .freigaben import complete_freigabe_schritt
                complete_freigabe_schritt(db, freigabe_id, current_person_id())
                flash("Technischer Test gespeichert und Freigabe-Schritt abgeschlossen.", "success")
            else:
                flash("Technischer Test gespeichert.", "success")
        else:
            # Freigabe-Schritt zurücksetzen, falls er zuvor auf "Erledigt" gesetzt war
            _reset_freigabe_schritt(db, idv_db_id, "Technischer Test")
            flash("Technischer Test gespeichert.", "success")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    return render_template("tests/technisch_form.html",
                           idv=idv, tech_test=tech_test,
                           freigabe_id=freigabe_id,
                           ergebnisse=_TECH_ERGEBNISSE,
                           today=_date.today().isoformat())


# ── Technischer Test: Löschen ─────────────────────────────────────────────

@bp.route("/idv/<int:idv_db_id>/technisch/loeschen", methods=["POST"])
@own_write_required
def delete_technischer_test_route(idv_db_id):
    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("idv.list_idv"))
    delete_technischer_test(db, idv_db_id)
    # Freigabe-Schritt zurücksetzen, damit ein neuer Test angelegt werden kann
    _reset_freigabe_schritt(db, idv_db_id, "Technischer Test")
    flash("Technischer Test gelöscht.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


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
