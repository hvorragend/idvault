from flask import Blueprint, render_template
from . import login_required, get_db
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import get_dashboard_stats

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    db   = get_db()
    stats = get_dashboard_stats(db)

    kritische_idvs = db.execute("""
        SELECT r.id, r.idv_id, r.bezeichnung, r.gda_wert, r.status,
               r.naechste_pruefung,
               CASE
                 WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
                 WHEN r.naechste_pruefung < date('now', '+30 days') THEN 'BALD FÄLLIG'
                 ELSE 'OK'
               END AS pruefstatus
        FROM idv_register r
        WHERE (r.gda_wert = 4 OR r.steuerungsrelevant = 1 OR r.dora_kritisch_wichtig = 1)
          AND r.status NOT IN ('Archiviert')
        ORDER BY r.gda_wert DESC, r.naechste_pruefung ASC
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
               r.idv_bezeichnung,
               CASE WHEN m.faellig_am < date('now') THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m
        JOIN v_idv_uebersicht r ON m.idv_id = (SELECT id FROM idv_register WHERE idv_id = r.idv_id)
        WHERE m.status IN ('Offen','In Bearbeitung')
        ORDER BY m.faellig_am ASC
        LIMIT 5
    """).fetchall()

    return render_template("dashboard.html",
        stats=stats,
        kritische_idvs=kritische_idvs,
        prueffaelligkeiten=prueffaelligkeiten,
        offene_massnahmen=offene_massnahmen,
    )
