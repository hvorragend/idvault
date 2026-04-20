"""Maßnahmen-Blueprint"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from . import login_required, own_write_required, get_db
from datetime import datetime, timezone
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import get_klassifizierungen
from ..security import ensure_can_write_idv

bp = Blueprint("measures", __name__, url_prefix="/massnahmen")


@bp.route("/")
@login_required
def list_measures():
    db   = get_db()
    filt = request.args.get("filter", "")
    where = "WHERE m.status IN ('Offen','In Bearbeitung')"
    if filt == "ueberfaellig":
        where += " AND m.faellig_am < date('now')"

    massnahmen = db.execute(f"""
        SELECT m.*, r.idv_id, r.bezeichnung AS idv_bezeichnung,
               p.nachname || ', ' || p.vorname AS verantwortlicher,
               CASE WHEN m.faellig_am < date('now') AND m.status IN ('Offen','In Bearbeitung')
                    THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m
        JOIN idv_register r ON m.idv_id = r.id
        LEFT JOIN persons p ON m.verantwortlicher_id = p.id
        {where}
        ORDER BY m.faellig_am ASC
    """).fetchall()

    return render_template("measures/list.html", massnahmen=massnahmen, filt=filt)


@bp.route("/neu/<int:idv_db_id>", methods=["GET", "POST"])
@own_write_required
def new_measure(idv_db_id):
    db  = get_db()
    ensure_can_write_idv(db, idv_db_id)
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("measures.list_measures"))

    if request.method == "POST":
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""
            INSERT INTO massnahmen
              (idv_id, titel, beschreibung, massnahmentyp, prioritaet,
               verantwortlicher_id, faellig_am, status, erstellt_am, aktualisiert_am)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            idv_db_id,
            request.form.get("titel", "").strip(),
            request.form.get("beschreibung") or None,
            request.form.get("massnahmentyp") or None,
            request.form.get("prioritaet", "Mittel"),
            request.form.get("verantwortlicher_id") or None,
            request.form.get("faellig_am") or None,
            "Offen", now, now
        ))
        db.commit()
        flash("Maßnahme angelegt.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    persons = db.execute("SELECT * FROM persons WHERE aktiv=1 ORDER BY nachname").fetchall()
    ist_wesentlich = bool(db.execute(
        "SELECT 1 FROM idv_wesentlichkeit WHERE idv_db_id=? AND erfuellt=1 LIMIT 1",
        (idv_db_id,),
    ).fetchone())
    return render_template("measures/form.html", idv=idv, persons=persons,
        ist_wesentlich=ist_wesentlich,
        massnahmentypen=get_klassifizierungen(db, "massnahmentyp"),
        prioritaeten=get_klassifizierungen(db, "massnahmen_prioritaet"))


@bp.route("/<int:m_id>/erledigen", methods=["POST"])
@own_write_required
def complete_measure(m_id):
    db  = get_db()
    row = db.execute("SELECT idv_id FROM massnahmen WHERE id=?", (m_id,)).fetchone()
    if not row:
        flash("Maßnahme nicht gefunden.", "error")
        return redirect(url_for("measures.list_measures"))
    ensure_can_write_idv(db, row[0])
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        UPDATE massnahmen SET status='Erledigt', erledigt_am=?, aktualisiert_am=?
        WHERE id=?
    """, (now, now, m_id))
    db.commit()
    flash("Maßnahme als erledigt markiert.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=row[0]))
