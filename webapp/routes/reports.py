"""Berichte-Blueprint: Auswertungen nach OE, Ersteller, Scan-Pfad"""
import json
from flask import Blueprint, render_template, request
from . import login_required, get_db

bp = Blueprint("reports", __name__, url_prefix="/berichte")


@bp.route("/")
@login_required
def index():
    db = get_db()

    # ── 1. Bericht nach Organisationseinheit ──────────────────────────────
    by_oe = db.execute("""
        SELECT
            ou.id                                AS oe_id,
            ou.kuerzel                           AS oe_kuerzel,
            ou.bezeichnung                       AS oe_bezeichnung,
            COUNT(r.id)                          AS anzahl,
            SUM(CASE WHEN (r.steuerungsrelevant=1 OR r.rechnungslegungsrelevant=1
                           OR r.dora_kritisch_wichtig=1
                           OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                     WHERE iw.idv_db_id=r.id AND iw.erfuellt=1))
                     THEN 1 ELSE 0 END)          AS wesentlich,
            SUM(CASE WHEN r.status = 'Genehmigt' THEN 1 ELSE 0 END)      AS genehmigt,
            SUM(CASE WHEN r.status = 'Entwurf' THEN 1 ELSE 0 END)        AS entwurf,
            SUM(CASE WHEN r.naechste_pruefung < date('now')
                      AND r.status NOT IN ('Archiviert','Abgekündigt') THEN 1 ELSE 0 END) AS ueberfaellig
        FROM idv_register r
        LEFT JOIN org_units ou ON r.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY ou.id, ou.kuerzel, ou.bezeichnung
        ORDER BY anzahl DESC, ou.bezeichnung
    """).fetchall()

    # ── 2. Bericht nach Fachverantwortlichem ─────────────────────────────
    by_fv = db.execute("""
        SELECT
            p.id                                 AS person_id,
            p.nachname || ', ' || p.vorname      AS person,
            ou.kuerzel                           AS oe_kuerzel,
            COUNT(r.id)                          AS anzahl,
            SUM(CASE WHEN (r.steuerungsrelevant=1 OR r.rechnungslegungsrelevant=1
                           OR r.dora_kritisch_wichtig=1
                           OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw
                                     WHERE iw.idv_db_id=r.id AND iw.erfuellt=1))
                     THEN 1 ELSE 0 END)          AS wesentlich,
            SUM(CASE WHEN r.status = 'Genehmigt' THEN 1 ELSE 0 END)      AS genehmigt,
            SUM(CASE WHEN r.naechste_pruefung < date('now')
                      AND r.status NOT IN ('Archiviert','Abgekündigt') THEN 1 ELSE 0 END) AS ueberfaellig
        FROM idv_register r
        LEFT JOIN persons  p  ON r.fachverantwortlicher_id = p.id
        LEFT JOIN org_units ou ON p.org_unit_id = ou.id
        WHERE r.status NOT IN ('Archiviert')
        GROUP BY p.id, p.nachname, p.vorname, ou.kuerzel
        ORDER BY anzahl DESC, p.nachname
    """).fetchall()

    # ── 3. Bericht nach Scan-Verzeichnis (share_root = Teilscan) ─────────
    by_path = db.execute("""
        SELECT
            f.share_root,
            COUNT(DISTINCT f.id)                 AS dateien_gesamt,
            COUNT(DISTINCT r.id)                 AS registriert,
            COUNT(DISTINCT f.id) - COUNT(DISTINCT r.id) AS nicht_registriert,
            SUM(CASE WHEN f.has_macros = 1 THEN 1 ELSE 0 END)           AS mit_makros,
            MAX(f.last_seen_at)                  AS letzter_fund,
            MAX(sr.started_at)                   AS letzter_scan
        FROM idv_files f
        LEFT JOIN idv_register r  ON r.file_id = f.id
        LEFT JOIN scan_runs    sr ON f.last_scan_run_id = sr.id
        WHERE f.status = 'active'
          AND f.share_root IS NOT NULL
        GROUP BY f.share_root
        ORDER BY dateien_gesamt DESC
    """).fetchall()

    # ── 4. Scan-Lauf-Übersicht (letzte 20) ───────────────────────────────
    try:
        scan_laeufe = db.execute("""
            SELECT id, started_at, finished_at, scan_paths,
                   total_files, new_files, changed_files,
                   moved_files, restored_files, archived_files, errors
            FROM scan_runs
            ORDER BY started_at DESC
            LIMIT 20
        """).fetchall()
    except Exception:
        scan_laeufe = []

    def _parse_paths(raw):
        try:
            return json.loads(raw or "[]")
        except Exception:
            return [raw] if raw else []

    return render_template("reports/index.html",
        by_oe=by_oe,
        by_fv=by_fv,
        by_path=by_path,
        scan_laeufe=scan_laeufe,
        parse_paths=_parse_paths,
    )
