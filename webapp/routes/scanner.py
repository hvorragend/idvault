"""Scanner-Funde Blueprint"""
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from . import login_required, write_access_required, get_db

bp = Blueprint("scanner", __name__, url_prefix="/scanner")

_EXT_TO_TYP = {
    ".xlsx": "Excel-Tabelle",
    ".xlsm": "Excel-Makro",
    ".xlsb": "Excel-Makro",
    ".xls":  "Excel-Tabelle",
    ".xltm": "Excel-Makro",
    ".xltx": "Excel-Tabelle",
    ".accdb": "Access-Datenbank",
    ".mdb":   "Access-Datenbank",
    ".accde": "Access-Datenbank",
    ".accdr": "Access-Datenbank",
    ".py":    "Python-Skript",
    ".r":     "Sonstige",
    ".rmd":   "Sonstige",
    ".sql":   "SQL-Skript",
    ".pbix":  "Power-BI-Bericht",
    ".pbit":  "Power-BI-Bericht",
}


def _idv_typ_vorschlag(extension: str, has_macros: int) -> str:
    ext = (extension or "").lower()
    if ext in (".xlsx", ".xls", ".xltx") and has_macros:
        return "Excel-Makro"
    return _EXT_TO_TYP.get(ext, "unklassifiziert")


@bp.route("/funde")
@login_required
def list_funde():
    db   = get_db()
    filt = request.args.get("filter", "")

    if filt == "archiv":
        where = "WHERE f.status = 'archiviert'"
    else:
        where = "WHERE f.status = 'active'"
        if filt == "ohne_idv":
            where += " AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
        elif filt == "mit_idv":
            where += " AND EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
        elif filt == "makros":
            where += " AND f.has_macros = 1"
        elif filt == "blattschutz":
            where += " AND f.has_sheet_protection = 1"

    dateien = db.execute(f"""
        SELECT f.*,
               r.idv_id       AS reg_idv_id,
               r.bezeichnung  AS reg_bezeichnung,
               r.id           AS reg_db_id
        FROM idv_files f
        LEFT JOIN idv_register r ON r.file_id = f.id
        {where}
        ORDER BY f.modified_at DESC
        LIMIT 500
    """).fetchall()

    gesamt       = db.execute("SELECT COUNT(*) FROM idv_files WHERE status='active'").fetchone()[0]
    ohne_idv     = db.execute("""
        SELECT COUNT(*) FROM idv_files f WHERE f.status='active'
        AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
    """).fetchone()[0]
    mit_makro    = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND has_macros=1"
    ).fetchone()[0]
    mit_schutz   = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND has_sheet_protection=1"
    ).fetchone()[0]
    archiviert   = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='archiviert'"
    ).fetchone()[0]

    return render_template("scanner/list.html",
        dateien=dateien, filt=filt,
        gesamt=gesamt, ohne_idv=ohne_idv, mit_makro=mit_makro,
        mit_schutz=mit_schutz, archiviert=archiviert,
        idv_typ_vorschlag=_idv_typ_vorschlag,
    )


@bp.route("/funde/<int:file_id>/benachrichtigen", methods=["POST"])
@write_access_required
def notify_file(file_id):
    """Sendet manuell eine E-Mail-Benachrichtigung für einen Scannerfund."""
    db   = get_db()
    file = db.execute("SELECT * FROM idv_files WHERE id=?", (file_id,)).fetchone()
    if not file:
        flash("Datei nicht gefunden.", "error")
        return redirect(url_for("scanner.list_funde"))

    # Koordinatoren und Admins aus Personen-Tabelle als Empfänger
    recipients = [
        r["email"] for r in db.execute("""
            SELECT email FROM persons
            WHERE aktiv=1 AND email IS NOT NULL
              AND rolle IN ('IDV-Koordinator','IDV-Administrator')
        """).fetchall()
        if r["email"]
    ]

    if not recipients:
        flash("Keine E-Mail-Empfänger konfiguriert (Koordinatoren/Administratoren ohne E-Mail-Adresse).", "warning")
        return redirect(url_for("scanner.list_funde"))

    try:
        from ..email_service import notify_new_scanner_file
        ok = notify_new_scanner_file(db, file, recipients)
        if ok:
            flash(f"Benachrichtigung gesendet an: {', '.join(recipients)}", "success")
        else:
            flash("E-Mail konnte nicht gesendet werden – SMTP-Einstellungen prüfen.", "warning")
    except Exception as exc:
        flash(f"Fehler beim E-Mail-Versand: {exc}", "error")

    return redirect(url_for("scanner.list_funde"))
