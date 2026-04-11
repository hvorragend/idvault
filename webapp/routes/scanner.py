"""Scanner-Funde Blueprint"""
import json
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from . import login_required, write_access_required, own_write_required, get_db

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


def _scan_run_label(row) -> str:
    """Lesbare Kurzbezeichnung eines Scan-Laufs."""
    if not row:
        return "–"
    try:
        paths = json.loads(row["scan_paths"] or "[]")
    except Exception:
        paths = []
    datum = (row["started_at"] or "")[:16].replace("T", " ")
    pfad  = paths[0] if paths else "?"
    if len(paths) > 1:
        pfad += f" (+{len(paths)-1})"
    return f"#{row['id']} · {datum} · {pfad}"


@bp.route("/funde")
@login_required
def list_funde():
    db          = get_db()
    filt        = request.args.get("filter", "")
    share_root  = request.args.get("share_root", "").strip()
    scan_run_id = request.args.get("scan_run", "").strip()

    # ---------- WHERE-Bedingungen ----------
    where_parts = []
    params      = []

    if scan_run_id:
        # Für einen konkreten Scan-Lauf: alle Dateien zeigen, nicht nur aktive
        try:
            where_parts.append("f.last_scan_run_id = ?")
            params.append(int(scan_run_id))
        except ValueError:
            scan_run_id = ""
    elif filt == "archiv":
        where_parts.append("f.status = 'archiviert'")
    else:
        where_parts.append("f.status = 'active'")
        if filt == "ohne_idv":
            where_parts.append(
                "NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
            )
        elif filt == "mit_idv":
            where_parts.append(
                "EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
            )
        elif filt == "makros":
            where_parts.append("f.has_macros = 1")
        elif filt == "blattschutz":
            where_parts.append("f.has_sheet_protection = 1")
        elif filt == "ignoriert":
            where_parts.append("f.bearbeitungsstatus = 'Ignoriert'")
        elif filt == "zur_registrierung":
            where_parts.append("f.bearbeitungsstatus = 'Zur Registrierung'")

    if share_root:
        where_parts.append("f.share_root = ?")
        params.append(share_root)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    dateien = db.execute(f"""
        SELECT f.*,
               reg.idv_id       AS reg_idv_id,
               reg.bezeichnung  AS reg_bezeichnung,
               reg.id           AS reg_db_id,
               sr.id            AS sr_id,
               sr.started_at    AS sr_started_at,
               sr.scan_paths    AS sr_scan_paths
        FROM idv_files f
        LEFT JOIN idv_register reg ON reg.file_id = f.id
        LEFT JOIN scan_runs    sr  ON f.last_scan_run_id = sr.id
        {where_sql}
        ORDER BY f.last_seen_at DESC, f.modified_at DESC
        LIMIT 500
    """, params).fetchall()

    # ---------- Zählkarten ----------
    gesamt     = db.execute("SELECT COUNT(*) FROM idv_files WHERE status='active'").fetchone()[0]
    ohne_idv   = db.execute("""
        SELECT COUNT(*) FROM idv_files f WHERE f.status='active'
        AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
    """).fetchone()[0]
    mit_makro  = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND has_macros=1"
    ).fetchone()[0]
    mit_schutz = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND has_sheet_protection=1"
    ).fetchone()[0]
    archiviert = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='archiviert'"
    ).fetchone()[0]
    try:
        ignoriert = db.execute(
            "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Ignoriert'"
        ).fetchone()[0]
        zur_registrierung = db.execute(
            "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Zur Registrierung'"
        ).fetchone()[0]
    except Exception:
        ignoriert = 0
        zur_registrierung = 0

    # ---------- Filter-Optionen ----------
    # Distinct share_roots für Dropdown
    share_roots = [
        r["share_root"] for r in db.execute("""
            SELECT DISTINCT share_root FROM idv_files
            WHERE share_root IS NOT NULL AND status = 'active'
            ORDER BY share_root
        """).fetchall()
    ]
    # Letzte 30 Scan-Läufe für Dropdown
    try:
        scan_runs = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files, archived_files
            FROM scan_runs
            ORDER BY started_at DESC
            LIMIT 30
        """).fetchall()
    except Exception:
        scan_runs = []

    # Letzten Scan-Lauf ermitteln (für Info-Banner)
    letzter_scan = scan_runs[0] if scan_runs else None

    return render_template("scanner/list.html",
        dateien=dateien, filt=filt,
        gesamt=gesamt, ohne_idv=ohne_idv, mit_makro=mit_makro,
        mit_schutz=mit_schutz, archiviert=archiviert,
        ignoriert=ignoriert, zur_registrierung=zur_registrierung,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        share_roots=share_roots,
        share_root_filt=share_root,
        scan_runs=scan_runs,
        scan_run_id_filt=scan_run_id,
        letzter_scan=letzter_scan,
        scan_run_label=_scan_run_label,
    )


@bp.route("/laeufe")
@login_required
def scan_laeufe():
    """Übersicht aller Scan-Läufe."""
    db = get_db()
    try:
        laeufe = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files, moved_files,
                   restored_files, archived_files, errors
            FROM scan_runs
            ORDER BY started_at DESC
            LIMIT 100
        """).fetchall()
    except Exception:
        laeufe = []
    return render_template("scanner/laeufe.html", laeufe=laeufe,
                           scan_run_label=_scan_run_label)


@bp.route("/funde/bulk-aktion", methods=["POST"])
@own_write_required
def bulk_aktion():
    """Massenmarkierung von Scanner-Funden (ignorieren / zur Registrierung)."""
    db      = get_db()
    aktion  = request.form.get("aktion", "")
    raw_ids = request.form.getlist("file_ids")

    if aktion not in ("ignorieren", "zur_registrierung"):
        flash("Ungültige Aktion.", "error")
        return redirect(url_for("scanner.list_funde"))

    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige Datei-IDs.", "error")
        return redirect(url_for("scanner.list_funde"))

    if not file_ids:
        flash("Keine Dateien ausgewählt.", "warning")
        return redirect(url_for("scanner.list_funde"))

    if aktion == "ignorieren":
        # Nur Dateien ohne Formeln und ohne IDV-Verknüpfung dürfen ignoriert werden
        placeholders = ",".join("?" * len(file_ids))
        kandidaten = db.execute(f"""
            SELECT f.id FROM idv_files f
            WHERE f.id IN ({placeholders})
              AND (f.formula_count IS NULL OR f.formula_count = 0)
              AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
        """, file_ids).fetchall()
        erlaubte_ids = [r["id"] for r in kandidaten]
        abgelehnt = len(file_ids) - len(erlaubte_ids)

        if erlaubte_ids:
            ph2 = ",".join("?" * len(erlaubte_ids))
            db.execute(
                f"UPDATE idv_files SET bearbeitungsstatus = 'Ignoriert' WHERE id IN ({ph2})",
                erlaubte_ids
            )
            db.commit()
            msg = f"{len(erlaubte_ids)} Datei(en) als 'Ignoriert' markiert."
            if abgelehnt:
                msg += f" {abgelehnt} Datei(en) übersprungen (Formeln vorhanden oder bereits registriert)."
            flash(msg, "success")
        else:
            flash(
                "Keine der ausgewählten Dateien konnte ignoriert werden "
                "(Formeln vorhanden oder bereits im IDV-Register).",
                "warning"
            )

    elif aktion == "zur_registrierung":
        placeholders = ",".join("?" * len(file_ids))
        db.execute(
            f"UPDATE idv_files SET bearbeitungsstatus = 'Zur Registrierung' WHERE id IN ({placeholders})",
            file_ids
        )
        db.commit()
        flash(f"{len(file_ids)} Datei(en) zur Registrierung vorgemerkt.", "success")

    return redirect(url_for("scanner.list_funde", filter=request.form.get("filt", "")))


@bp.route("/funde/<int:file_id>/benachrichtigen", methods=["POST"])
@write_access_required
def notify_file(file_id):
    """Sendet manuell eine E-Mail-Benachrichtigung für einen Scannerfund."""
    db   = get_db()
    file = db.execute("SELECT * FROM idv_files WHERE id=?", (file_id,)).fetchone()
    if not file:
        flash("Datei nicht gefunden.", "error")
        return redirect(url_for("scanner.list_funde"))

    recipients = [
        r["email"] for r in db.execute("""
            SELECT email FROM persons
            WHERE aktiv=1 AND email IS NOT NULL
              AND rolle IN ('IDV-Koordinator','IDV-Administrator','IDV-Entwickler')
        """).fetchall()
        if r["email"]
    ]

    if not recipients:
        flash("Keine E-Mail-Empfänger konfiguriert.", "warning")
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
