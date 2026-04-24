"""Admin-Sub-Modul: Pfad-Profile für Bulk-Registrierung (#314).

Ein Pfad-Profil verknüpft einen Pfad-Präfix (z. B.
``\\srv\share\Abteilung_Kredit\``) mit Default-Kopfdaten (OE,
Fachverantwortlicher, Koordinator, Entwicklungsart, Prüfintervall).
Beim Öffnen der Bulk-Registrierung wird das am besten passende Profil
als Vorbelegung gezogen (längstes Präfix gewinnt; siehe
``webapp/routes/eigenentwicklung.py::_best_fund_pfad_profil``).
"""
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash

from .. import admin_required, get_db, current_person_id
from ..eigenentwicklung import ENTWICKLUNGSART_LABEL
from ...db_writer import get_writer
from db_write_tx import write_tx
from . import bp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_or_none(value) -> int | None:
    try:
        v = int(str(value).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _entwicklungsarten(db) -> list[tuple[str, str]]:
    """Liefert die Liste der verfügbaren Entwicklungsarten als
    ``(key, label)``-Tupel. Der Key entspricht dem in der DB gespeicherten
    Wert; das Label wird aus :data:`ENTWICKLUNGSART_LABEL` gezogen
    (MaRisk/DORA-konforme Schreibweise: "Arbeitshilfe", "IDV", …).

    Datenquelle ist die Stammdaten-Klassifikation
    (``klassifikation.kategorie='entwicklungsart'``); Fallback ist die
    Default-Liste aus :mod:`webapp.routes.eigenentwicklung`.
    """
    try:
        rows = db.execute(
            "SELECT wert FROM klassifikation WHERE kategorie='entwicklungsart' "
            "ORDER BY reihenfolge, wert"
        ).fetchall()
    except Exception:
        rows = []
    werte = [r["wert"] for r in rows if r["wert"]]
    if not werte:
        werte = list(ENTWICKLUNGSART_LABEL.keys())
    return [(w, ENTWICKLUNGSART_LABEL.get(w, w)) for w in werte]


@bp.route("/pfad-profile", methods=["GET"])
@admin_required
def list_pfad_profile():
    db = get_db()
    profile = db.execute("""
        SELECT p.id, p.pfad_praefix, p.org_unit_id, p.fachverantwortlicher_id,
               p.idv_koordinator_id, p.entwicklungsart, p.pruefintervall_monate,
               p.bemerkung, p.aktiv,
               o.bezeichnung                        AS oe_name,
               pv.nachname || ', ' || pv.vorname    AS fachv_name,
               pk.nachname || ', ' || pk.vorname    AS koord_name
          FROM fund_pfad_profile p
          LEFT JOIN org_units o  ON p.org_unit_id             = o.id
          LEFT JOIN persons   pv ON p.fachverantwortlicher_id = pv.id
          LEFT JOIN persons   pk ON p.idv_koordinator_id      = pk.id
         ORDER BY p.aktiv DESC, p.pfad_praefix
    """).fetchall()
    org_units = db.execute(
        "SELECT id, bezeichnung FROM org_units WHERE aktiv=1 ORDER BY bezeichnung"
    ).fetchall()
    personen = db.execute(
        "SELECT id, nachname, vorname FROM persons WHERE aktiv=1 "
        "ORDER BY nachname, vorname"
    ).fetchall()
    return render_template("admin/pfad_profile.html",
                           profile=profile,
                           org_units=org_units,
                           personen=personen,
                           entwicklungsarten=_entwicklungsarten(db))


@bp.route("/pfad-profile/neu", methods=["POST"])
@admin_required
def new_pfad_profil():
    db = get_db()
    praefix = (request.form.get("pfad_praefix") or "").strip()
    if not praefix:
        flash("Pfad-Präfix ist erforderlich.", "error")
        return redirect(url_for("admin.list_pfad_profile"))

    data = {
        "pfad_praefix":            praefix,
        "org_unit_id":             _int_or_none(request.form.get("org_unit_id")),
        "fachverantwortlicher_id": _int_or_none(request.form.get("fachverantwortlicher_id")),
        "idv_koordinator_id":      _int_or_none(request.form.get("idv_koordinator_id")),
        "entwicklungsart":         (request.form.get("entwicklungsart") or "").strip() or None,
        "pruefintervall_monate":   _int_or_none(request.form.get("pruefintervall_monate")),
        "bemerkung":               (request.form.get("bemerkung") or "").strip() or None,
    }
    person_id = current_person_id()
    now       = _now()

    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO fund_pfad_profile
                    (pfad_praefix, org_unit_id, fachverantwortlicher_id,
                     idv_koordinator_id, entwicklungsart, pruefintervall_monate,
                     bemerkung, aktiv, created_at, created_by_id)
                VALUES (?,?,?,?,?,?,?,1,?,?)
            """, (data["pfad_praefix"], data["org_unit_id"],
                  data["fachverantwortlicher_id"], data["idv_koordinator_id"],
                  data["entwicklungsart"], data["pruefintervall_monate"],
                  data["bemerkung"], now, person_id))

    try:
        get_writer().submit(_do, wait=True)
        flash(f"Profil '{praefix}' angelegt.", "success")
    except Exception as exc:
        flash(f"Profil konnte nicht angelegt werden: {exc}", "error")
    return redirect(url_for("admin.list_pfad_profile"))


@bp.route("/pfad-profile/<int:profil_id>/bearbeiten", methods=["POST"])
@admin_required
def edit_pfad_profil(profil_id):
    db = get_db()
    profil = db.execute(
        "SELECT * FROM fund_pfad_profile WHERE id=?", (profil_id,)
    ).fetchone()
    if not profil:
        flash("Profil nicht gefunden.", "error")
        return redirect(url_for("admin.list_pfad_profile"))

    praefix = (request.form.get("pfad_praefix") or "").strip() or profil["pfad_praefix"]
    data = {
        "pfad_praefix":            praefix,
        "org_unit_id":             _int_or_none(request.form.get("org_unit_id")),
        "fachverantwortlicher_id": _int_or_none(request.form.get("fachverantwortlicher_id")),
        "idv_koordinator_id":      _int_or_none(request.form.get("idv_koordinator_id")),
        "entwicklungsart":         (request.form.get("entwicklungsart") or "").strip() or None,
        "pruefintervall_monate":   _int_or_none(request.form.get("pruefintervall_monate")),
        "bemerkung":               (request.form.get("bemerkung") or "").strip() or None,
        "aktiv":                   1 if request.form.get("aktiv") else 0,
    }
    now = _now()

    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE fund_pfad_profile
                   SET pfad_praefix=?, org_unit_id=?, fachverantwortlicher_id=?,
                       idv_koordinator_id=?, entwicklungsart=?,
                       pruefintervall_monate=?, bemerkung=?, aktiv=?,
                       updated_at=?
                 WHERE id=?
            """, (data["pfad_praefix"], data["org_unit_id"],
                  data["fachverantwortlicher_id"], data["idv_koordinator_id"],
                  data["entwicklungsart"], data["pruefintervall_monate"],
                  data["bemerkung"], data["aktiv"], now, profil_id))

    try:
        get_writer().submit(_do, wait=True)
        flash("Profil gespeichert.", "success")
    except Exception as exc:
        flash(f"Speichern fehlgeschlagen: {exc}", "error")
    return redirect(url_for("admin.list_pfad_profile"))


@bp.route("/pfad-profile/<int:profil_id>/loeschen", methods=["POST"])
@admin_required
def delete_pfad_profil(profil_id):
    db = get_db()
    profil = db.execute(
        "SELECT pfad_praefix FROM fund_pfad_profile WHERE id=?", (profil_id,)
    ).fetchone()
    if not profil:
        flash("Profil nicht gefunden.", "error")
        return redirect(url_for("admin.list_pfad_profile"))

    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM fund_pfad_profile WHERE id=?", (profil_id,))

    get_writer().submit(_do, wait=True)
    flash(f"Profil '{profil['pfad_praefix']}' gelöscht.", "success")
    return redirect(url_for("admin.list_pfad_profile"))
