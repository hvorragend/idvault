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
    get_prefilled_findings, save_prefilled_findings,
    get_matching_vorlagen, _idv_ist_wesentlich,
)
from db_write_tx import write_tx
from datetime import date as _date, datetime as _datetime
from ..db_writer import get_writer
from ..security import validate_upload_mime, ensure_can_read_idv, ensure_can_write_idv

bp = Blueprint("tests", __name__, url_prefix="/tests")

_BEWERTUNGEN     = ["Offen", "Erledigt"]
_TECH_ERGEBNISSE = ["Offen", "Erledigt"]

# Prüfzeugnis-Checks für die technische Abnahme (Issue #349).
# Jeder Eintrag beschreibt einen maschinell erfassten Prüfpunkt:
#   kind        – stabiler Schlüssel, landet in tests_prefilled_findings
#   label       – Anzeigename im Formular
#   describe    – Funktion dict(scanner-row) -> str, erzeugt die zur
#                 Anzeige kommende Zusammenfassung des Scanner-Befunds
# Die Reihenfolge in dieser Liste bestimmt auch die Reihenfolge im
# Formular (Zell-/Blattschutz, Makros, Formelanzahl, externe Verknüpfungen,
# SHA-256, Dateigröße/Sheets – vgl. Akzeptanzkriterien in #349).
PRUEFZEUGNIS_CHECKS: list[dict] = [
    {
        "kind":  "makros",
        "label": "Makros",
        "describe": lambda d: "ja (VBA-Makros vorhanden)" if d.get("has_macros") else "nein",
    },
    {
        "kind":  "externe_verknuepfungen",
        "label": "Externe Verknüpfungen",
        "describe": lambda d: "ja" if d.get("has_external_links") else "nein",
    },
    {
        "kind":  "blattschutz",
        "label": "Blattschutz",
        "describe": lambda d: (
            f"aktiv auf {d.get('protected_sheets_count') or 0} Blatt/Blättern"
            + (" (mit Passwort)" if d.get("sheet_protection_has_pw") else "")
            if d.get("has_sheet_protection") else "keiner"
        ),
    },
    {
        "kind":  "zellschutz",
        "label": "Zell-/Arbeitsmappenschutz",
        "describe": lambda d: "Arbeitsmappenschutz aktiv" if d.get("workbook_protected") else "nicht aktiv",
    },
    {
        "kind":  "formel_anzahl",
        "label": "Formelanzahl",
        "describe": lambda d: f"{d.get('formula_count') or 0} Formelzellen",
    },
    {
        "kind":  "sha256",
        "label": "SHA-256",
        "describe": lambda d: d.get("file_hash") or "–",
    },
    {
        "kind":  "dateigroesse_blaetter",
        "label": "Dateigröße / Tabellenblätter",
        "describe": lambda d: (
            f"{round((d.get('size_bytes') or 0) / 1024, 1)} KB · "
            f"{d.get('sheet_count') or 0} Tabellenblatt/Tabellenblätter"
        ),
    },
]
_PRUEFZEUGNIS_KINDS = {c["kind"] for c in PRUEFZEUGNIS_CHECKS}

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
        flash("Eigenentwicklung nicht gefunden.", "error")
    return idv


def _load_testfall_vorlagen(db, art: str, idv_typ: str | None = None,
                            idv_db_id: int | None = None) -> list:
    """Liefert aktive Testfall-Vorlagen für Art (fachlich/technisch).

    Vorlagen mit ``idv_typ IS NULL`` sind typ-unabhängig und werden immer
    mit aufgenommen. Bei gesetztem ``idv_typ`` werden zusätzlich die passenden
    typ-spezifischen Vorlagen zurückgegeben.

    Wenn ``idv_db_id`` übergeben wird, berücksichtigt das Ergebnis
    zusätzlich die Scope-Tabelle (Issue #350): nur Vorlagen, die zur
    OE und Klassifikation der IDV passen, werden zurückgegeben.
    Verpflichtende Vorlagen tragen ``mandatory=1`` und werden zuerst
    sortiert.
    """
    try:
        if idv_db_id is not None:
            row = db.execute(
                "SELECT org_unit_id FROM idv_register WHERE id=?", (idv_db_id,),
            ).fetchone()
            oe_id = row["org_unit_id"] if row else None
            ist_wes = _idv_ist_wesentlich(db, idv_db_id)
            return get_matching_vorlagen(db, art, idv_typ, oe_id, ist_wes)
        # Fallback ohne IDV-Kontext: bisheriges Verhalten + mandatory=0
        if idv_typ:
            rows = db.execute(
                "SELECT id, titel, idv_typ, beschreibung, parametrisierung, "
                "       testdaten, erwartetes_ergebnis, 0 AS mandatory "
                "  FROM testfall_vorlagen "
                " WHERE aktiv=1 AND art=? AND (idv_typ IS NULL OR idv_typ=?) "
                " ORDER BY (idv_typ IS NULL) ASC, titel",
                (art, idv_typ),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, titel, idv_typ, beschreibung, parametrisierung, "
                "       testdaten, erwartetes_ergebnis, 0 AS mandatory "
                "  FROM testfall_vorlagen "
                " WHERE aktiv=1 AND art=? "
                " ORDER BY (idv_typ IS NULL) ASC, titel",
                (art,),
            ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _scanner_metadata_for_idv(db, idv_db_id: int) -> list:
    """Liefert Scanner-Metadaten aller mit der IDV verknüpften Dateien.

    Wird im Technischen Test als Prefill-Quelle angeboten: Makros, externe
    Verknüpfungen, Blattschutz, Formel-Anzahl etc. sind bereits vom Scan
    bekannt und müssen vom Prüfer nur bestätigt/kommentiert werden.
    """
    rows = db.execute("""
        SELECT f.id, f.file_name, f.full_path, f.extension, f.size_bytes,
               f.modified_at, f.file_owner, f.file_hash,
               f.has_macros, f.has_external_links,
               f.sheet_count, f.named_ranges_count, f.formula_count,
               f.has_sheet_protection, f.protected_sheets_count,
               f.sheet_protection_has_pw, f.workbook_protected,
               f.last_scan_run_id
          FROM idv_files f
         WHERE f.id = (SELECT file_id FROM idv_register WHERE id = ?)
        UNION
        SELECT f.id, f.file_name, f.full_path, f.extension, f.size_bytes,
               f.modified_at, f.file_owner, f.file_hash,
               f.has_macros, f.has_external_links,
               f.sheet_count, f.named_ranges_count, f.formula_count,
               f.has_sheet_protection, f.protected_sheets_count,
               f.sheet_protection_has_pw, f.workbook_protected,
               f.last_scan_run_id
          FROM idv_files f
          JOIN idv_file_links lnk ON lnk.file_id = f.id
         WHERE lnk.idv_db_id = ?
        ORDER BY file_name
    """, (idv_db_id, idv_db_id)).fetchall()
    return [dict(r) for r in rows]


def _build_pruefzeugnis(scanner_dateien: list, overrides: list) -> list:
    """Baut das Prüfzeugnis-View-Modell für die technische Abnahme.

    Eingabe:
      * ``scanner_dateien`` – dict-Rows aus ``_scanner_metadata_for_idv``
      * ``overrides`` – dict-Rows aus ``db.get_prefilled_findings`` (nur
        Einträge mit manual_override=1 landen dort)

    Rückgabe: Liste je Scanner-Datei mit den Prüfzeugnis-Checks in
    stabiler Reihenfolge. Jeder Check enthält die maschinelle
    Zusammenfassung, den aktuellen Override-Status (ticked / Kommentar /
    Prüferin bzw. Prüfer + Zeitstempel + Scan-Run-Referenz).
    """
    overrides_by_key = {(o["file_id"], o["check_kind"]): o for o in overrides or []}
    result = []
    for d in scanner_dateien or []:
        checks = []
        for spec in PRUEFZEUGNIS_CHECKS:
            key = (d["id"], spec["kind"])
            ov = overrides_by_key.get(key)
            checks.append({
                "kind":             spec["kind"],
                "label":            spec["label"],
                "machine_summary":  spec["describe"](d),
                "is_override":      bool(ov and ov.get("manual_override")),
                "manual_comment":   (ov or {}).get("manual_comment") or "",
                "confirmed_by":     (ov or {}).get("confirmed_by_name") or "",
                "recorded_at":      (ov or {}).get("recorded_at") or "",
                "source_scan_run_id": (ov or {}).get("source_scan_run_id")
                                       if ov else d.get("last_scan_run_id"),
            })
        result.append({
            "file_id":    d["id"],
            "file_name":  d["file_name"],
            "full_path":  d.get("full_path"),
            "last_scan_run_id": d.get("last_scan_run_id"),
            "checks":     checks,
        })
    return result


def _parse_pruefzeugnis_form(form, scanner_dateien: list) -> tuple[list, list]:
    """Parst die Formular-Werte des Prüfzeugnisses.

    Rückgabe:
      * ``overrides`` – Liste der zu persistierenden Abweichungen (für
        ``save_prefilled_findings``)
      * ``missing_comments`` – Override-Häkchen ohne Pflichtkommentar; der
        Aufrufer flasht eine Warnung und ignoriert diese Einträge

    Pro ``file × check_kind`` sind die erwarteten Felder
    ``override_<file_id>_<kind>`` (Checkbox, Wert ``1``) sowie
    ``override_comment_<file_id>_<kind>``. Das Feld
    ``machine_result_<file_id>_<kind>`` trägt die beim Rendern erzeugte
    Zusammenfassung mit in den Audit-Eintrag, damit späteres Nachscannen
    die Ausgangslage erkennen lässt.
    """
    overrides: list[dict] = []
    missing: list[str] = []
    files_by_id = {int(d["id"]): d for d in scanner_dateien or []}
    for file_id_s, fdata in files_by_id.items():
        for spec in PRUEFZEUGNIS_CHECKS:
            kind = spec["kind"]
            key = f"override_{file_id_s}_{kind}"
            if not form.get(key):
                continue
            comment = (form.get(f"override_comment_{file_id_s}_{kind}") or "").strip()
            machine_result = (
                form.get(f"machine_result_{file_id_s}_{kind}")
                or spec["describe"](fdata)
            )
            if not comment:
                missing.append(f"{fdata['file_name']} · {spec['label']}")
                continue
            overrides.append({
                "file_id":            file_id_s,
                "check_kind":         kind,
                "machine_result":     machine_result,
                "source_scan_run_id": fdata.get("last_scan_run_id"),
                "manual_comment":     comment,
            })
    return overrides, missing


def _log_pruefzeugnis_audit(
    conn,
    idv_db_id: int,
    person_id: int | None,
    bearbeiter_name: str,
    overrides: list,
    scanner_dateien: list,
    total_checks: int,
) -> None:
    """Schreibt Audit-Einträge zum Prüfzeugnis in ``idv_history``.

    - Eine Summen-Zeile (``pruefzeugnis_gespeichert``) hält fest, wie viele
      Checks maschinell bestätigt und wie viele manuell überschrieben
      wurden – daraus ergibt sich im Trail "Maschinell, ungeändert" für
      alle impliziten Einträge.
    - Pro Override zusätzlich eine Zeile (``pruefzeugnis_override``) mit
      Datei, Prüfpunkt, Maschinenergebnis und Begründung.
    """
    overridden = len(overrides)
    bestaetigt = max(0, total_checks - overridden)
    files_by_id = {int(d["id"]): d for d in scanner_dateien or []}
    kind_labels = {c["kind"]: c["label"] for c in PRUEFZEUGNIS_CHECKS}

    conn.execute(
        """
        INSERT INTO idv_history
            (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name)
        VALUES (?, 'pruefzeugnis_gespeichert', ?, ?, ?)
        """,
        (
            idv_db_id,
            f"Prüfzeugnis gespeichert: {bestaetigt} maschinell bestätigt, "
            f"{overridden} manuell überschrieben.",
            person_id,
            bearbeiter_name or None,
        ),
    )
    for ov in overrides:
        fdata = files_by_id.get(int(ov["file_id"])) or {}
        label = kind_labels.get(ov["check_kind"], ov["check_kind"])
        conn.execute(
            """
            INSERT INTO idv_history
                (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name)
            VALUES (?, 'pruefzeugnis_override', ?, ?, ?)
            """,
            (
                idv_db_id,
                f"{fdata.get('file_name') or '?'} · {label}: "
                f"maschinell „{ov.get('machine_result') or '—'}“, "
                f"manuell überschrieben – {ov['manual_comment']}",
                person_id,
                bearbeiter_name or None,
            ),
        )


def _reset_freigabe_schritt(db, idv_db_id: int, schritt: str) -> None:
    """Setzt einen Freigabe-Schritt auf 'Ausstehend' zurück, wenn der Test gelöscht wird."""
    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_freigaben SET status='Ausstehend', durchgefuehrt_von_id=NULL, "
                "durchgefuehrt_am=NULL WHERE idv_id=? AND schritt=? AND status='Erledigt'",
                (idv_db_id, schritt),
            )
    get_writer().submit(_do, wait=True)


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
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
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

@bp.route("/eigenentwicklung/<int:idv_db_id>/fachlich/neu", methods=["GET", "POST"])
@own_write_required
def new_fachlicher_testfall(idv_db_id):
    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("eigenentwicklung.list_idv"))

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

            get_writer().submit(
                lambda c: create_fachlicher_testfall(c, idv_db_id, data),
                wait=True,
            )

            if data["bewertung"] == "Erledigt":
                if freigabe_id:
                    from . import current_person_id
                    from .freigaben import complete_freigabe_schritt
                    abgeschlossen = complete_freigabe_schritt(
                        db, freigabe_id, current_person_id()
                    )
                    if abgeschlossen:
                        flash("Test gespeichert und Freigabe-Schritt abgeschlossen.", "success")
                    else:
                        flash(
                            "Test gespeichert. Der Freigabe-Schritt wurde NICHT "
                            "abgeschlossen: Sie sind entweder als Entwickler dieser "
                            "Eigenentwicklung eingetragen (Funktionstrennung) oder "
                            "nicht als Prüfer/Stellvertreter/Pool-Mitglied zugewiesen.",
                            "warning",
                        )
                else:
                    flash("Test gespeichert.", "success")
            else:
                # Freigabe-Schritt zurücksetzen, falls er zuvor auf "Erledigt" gesetzt war
                _reset_freigabe_schritt(db, idv_db_id, "Fachlicher Test")
                flash("Test gespeichert.", "success")
            return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    return render_template("tests/fachlich_form.html",
                           idv=idv, testfall=None,
                           freigabe_id=freigabe_id,
                           bewertungen=_BEWERTUNGEN,
                           vorlagen=_load_testfall_vorlagen(
                               db, "fachlich", idv["idv_typ"], idv["id"]),
                           today=_date.today().isoformat())


# ── Fachlicher Testfall: Bearbeiten ───────────────────────────────────────

@bp.route("/fachlich/<int:testfall_id>/bearbeiten", methods=["GET", "POST"])
@own_write_required
def edit_fachlicher_testfall(testfall_id):
    db       = get_db()
    testfall = get_fachlicher_testfall(db, testfall_id)
    if not testfall:
        flash("Testfall nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))

    ensure_can_write_idv(db, testfall["idv_id"])
    idv = _get_idv_or_404(db, testfall["idv_id"])
    if not idv:
        return redirect(url_for("eigenentwicklung.list_idv"))

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

            get_writer().submit(
                lambda c: update_fachlicher_testfall(c, testfall_id, data),
                wait=True,
            )

            if data["bewertung"] == "Erledigt":
                if freigabe_id:
                    from . import current_person_id
                    from .freigaben import complete_freigabe_schritt
                    abgeschlossen = complete_freigabe_schritt(
                        db, freigabe_id, current_person_id()
                    )
                    if abgeschlossen:
                        flash("Test gespeichert und Freigabe-Schritt abgeschlossen.", "success")
                    else:
                        flash(
                            "Test gespeichert. Der Freigabe-Schritt wurde NICHT "
                            "abgeschlossen: Sie sind entweder als Entwickler dieser "
                            "Eigenentwicklung eingetragen (Funktionstrennung) oder "
                            "nicht als Prüfer/Stellvertreter/Pool-Mitglied zugewiesen.",
                            "warning",
                        )
                else:
                    flash("Test gespeichert.", "success")
            else:
                # Freigabe-Schritt zurücksetzen, falls er zuvor auf "Erledigt" gesetzt war
                _reset_freigabe_schritt(db, idv["id"], "Fachlicher Test")
                flash("Test gespeichert.", "success")
            return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv["id"]))

    return render_template("tests/fachlich_form.html",
                           idv=idv, testfall=testfall,
                           freigabe_id=freigabe_id,
                           bewertungen=_BEWERTUNGEN,
                           vorlagen=_load_testfall_vorlagen(
                               db, "fachlich", idv["idv_typ"], idv["id"]),
                           today=_date.today().isoformat())


# ── Fachlicher Testfall: Löschen ──────────────────────────────────────────

@bp.route("/fachlich/<int:testfall_id>/loeschen", methods=["POST"])
@own_write_required
def delete_fachlicher_testfall_route(testfall_id):
    db       = get_db()
    testfall = get_fachlicher_testfall(db, testfall_id)
    if not testfall:
        flash("Testfall nicht gefunden.", "error")
        return redirect(url_for("eigenentwicklung.list_idv"))
    idv_db_id = testfall["idv_id"]
    ensure_can_write_idv(db, idv_db_id)
    get_writer().submit(
        lambda c: delete_fachlicher_testfall(c, testfall_id),
        wait=True,
    )
    # Freigabe-Schritt zurücksetzen, damit ein neuer Test angelegt werden kann
    _reset_freigabe_schritt(db, idv_db_id, "Fachlicher Test")
    flash("Test gelöscht.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ── Technischer Test: Anlegen / Bearbeiten ────────────────────────────────

@bp.route("/eigenentwicklung/<int:idv_db_id>/technisch", methods=["GET", "POST"])
@own_write_required
def edit_technischer_test(idv_db_id):
    db        = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv       = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("eigenentwicklung.list_idv"))

    # Zugriff nur, wenn der Phase-1-Schritt aktiv ist
    gate = _require_phase1_schritt(db, idv_db_id, "Technischer Test")
    if gate is not None:
        return gate

    tech_test       = get_technischer_test(db, idv_db_id)
    scanner_dateien = _scanner_metadata_for_idv(db, idv_db_id)

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

        # Prüfzeugnis (Issue #349): maschinelle Checks sind Default-
        # bestätigt; hier werden ausschliesslich die vom Prüfer aktiv
        # widerlegten Items eingesammelt. Override ohne Pflichtkommentar
        # wird verworfen und dem Prüfer als Warnung angezeigt.
        pruefzeugnis_overrides, missing_comments = _parse_pruefzeugnis_form(
            request.form, scanner_dateien,
        )
        for item in missing_comments:
            flash(
                f"Prüfzeugnis: „{item}“ wurde ohne Pflichtkommentar als "
                "Override markiert und deshalb als maschinell bestätigt "
                "übernommen. Bitte Begründung nachtragen.",
                "warning",
            )

        from . import current_person_id
        person_id = current_person_id()
        bearbeiter_name = (data.get("pruefer") or "").strip()
        total_checks = len(scanner_dateien) * len(PRUEFZEUGNIS_CHECKS)

        def _persist(c):
            save_technischer_test(c, idv_db_id, data)
            tt = get_technischer_test(c, idv_db_id)
            if tt is not None:
                save_prefilled_findings(
                    c, tt["id"], pruefzeugnis_overrides,
                    confirmed_by_id=person_id,
                )
                _log_pruefzeugnis_audit(
                    c, idv_db_id, person_id, bearbeiter_name,
                    pruefzeugnis_overrides, scanner_dateien, total_checks,
                )

        get_writer().submit(_persist, wait=True)

        if data["ergebnis"] == "Erledigt":
            if freigabe_id:
                from . import current_person_id
                from .freigaben import complete_freigabe_schritt
                abgeschlossen = complete_freigabe_schritt(
                    db, freigabe_id, current_person_id()
                )
                if abgeschlossen:
                    flash("Technischer Test gespeichert und Freigabe-Schritt abgeschlossen.", "success")
                else:
                    flash(
                        "Technischer Test gespeichert. Der Freigabe-Schritt wurde NICHT "
                        "abgeschlossen: Sie sind entweder als Entwickler dieser "
                        "Eigenentwicklung eingetragen (Funktionstrennung) oder "
                        "nicht als Prüfer/Stellvertreter/Pool-Mitglied zugewiesen.",
                        "warning",
                    )
            else:
                flash("Technischer Test gespeichert.", "success")
        else:
            # Freigabe-Schritt zurücksetzen, falls er zuvor auf "Erledigt" gesetzt war
            _reset_freigabe_schritt(db, idv_db_id, "Technischer Test")
            flash("Technischer Test gespeichert.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    prefilled_overrides = (
        get_prefilled_findings(db, tech_test["id"]) if tech_test else []
    )
    pruefzeugnis = _build_pruefzeugnis(scanner_dateien, prefilled_overrides)

    return render_template("tests/technisch_form.html",
                           idv=idv, tech_test=tech_test,
                           freigabe_id=freigabe_id,
                           ergebnisse=_TECH_ERGEBNISSE,
                           scanner_dateien=scanner_dateien,
                           pruefzeugnis=pruefzeugnis,
                           vorlagen=_load_testfall_vorlagen(
                               db, "technisch", idv["idv_typ"], idv["id"]),
                           today=_date.today().isoformat())


# ── Technischer Test: Löschen ─────────────────────────────────────────────

@bp.route("/eigenentwicklung/<int:idv_db_id>/technisch/loeschen", methods=["POST"])
@own_write_required
def delete_technischer_test_route(idv_db_id):
    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv = _get_idv_or_404(db, idv_db_id)
    if not idv:
        return redirect(url_for("eigenentwicklung.list_idv"))
    get_writer().submit(
        lambda c: delete_technischer_test(c, idv_db_id),
        wait=True,
    )
    # Freigabe-Schritt zurücksetzen, damit ein neuer Test angelegt werden kann
    _reset_freigabe_schritt(db, idv_db_id, "Technischer Test")
    flash("Technischer Test gelöscht.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


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
