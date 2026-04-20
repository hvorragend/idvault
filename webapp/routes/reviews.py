"""Prüfungen-Blueprint"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from . import login_required, own_write_required, get_db
from datetime import datetime, timezone, date as _date
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from db import get_klassifizierungen
from ..security import ensure_can_read_idv, ensure_can_write_idv

bp = Blueprint("reviews", __name__, url_prefix="/pruefungen")


@bp.route("/")
@login_required
def list_reviews():
    db   = get_db()
    filt = request.args.get("filter", "")
    where = ""
    if filt == "ueberfaellig":
        where = "AND r.naechste_pruefung < date('now') AND r.status NOT IN ('Archiviert','Abgekündigt')"

    pruefungen = db.execute(f"""
        SELECT p.*, r.idv_id, r.bezeichnung AS idv_bezeichnung,
               per.nachname || ', ' || per.vorname AS pruefer
        FROM pruefungen p
        JOIN idv_register r ON p.idv_id = r.id
        LEFT JOIN persons per ON p.pruefer_id = per.id
        WHERE 1=1 {where}
        ORDER BY p.pruefungsdatum DESC
        LIMIT 100
    """).fetchall()

    return render_template("reviews/list.html", pruefungen=pruefungen, filt=filt)


@bp.route("/neu/<int:idv_db_id>", methods=["GET", "POST"])
@own_write_required
def new_review(idv_db_id):
    db  = get_db()
    # VULN-E: Fremde IDVs dürfen nicht geprüft werden, wenn man nicht beteiligt ist.
    ensure_can_write_idv(db, idv_db_id)
    idv = db.execute("SELECT * FROM idv_register WHERE id = ?", (idv_db_id,)).fetchone()
    if not idv:
        flash("Eigenentwicklung nicht gefunden.", "error")
        return redirect(url_for("reviews.list_reviews"))

    if request.method == "POST":
        now = datetime.now(timezone.utc).isoformat()
        pruefungsdatum = request.form.get("pruefungsdatum") or _date.today().isoformat()
        ergebnis = request.form.get("ergebnis", "Ohne Befund")
        pruefer_id = request.form.get("pruefer_id") or None
        befunde = request.form.get("befunde") or None
        naechste = request.form.get("naechste_pruefung") or None
        kommentar = request.form.get("kommentar") or None

        db.execute("""
            INSERT INTO pruefungen
              (idv_id, pruefungsart, pruefungsdatum, pruefer_id, ergebnis,
               befunde, naechste_pruefung, kommentar, erstellt_am)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (idv_db_id, request.form.get("pruefungsart","Regelprüfung"),
              pruefungsdatum, pruefer_id, ergebnis,
              befunde, naechste, kommentar, now))

        # Nächste Prüfung im Register aktualisieren
        if naechste:
            db.execute("""
                UPDATE idv_register SET letzte_pruefung=?, naechste_pruefung=?, aktualisiert_am=?
                WHERE id=?
            """, (pruefungsdatum, naechste, now, idv_db_id))

        db.execute("""
            INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_am)
            VALUES (?,?,?,?)
        """, (idv_db_id, "geprueft", f"Prüfung {ergebnis} am {pruefungsdatum}", now))

        db.commit()
        flash("Prüfung gespeichert.", "success")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    persons = db.execute("SELECT * FROM persons WHERE aktiv=1 ORDER BY nachname").fetchall()
    ist_wesentlich = bool(db.execute(
        "SELECT 1 FROM idv_wesentlichkeit WHERE idv_db_id=? AND erfuellt=1 LIMIT 1",
        (idv_db_id,),
    ).fetchone())
    return render_template("reviews/form.html", idv=idv, persons=persons,
        ist_wesentlich=ist_wesentlich,
        pruefungsarten=get_klassifizierungen(db, "pruefungsart"),
        pruefungs_ergebnisse=get_klassifizierungen(db, "pruefungs_ergebnis"))


@bp.context_processor
def inject_today():
    return {"today": _date.today().isoformat()}
