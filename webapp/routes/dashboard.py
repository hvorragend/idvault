from flask import Blueprint, render_template
from . import login_required, get_db, can_read_all, current_person_id
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import get_dashboard_stats

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    db  = get_db()
    # Eingeschränkte Nutzer (z.B. Fachverantwortliche) sehen nur ihre eigenen
    # unvollständigen IDVs, damit der Zähler zu ihren Berechtigungen passt.
    pid   = None if can_read_all() else current_person_id()
    stats = get_dashboard_stats(db, person_id=pid)

    kritische_idvs = db.execute("""
        SELECT r.id, r.idv_id, r.bezeichnung, r.status,
               r.steuerungsrelevant, r.rechnungslegungsrelevant, r.dora_kritisch_wichtig,
               r.naechste_pruefung,
               CASE
                 WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
                 WHEN r.naechste_pruefung < date('now', '+30 days') THEN 'BALD FÄLLIG'
                 ELSE 'OK'
               END AS pruefstatus
        FROM idv_register r
        WHERE (r.steuerungsrelevant=1 OR r.rechnungslegungsrelevant=1 OR r.dora_kritisch_wichtig=1
               OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id=r.id AND iw.erfuellt=1))
          AND r.status NOT IN ('Archiviert')
        ORDER BY r.naechste_pruefung ASC
        LIMIT 8
    """).fetchall()

    prueffaelligkeiten = db.execute("""
        SELECT r.id, r.idv_id, r.bezeichnung, r.naechste_pruefung,
               CASE
                 WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
                 ELSE 'Bald fällig'
               END AS faelligkeit
        FROM idv_register r
        WHERE r.naechste_pruefung < date('now', '+90 days')
          AND r.status NOT IN ('Archiviert','Abgekündigt')
        ORDER BY r.naechste_pruefung ASC
        LIMIT 6
    """).fetchall()

    offene_massnahmen = db.execute("""
        SELECT m.titel, m.prioritaet, m.faellig_am, m.status,
               r.idv_id, r.bezeichnung AS idv_bezeichnung,
               CASE WHEN m.faellig_am < date('now') THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m
        JOIN idv_register r ON m.idv_id = r.id
        WHERE m.status IN ('Offen','In Bearbeitung')
        ORDER BY m.faellig_am ASC
        LIMIT 5
    """).fetchall()

    letzter_scan = db.execute("""
        SELECT id, started_at, finished_at, total_files, new_files, changed_files, scan_status
        FROM scan_runs
        ORDER BY started_at DESC LIMIT 1
    """).fetchone()

    unverknuepfte_funde = db.execute("""
        SELECT COUNT(*) FROM idv_files
        WHERE status = 'active'
          AND id NOT IN (SELECT file_id FROM idv_file_links)
          AND id NOT IN (SELECT COALESCE(file_id, -1) FROM idv_register WHERE file_id IS NOT NULL)
    """).fetchone()[0]

    return render_template("dashboard.html",
        stats=stats,
        kritische_idvs=kritische_idvs,
        prueffaelligkeiten=prueffaelligkeiten,
        offene_massnahmen=offene_massnahmen,
        letzter_scan=letzter_scan,
        unverknuepfte_funde=unverknuepfte_funde,
    )
