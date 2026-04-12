"""Scanner-Funde Blueprint"""
import json
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from . import login_required, write_access_required, own_write_required, get_db, admin_required, current_user_role, ROLE_ADMIN

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
    elif filt == "duplikate":
        where_parts.append("f.status = 'active'")
        where_parts.append("""f.file_hash IN (
            SELECT file_hash FROM idv_files
            WHERE status='active' AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
            GROUP BY file_hash HAVING COUNT(*) > 1
        )""")
    else:
        where_parts.append("f.status = 'active'")
        _no_idv = (
            "NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
            " AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)"
        )
        _has_idv = (
            "(EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
            " OR EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id))"
        )
        if filt == "ohne_idv":
            where_parts.append(_no_idv)
            where_parts.append("(f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')")
        elif filt == "mit_idv":
            where_parts.append(_has_idv)
        elif filt == "makros":
            where_parts.append("f.has_macros = 1")
        elif filt == "blattschutz":
            where_parts.append("f.has_sheet_protection = 1")
        elif filt == "ignoriert":
            where_parts.append("f.bearbeitungsstatus = 'Ignoriert'")
        elif filt == "zur_registrierung":
            where_parts.append("f.bearbeitungsstatus = 'Zur Registrierung'")
        else:
            # Standard-Ansicht "Alle": als Ignoriert bewertete Dateien ausblenden
            where_parts.append("(f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')")

    if share_root:
        where_parts.append("f.share_root = ?")
        params.append(share_root)

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    # Duplikate nach Hash sortieren, damit Jinja2-groupby funktioniert
    order_sql = (
        "ORDER BY f.file_hash, f.last_seen_at DESC"
        if filt == "duplikate"
        else "ORDER BY f.last_seen_at DESC, f.modified_at DESC"
    )

    dateien = db.execute(f"""
        SELECT f.*,
               COALESCE(reg.idv_id,       lnk_reg.idv_id)      AS reg_idv_id,
               COALESCE(reg.bezeichnung,  lnk_reg.bezeichnung)  AS reg_bezeichnung,
               COALESCE(reg.id,           lnk_reg.id)           AS reg_db_id,
               sr.id            AS sr_id,
               sr.started_at    AS sr_started_at,
               sr.scan_paths    AS sr_scan_paths
        FROM idv_files f
        LEFT JOIN idv_register  reg     ON reg.file_id    = f.id
        LEFT JOIN idv_file_links lnk    ON lnk.file_id    = f.id
        LEFT JOIN idv_register  lnk_reg ON lnk_reg.id     = lnk.idv_db_id
        LEFT JOIN scan_runs     sr      ON f.last_scan_run_id = sr.id
        {where_sql}
        {order_sql}
        LIMIT 500
    """, params).fetchall()

    # ---------- Duplikat-Erkennung (datenbankweit, nicht nur in der aktuellen Seite) ----------
    duplicate_hashes = {
        r["file_hash"] for r in db.execute("""
            SELECT file_hash FROM idv_files
            WHERE status = 'active'
              AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
            GROUP BY file_hash HAVING COUNT(*) > 1
        """).fetchall()
    }

    # ---------- Zählkarten ----------
    gesamt     = db.execute("SELECT COUNT(*) FROM idv_files WHERE status='active'").fetchone()[0]
    ohne_idv   = db.execute("""
        SELECT COUNT(*) FROM idv_files f WHERE f.status='active'
        AND NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)
        AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)
        AND (f.bearbeitungsstatus IS NULL OR f.bearbeitungsstatus != 'Ignoriert')
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

    try:
        duplikate_anzahl = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT file_hash FROM idv_files
                WHERE status='active' AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
                GROUP BY file_hash HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
    except Exception:
        duplikate_anzahl = 0

    # ---------- Filter-Optionen ----------
    share_roots = [
        r["share_root"] for r in db.execute("""
            SELECT DISTINCT share_root FROM idv_files
            WHERE share_root IS NOT NULL AND status = 'active'
            ORDER BY share_root
        """).fetchall()
    ]
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

    letzter_scan = scan_runs[0] if scan_runs else None
    is_admin = current_user_role() == ROLE_ADMIN

    return render_template("scanner/list.html",
        dateien=dateien, filt=filt,
        gesamt=gesamt, ohne_idv=ohne_idv, mit_makro=mit_makro,
        mit_schutz=mit_schutz, archiviert=archiviert,
        ignoriert=ignoriert, zur_registrierung=zur_registrierung,
        duplikate_anzahl=duplikate_anzahl,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        share_roots=share_roots,
        share_root_filt=share_root,
        scan_runs=scan_runs,
        scan_run_id_filt=scan_run_id,
        letzter_scan=letzter_scan,
        scan_run_label=_scan_run_label,
        duplicate_hashes=duplicate_hashes,
        is_admin=is_admin,
    )


@bp.route("/eingang")
@login_required
def eingang_funde():
    """Eingang: Neue, unbearbeitete Scanner-Funde als priorisierte Arbeitsliste."""
    db = get_db()
    share_root = request.args.get("share_root", "").strip()
    sort       = request.args.get("sort", "prioritaet")
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", 100))
    except (ValueError, TypeError):
        per_page = 100
    if per_page not in (50, 100, 200):
        per_page = 100
    offset = (page - 1) * per_page

    _no_idv = (
        "NOT EXISTS (SELECT 1 FROM idv_register r WHERE r.file_id = f.id)"
        " AND NOT EXISTS (SELECT 1 FROM idv_file_links lnk WHERE lnk.file_id = f.id)"
    )
    where_parts = ["f.status = 'active'", "f.bearbeitungsstatus = 'Neu'", _no_idv]
    params = []
    if share_root:
        where_parts.append("f.share_root = ?")
        params.append(share_root)
    where_sql = "WHERE " + " AND ".join(where_parts)

    sort_map = {
        "prioritaet": "f.has_macros DESC, f.formula_count DESC, f.first_seen_at ASC",
        "datum":      "f.first_seen_at DESC",
        "share":      "f.share_root, f.has_macros DESC, f.formula_count DESC",
        "groesse":    "f.size_bytes DESC",
    }
    order_sql = "ORDER BY " + sort_map.get(sort, sort_map["prioritaet"])

    dateien = db.execute(
        f"SELECT f.*, sr.id AS sr_id, sr.started_at AS sr_started_at "
        f"FROM idv_files f LEFT JOIN scan_runs sr ON f.last_scan_run_id = sr.id "
        f"{where_sql} {order_sql} LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()

    total = db.execute(
        f"SELECT COUNT(*) FROM idv_files f {where_sql}", params
    ).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Stats-Karten
    neu_gesamt = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Neu'"
    ).fetchone()[0]
    neu_mit_makros = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Neu' AND has_macros=1"
    ).fetchone()[0]
    zur_registrierung_count = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active' AND bearbeitungsstatus='Zur Registrierung'"
    ).fetchone()[0]
    gesamt_aktiv = db.execute(
        "SELECT COUNT(*) FROM idv_files WHERE status='active'"
    ).fetchone()[0]

    # Hotspot-Tabellen
    nach_share = db.execute("""
        SELECT share_root,
               COUNT(*) AS anzahl,
               SUM(has_macros) AS mit_makros,
               SUM(CASE WHEN formula_count > 0 THEN 1 ELSE 0 END) AS mit_formeln
        FROM idv_files
        WHERE status='active' AND bearbeitungsstatus='Neu'
        GROUP BY share_root
        ORDER BY anzahl DESC
        LIMIT 10
    """).fetchall()

    nach_typ = db.execute("""
        SELECT extension,
               COUNT(*) AS anzahl,
               SUM(has_macros) AS mit_makros
        FROM idv_files
        WHERE status='active' AND bearbeitungsstatus='Neu'
        GROUP BY extension
        ORDER BY anzahl DESC
        LIMIT 8
    """).fetchall()

    share_roots = [r["share_root"] for r in db.execute(
        "SELECT DISTINCT share_root FROM idv_files "
        "WHERE share_root IS NOT NULL AND status='active' AND bearbeitungsstatus='Neu' "
        "ORDER BY share_root"
    ).fetchall()]

    # Duplikate datenbankweit ermitteln (nicht nur auf der aktuellen Seite)
    duplicate_hashes = {
        r["file_hash"] for r in db.execute("""
            SELECT file_hash FROM idv_files
            WHERE status = 'active'
              AND file_hash IS NOT NULL AND file_hash != 'HASH_ERROR'
            GROUP BY file_hash HAVING COUNT(*) > 1
        """).fetchall()
    }
    is_admin = current_user_role() == ROLE_ADMIN

    return render_template("scanner/eingang.html",
        dateien=dateien,
        total=total, total_pages=total_pages,
        page=page, per_page=per_page,
        neu_gesamt=neu_gesamt,
        neu_mit_makros=neu_mit_makros,
        zur_registrierung_count=zur_registrierung_count,
        gesamt_aktiv=gesamt_aktiv,
        nach_share=nach_share,
        nach_typ=nach_typ,
        share_roots=share_roots,
        share_root_filt=share_root,
        sort=sort,
        duplicate_hashes=duplicate_hashes,
        idv_typ_vorschlag=_idv_typ_vorschlag,
        is_admin=is_admin,
    )


@bp.route("/bewertet")
@login_required
def bewertet():
    """Übersicht bewerteter Eigenentwicklungen:
    a) Ignorierte Scanner-Funde (keine Formeln o.ä.)
    b) Nicht wesentliche IDVs aus dem Scanner
    """
    db = get_db()

    _WESENTLICH_SQL = """(
        r.steuerungsrelevant = 1 OR r.rechnungslegungsrelevant = 1 OR r.dora_kritisch_wichtig = 1
        OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1)
    )"""

    # a) Ignorierte Scanner-Funde
    ignorierte = db.execute("""
        SELECT f.*,
               reg.idv_id      AS reg_idv_id,
               reg.bezeichnung AS reg_bezeichnung,
               reg.id          AS reg_db_id,
               sr.id           AS sr_id,
               sr.started_at   AS sr_started_at
        FROM idv_files f
        LEFT JOIN idv_register reg ON reg.file_id = f.id
        LEFT JOIN scan_runs    sr  ON f.last_scan_run_id = sr.id
        WHERE f.status = 'active' AND f.bearbeitungsstatus = 'Ignoriert'
        ORDER BY f.last_seen_at DESC, f.modified_at DESC
        LIMIT 500
    """).fetchall()

    # b) Nicht wesentliche IDVs, die aus dem Scanner stammen (file_id gesetzt)
    nicht_wesentliche = db.execute(f"""
        SELECT r.id AS idv_db_id, r.idv_id, r.bezeichnung, r.status,
               r.bearbeitungsstatus AS idv_bearbeitungsstatus,
               f.file_name, f.full_path, f.share_root,
               f.id AS file_id,
               p.nachname || ', ' || p.vorname AS fachverantwortlicher,
               ou.kuerzel AS org_einheit
        FROM idv_register r
        JOIN idv_files f ON r.file_id = f.id
        LEFT JOIN persons  p  ON r.fachverantwortlicher_id = p.id
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE f.status = 'active'
          AND NOT {_WESENTLICH_SQL}
        ORDER BY r.bezeichnung
        LIMIT 500
    """).fetchall()

    return render_template("scanner/bewertet.html",
        ignorierte=ignorierte,
        nicht_wesentliche=nicht_wesentliche,
        idv_typ_vorschlag=_idv_typ_vorschlag,
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


@bp.route("/funde/zusammenfassen", methods=["GET", "POST"])
@own_write_required
def zusammenfassen():
    """Mehrere Scanner-Funde zu einem IDV-Projekt zusammenfassen.

    GET  – Bestätigungsseite mit Dateiliste + Optionen
    POST – Dateien mit bestehendem IDV verknüpfen
           oder Weiterleitung zur IDV-Neuanlage
    """
    db = get_db()

    if request.method == "POST":
        aktion   = request.form.get("aktion", "")
        raw_ids  = request.form.getlist("file_ids")
        try:
            file_ids = [int(i) for i in raw_ids if i]
        except ValueError:
            flash("Ungültige Datei-IDs.", "error")
            return redirect(url_for("scanner.list_funde"))

        if not file_ids:
            flash("Keine Dateien ausgewählt.", "warning")
            return redirect(url_for("scanner.list_funde"))

        if aktion == "neues_idv":
            # Primärdatei + zusätzliche IDs an IDV-Neuanlage übergeben
            primary_id  = request.form.get("primary_file_id", "")
            extra_ids   = [str(i) for i in file_ids if str(i) != primary_id]
            url = url_for("idv.new_idv",
                          file_id=primary_id,
                          extra_file_ids=",".join(extra_ids))
            return redirect(url)

        elif aktion == "zu_idv":
            idv_db_id = request.form.get("idv_db_id", "")
            try:
                idv_db_id = int(idv_db_id)
            except (ValueError, TypeError):
                flash("Ungültige IDV-Auswahl.", "error")
                return redirect(url_for("scanner.list_funde"))

            idv_row = db.execute(
                "SELECT id, idv_id FROM idv_register WHERE id=?", (idv_db_id,)
            ).fetchone()
            if not idv_row:
                flash("IDV nicht gefunden.", "error")
                return redirect(url_for("scanner.list_funde"))

            linked = 0
            for fid in file_ids:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) VALUES (?, ?)",
                        (idv_db_id, fid)
                    )
                    db.execute(
                        "UPDATE idv_files SET bearbeitungsstatus='Registriert' WHERE id=?",
                        (fid,)
                    )
                    linked += 1
                except Exception:
                    pass
            db.commit()
            flash(
                f"{linked} Datei(en) mit IDV {idv_row['idv_id']} verknüpft.",
                "success"
            )
            return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

        flash("Unbekannte Aktion.", "error")
        return redirect(url_for("scanner.list_funde"))

    # ---------- GET ----------
    raw_ids = request.args.getlist("file_ids")
    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        file_ids = []

    if not file_ids:
        flash("Keine Dateien ausgewählt.", "warning")
        return redirect(url_for("scanner.list_funde"))

    ph = ",".join("?" * len(file_ids))
    dateien = db.execute(
        f"SELECT * FROM idv_files WHERE id IN ({ph}) ORDER BY last_seen_at DESC",
        file_ids
    ).fetchall()

    # Bestehende IDVs für Dropdown
    idvs = db.execute("""
        SELECT id, idv_id, bezeichnung FROM idv_register
        WHERE status NOT IN ('Außer Betrieb', 'Abgelöst')
        ORDER BY idv_id
    """).fetchall()

    return render_template("scanner/zusammenfassen.html",
        dateien=dateien,
        idvs=idvs,
        idv_typ_vorschlag=_idv_typ_vorschlag,
    )


@bp.route("/funde/bulk-aktion", methods=["POST"])
@own_write_required
def bulk_aktion():
    """Massenmarkierung von Scanner-Funden (ignorieren / zur Registrierung)."""
    db      = get_db()
    aktion  = request.form.get("aktion", "")
    raw_ids = request.form.getlist("file_ids")

    if aktion == "zusammenfassen":
        # Weiterleitung zur Zusammenfassen-Seite (GET)
        from flask import url_for as _uf
        ids_qs = "&".join(f"file_ids={i}" for i in raw_ids if i)
        return redirect(url_for("scanner.zusammenfassen") + "?" + ids_qs)

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
        from flask import session as _session
        from . import ROLE_ADMIN
        ist_admin = (_session.get("user_role") == ROLE_ADMIN)

        placeholders = ",".join("?" * len(file_ids))
        if ist_admin:
            # Admins dürfen alle Dateien ignorieren – keine Einschränkungen
            erlaubte_ids = [r["id"] for r in db.execute(
                f"SELECT id FROM idv_files WHERE id IN ({placeholders})", file_ids
            ).fetchall()]
            abgelehnt = 0
        else:
            # Nur Dateien ohne Formeln und ohne IDV-Verknüpfung
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

    return_to = request.form.get("return_to", "")
    if return_to == "eingang":
        return redirect(url_for("scanner.eingang_funde",
            share_root=request.form.get("share_root_filt", ""),
            page=request.form.get("page", 1),
            per_page=request.form.get("per_page", 100),
            sort=request.form.get("sort", "prioritaet")))
    return redirect(url_for("scanner.list_funde", filter=request.form.get("filt", "")))


@bp.route("/funde/<int:file_id>/loeschen", methods=["POST"])
@admin_required
def loeschen(file_id):
    """Löscht einen Scannerfund-Eintrag dauerhaft (nur für Administratoren)."""
    db = get_db()
    datei = db.execute("SELECT * FROM idv_files WHERE id=?", (file_id,)).fetchone()
    if not datei:
        flash("Datei nicht gefunden.", "error")
        return redirect(url_for("scanner.list_funde"))

    idv_link = db.execute(
        "SELECT id, idv_id FROM idv_register WHERE file_id=?", (file_id,)
    ).fetchone()
    if idv_link:
        flash(
            f"Datei ist mit IDV {idv_link['idv_id']} verknüpft und kann nicht gelöscht werden. "
            "Bitte zuerst die IDV-Verknüpfung aufheben.",
            "error"
        )
        return redirect(url_for("scanner.list_funde"))

    datei_name = datei["file_name"]
    # Abhängige Einträge vor dem Hauptlöschen entfernen (FK-Constraints)
    db.execute("DELETE FROM idv_file_history WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM idv_file_links  WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM idv_files WHERE id=?", (file_id,))
    db.commit()
    flash(f"Scannerfund \"{datei_name}\" wurde gelöscht.", "success")
    return redirect(url_for("scanner.list_funde"))


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
