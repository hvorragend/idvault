"""Maßnahmen-Blueprint"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from . import (login_required, own_write_required, admin_required, get_db,
               can_read_all, current_person_id)
from datetime import datetime, timezone
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import get_klassifizierungen
from db_write_tx import write_tx
from ..db_writer import get_writer
from ..security import ensure_can_read_idv, ensure_can_write_idv

bp = Blueprint("measures", __name__, url_prefix="/massnahmen")


@bp.route("/")
@login_required
def list_measures():
    db   = get_db()
    filt = request.args.get("filter", "")
    where_parts = ["m.status IN ('Offen','In Bearbeitung')"]
    params: list = []
    if filt == "ueberfaellig":
        where_parts.append("m.faellig_am < date('now')")

    # Row-Level-Sichtbarkeit analog zur IDV-Liste: User ohne Read-All-Rolle
    # sehen nur Maßnahmen zu IDVs, an denen sie beteiligt sind, plus eigene
    # Verantwortungs-/Erledigungseinträge.
    if not can_read_all():
        pid = current_person_id()
        if pid:
            where_parts.append("""(
                r.fachverantwortlicher_id = ?
                OR r.idv_entwickler_id   = ?
                OR r.idv_koordinator_id  = ?
                OR r.stellvertreter_id   = ?
                OR m.verantwortlicher_id = ?
                OR m.erledigt_von_id     = ?
            )""")
            params += [pid, pid, pid, pid, pid, pid]
        else:
            where_parts.append("0")

    massnahmen = db.execute(f"""
        SELECT m.*, r.idv_id, r.bezeichnung AS idv_bezeichnung,
               p.nachname || ', ' || p.vorname AS verantwortlicher,
               CASE WHEN m.faellig_am < date('now') AND m.status IN ('Offen','In Bearbeitung')
                    THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m
        JOIN idv_register r ON m.idv_id = r.id
        LEFT JOIN persons p ON m.verantwortlicher_id = p.id
        WHERE {" AND ".join(where_parts)}
        ORDER BY m.faellig_am ASC
    """, params).fetchall()

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
        params = (
            idv_db_id,
            request.form.get("titel", "").strip(),
            request.form.get("beschreibung") or None,
            request.form.get("massnahmentyp") or None,
            request.form.get("prioritaet", "Mittel"),
            request.form.get("verantwortlicher_id") or None,
            request.form.get("faellig_am") or None,
            "Offen", now, now,
        )

        def _do(c):
            with write_tx(c):
                c.execute("""
                    INSERT INTO massnahmen
                      (idv_id, titel, beschreibung, massnahmentyp, prioritaet,
                       verantwortlicher_id, faellig_am, status, erstellt_am, aktualisiert_am)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, params)

        get_writer().submit(_do, wait=True)
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


@bp.route("/<int:m_id>")
@login_required
def detail_measure(m_id):
    db = get_db()
    m = db.execute("""
        SELECT m.*, r.idv_id, r.bezeichnung AS idv_bezeichnung, r.idv_id AS idv_register_id,
               p.nachname || ', ' || p.vorname AS verantwortlicher,
               CASE WHEN m.faellig_am < date('now') AND m.status IN ('Offen','In Bearbeitung')
                    THEN 'ÜBERFÄLLIG' ELSE 'OK' END AS faelligkeitsstatus
        FROM massnahmen m
        JOIN idv_register r ON m.idv_id = r.id
        LEFT JOIN persons p ON m.verantwortlicher_id = p.id
        WHERE m.id = ?
    """, (m_id,)).fetchone()
    if not m:
        flash("Maßnahme nicht gefunden.", "error")
        return redirect(url_for("measures.list_measures"))
    # Lesezugriff auf das zugrundeliegende IDV sicherstellen, damit Nutzer
    # ohne Beteiligung Maßnahmen auch read-only nicht über die Detail-URL
    # einsehen können (Pendant zu reviews.edit_review).
    ensure_can_read_idv(db, m["idv_id"])
    idv = db.execute("SELECT * FROM idv_register WHERE id=?", (m["idv_id"],)).fetchone()
    ist_wesentlich = bool(db.execute(
        "SELECT 1 FROM idv_wesentlichkeit WHERE idv_db_id=? AND erfuellt=1 LIMIT 1",
        (m["idv_id"],),
    ).fetchone())
    return render_template("measures/detail.html", m=m, idv=idv, ist_wesentlich=ist_wesentlich)


@bp.route("/<int:m_id>/bearbeiten", methods=["GET", "POST"])
@admin_required
def edit_measure(m_id):
    db = get_db()
    m  = db.execute("SELECT * FROM massnahmen WHERE id=?", (m_id,)).fetchone()
    if not m:
        flash("Maßnahme nicht gefunden.", "error")
        return redirect(url_for("measures.list_measures"))
    idv = db.execute("SELECT * FROM idv_register WHERE id=?", (m["idv_id"],)).fetchone()

    if request.method == "POST":
        titel = request.form.get("titel", "").strip()
        if not titel:
            flash("Titel ist ein Pflichtfeld.", "error")
        else:
            now          = datetime.now(timezone.utc).isoformat()
            beschreibung = request.form.get("beschreibung") or None
            mtyp         = request.form.get("massnahmentyp") or None
            prioritaet   = request.form.get("prioritaet", "Mittel")
            verantw_id   = request.form.get("verantwortlicher_id") or None
            faellig_am   = request.form.get("faellig_am") or None
            status       = request.form.get("status", m["status"])

            def _do(c):
                with write_tx(c):
                    c.execute("""
                        UPDATE massnahmen SET
                            titel=?, beschreibung=?, massnahmentyp=?, prioritaet=?,
                            verantwortlicher_id=?, faellig_am=?, status=?, aktualisiert_am=?
                        WHERE id=?
                    """, (titel, beschreibung, mtyp, prioritaet,
                          verantw_id, faellig_am, status, now, m_id))

            get_writer().submit(_do, wait=True)
            flash("Maßnahme aktualisiert.", "success")
            return redirect(url_for("measures.detail_measure", m_id=m_id))

    persons = db.execute("SELECT * FROM persons WHERE aktiv=1 ORDER BY nachname").fetchall()
    ist_wesentlich = bool(db.execute(
        "SELECT 1 FROM idv_wesentlichkeit WHERE idv_db_id=? AND erfuellt=1 LIMIT 1",
        (m["idv_id"],),
    ).fetchone())
    return render_template("measures/edit_form.html", m=m, idv=idv, persons=persons,
        ist_wesentlich=ist_wesentlich,
        massnahmentypen=get_klassifizierungen(db, "massnahmentyp"),
        prioritaeten=get_klassifizierungen(db, "massnahmen_prioritaet"),
        statuswerte=["Offen", "In Bearbeitung", "Zurückgestellt"])


@bp.route("/<int:m_id>/loeschen", methods=["POST"])
@admin_required
def delete_measure(m_id):
    db  = get_db()
    row = db.execute("SELECT idv_id, titel FROM massnahmen WHERE id=?", (m_id,)).fetchone()
    if not row:
        flash("Maßnahme nicht gefunden.", "error")
        return redirect(url_for("measures.list_measures"))
    idv_db_id = row["idv_id"]

    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM massnahmen WHERE id=?", (m_id,))

    get_writer().submit(_do, wait=True)
    flash(f'Maßnahme "{row["titel"]}" gelöscht.', "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


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

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE massnahmen SET status='Erledigt', erledigt_am=?, aktualisiert_am=?
                WHERE id=?
            """, (now, now, m_id))

    get_writer().submit(_do, wait=True)
    flash("Maßnahme als erledigt markiert.", "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=row[0]))
