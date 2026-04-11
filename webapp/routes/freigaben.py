"""Test- und Freigabeverfahren Blueprint (MaRisk AT 7.2 / BAIT / DORA)

Schrittfolge: Fachlicher Test → Technischer Test → Fachliche Abnahme → Technische Abnahme
Funktionstrennung: Entwickler der IDV darf keine Schritte abschließen.
Nur wesentliche IDVs durchlaufen dieses Verfahren.
"""
from flask import Blueprint, request, flash, redirect, url_for, session
from datetime import datetime, timezone
from . import login_required, own_write_required, get_db, current_person_id

bp = Blueprint("freigaben", __name__, url_prefix="/freigaben")

_SCHRITTE = [
    "Fachlicher Test",
    "Technischer Test",
    "Fachliche Abnahme",
    "Technische Abnahme",
]

_WESENTLICH_SQL = """(
    r.steuerungsrelevant = 1 OR r.rechnungslegungsrelevant = 1 OR r.dora_kritisch_wichtig = 1
    OR EXISTS(SELECT 1 FROM idv_wesentlichkeit iw WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1)
)"""


def _ist_wesentlich(db, idv_db_id: int) -> bool:
    row = db.execute(
        f"SELECT 1 FROM idv_register r WHERE r.id = ? AND {_WESENTLICH_SQL}",
        (idv_db_id,)
    ).fetchone()
    return row is not None


def _funktionstrennung_ok(db, idv_db_id: int, person_id: int) -> bool:
    """True wenn der angemeldete User NICHT der eingetragene Entwickler ist."""
    row = db.execute(
        "SELECT idv_entwickler_id FROM idv_register WHERE id = ?", (idv_db_id,)
    ).fetchone()
    if not row:
        return True
    return row["idv_entwickler_id"] != person_id


@bp.route("/idv/<int:idv_db_id>/starten", methods=["POST"])
@own_write_required
def starten(idv_db_id):
    """Startet den ersten Freigabe-Schritt (Fachlicher Test)."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    # Guard: IDV muss wesentlich sein
    if not _ist_wesentlich(db, idv_db_id):
        flash("Freigabeverfahren nur für wesentliche IDVs erforderlich.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Guard: kein offener Schritt vorhanden
    offen = db.execute(
        "SELECT id FROM idv_freigaben WHERE idv_id=? AND status='Ausstehend'",
        (idv_db_id,)
    ).fetchone()
    if offen:
        flash("Ein Freigabe-Schritt ist bereits offen.", "warning")
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    # Ersten Schritt anlegen
    db.execute("""
        INSERT INTO idv_freigaben (idv_id, schritt, status, beauftragt_von_id, beauftragt_am)
        VALUES (?, ?, 'Ausstehend', ?, ?)
    """, (idv_db_id, _SCHRITTE[0], person_id, now))

    # Bearbeitungsstatus auf "Freigabe ausstehend"
    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus='Freigabe ausstehend', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_gestartet",
         f"Freigabeverfahren gestartet – Schritt 1: {_SCHRITTE[0]}", person_id)
    )
    db.commit()

    # E-Mail an Koordinatoren / Fachverantwortliche (nicht an Entwickler)
    _notify_naechster_schritt(db, idv_db_id, _SCHRITTE[0])

    flash(f"Freigabeverfahren gestartet – Schritt '{_SCHRITTE[0]}' offen.", "success")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


@bp.route("/<int:freigabe_id>/abschliessen", methods=["POST"])
@own_write_required
def abschliessen(freigabe_id):
    """Schließt einen Freigabe-Schritt als 'Bestanden' ab."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("idv.list_idv"))

    idv_db_id = freigabe["idv_id"]

    # Funktionstrennung prüfen
    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler dieser IDV eingetragen "
            "und dürfen keine Freigabe-Schritte abschließen.",
            "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    kommentar = request.form.get("kommentar", "")

    # Schritt abschließen
    db.execute("""
        UPDATE idv_freigaben
        SET status='Bestanden', durchgefuehrt_von_id=?, durchgefuehrt_am=?, kommentar=?
        WHERE id=?
    """, (person_id, now, kommentar or None, freigabe_id))

    # Nächsten Schritt bestimmen
    aktueller_idx = _SCHRITTE.index(freigabe["schritt"]) if freigabe["schritt"] in _SCHRITTE else -1
    naechster_idx = aktueller_idx + 1

    if naechster_idx < len(_SCHRITTE):
        # Nächsten Schritt anlegen
        naechster = _SCHRITTE[naechster_idx]
        db.execute("""
            INSERT INTO idv_freigaben (idv_id, schritt, status, beauftragt_von_id, beauftragt_am)
            VALUES (?, ?, 'Ausstehend', ?, ?)
        """, (idv_db_id, naechster, person_id, now))
        db.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
            (idv_db_id, "freigabe_schritt_bestanden",
             f"{freigabe['schritt']} bestanden → {naechster} offen", person_id)
        )
        db.commit()
        _notify_naechster_schritt(db, idv_db_id, naechster)
        flash(f"'{freigabe['schritt']}' bestanden – nächster Schritt: '{naechster}'.", "success")
    else:
        # Alle 4 Schritte bestanden → Freigabe erteilt
        db.execute("""
            UPDATE idv_register
            SET bearbeitungsstatus='Freigegeben', dokumentationsstatus='Dokumentiert',
                aktualisiert_am=?
            WHERE id=?
        """, (now, idv_db_id))
        db.execute(
            "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
            (idv_db_id, "freigabe_erteilt",
             "Alle 4 Freigabe-Schritte bestanden – IDV freigegeben", person_id)
        )
        db.commit()
        _notify_freigabe_erteilt(db, idv_db_id)
        flash("Alle Freigabe-Schritte bestanden – IDV ist jetzt freigegeben.", "success")

    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


@bp.route("/<int:freigabe_id>/ablehnen", methods=["POST"])
@own_write_required
def ablehnen(freigabe_id):
    """Markiert einen Freigabe-Schritt als 'Nicht bestanden'."""
    db        = get_db()
    person_id = current_person_id()
    now       = datetime.now(timezone.utc).isoformat()

    freigabe = db.execute(
        "SELECT * FROM idv_freigaben WHERE id=?", (freigabe_id,)
    ).fetchone()
    if not freigabe or freigabe["status"] != "Ausstehend":
        flash("Freigabe-Schritt nicht gefunden oder bereits abgeschlossen.", "error")
        return redirect(url_for("idv.list_idv"))

    idv_db_id = freigabe["idv_id"]

    # Funktionstrennung prüfen
    if not _funktionstrennung_ok(db, idv_db_id, person_id):
        flash(
            "Funktionstrennung: Sie sind als Entwickler eingetragen "
            "und dürfen keine Freigabe-Schritte ablehnen.",
            "error"
        )
        return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))

    befunde   = request.form.get("befunde", "")
    kommentar = request.form.get("kommentar", "")

    db.execute("""
        UPDATE idv_freigaben
        SET status='Nicht bestanden', durchgefuehrt_von_id=?, durchgefuehrt_am=?,
            befunde=?, kommentar=?
        WHERE id=?
    """, (person_id, now, befunde or None, kommentar or None, freigabe_id))

    # Bearbeitungsstatus zurücksetzen
    db.execute(
        "UPDATE idv_register SET bearbeitungsstatus='In Bearbeitung', aktualisiert_am=? WHERE id=?",
        (now, idv_db_id)
    )
    db.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, "freigabe_abgelehnt",
         f"{freigabe['schritt']} nicht bestanden. Befunde: {befunde}", person_id)
    )
    db.commit()

    flash(f"'{freigabe['schritt']}' nicht bestanden – Verfahren unterbrochen.", "warning")
    return redirect(url_for("idv.detail_idv", idv_db_id=idv_db_id))


# ---------------------------------------------------------------------------
# Interne Hilfsfunktionen
# ---------------------------------------------------------------------------

def _notify_naechster_schritt(db, idv_db_id: int, schritt: str) -> None:
    """Sendet E-Mail an Koordinatoren/Fachverantwortliche (nicht Entwickler)."""
    try:
        idv = db.execute(
            "SELECT idv_id, bezeichnung, idv_entwickler_id FROM idv_register WHERE id=?",
            (idv_db_id,)
        ).fetchone()
        if not idv:
            return

        recipients = [
            r["email"] for r in db.execute("""
                SELECT DISTINCT p.email FROM persons p
                WHERE p.aktiv=1 AND p.email IS NOT NULL
                  AND p.rolle IN ('IDV-Koordinator','IDV-Administrator')
                  AND p.id != ?
            """, (idv["idv_entwickler_id"] or 0,)).fetchall()
            if r["email"]
        ]
        if not recipients:
            return

        from ..email_service import notify_freigabe_schritt
        notify_freigabe_schritt(db, idv, schritt, recipients)
    except Exception:
        pass


def _notify_freigabe_erteilt(db, idv_db_id: int) -> None:
    """Sendet Abschluss-E-Mail wenn alle 4 Schritte bestanden."""
    try:
        idv = db.execute(
            "SELECT idv_id, bezeichnung FROM idv_register WHERE id=?", (idv_db_id,)
        ).fetchone()
        if not idv:
            return

        recipients = [
            r["email"] for r in db.execute("""
                SELECT email FROM persons
                WHERE aktiv=1 AND email IS NOT NULL
                  AND rolle IN ('IDV-Koordinator','IDV-Administrator','IDV-Entwickler')
            """).fetchall()
            if r["email"]
        ]
        if not recipients:
            return

        from ..email_service import notify_freigabe_abgeschlossen
        notify_freigabe_abgeschlossen(db, idv, recipients)
    except Exception:
        pass
