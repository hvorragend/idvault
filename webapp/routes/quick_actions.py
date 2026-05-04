"""Quick-Actions Blueprint – signierte Magic-Links aus E-Mail-CTAs.

Vor #352 (Login-Shortcut):
  GET /quick/freigabe/<freigabe_id>?token=<signed>
    Token wird validiert (7-Tage-TTL, HMAC-SHA256). Nicht eingeloggt → Ziel
    in Session merken, auf Login weiterleiten. Eingeloggt → direkt auf die
    Freigabe-Seite weiterleiten.

Seit #352 (anonyme Quick-Action):
  Enthält der Token zusätzlich ``p`` (person_id) und ist der Master-Schalter
  ``quick_action_freigabe_enabled`` aktiv, zeigt der GET-Endpunkt nicht
  eingeloggten Besuchern eine kompakte Action-Seite („Freigeben" / „Ablehnen
  mit Begründung") — nur für einfache Schritte (Sicht-Freigabe u. ä.). Für
  Schritte, die Datei-Uploads oder gesonderte Nachweise brauchen
  (Phase 1 „Fachlicher/Technischer Test", „Fachliche Abnahme" mit
  ungeschütztem Excel, Archivierungs-Schritt), verweist die Seite auf die
  Anmeldung. Die Aktion selbst läuft über ``POST /quick/freigabe/<id>/aktion``.
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint, request, redirect, url_for, session, abort, current_app,
    render_template, flash,
)

from .. import limiter
from ..db_flask import get_db
from ..db_writer import get_writer
from db_write_tx import write_tx

log = logging.getLogger("idvscope.quick_actions")

bp = Blueprint("quick_actions", __name__, url_prefix="/quick")


# Schritt-Namen, bei denen die Quick-Action-Seite nicht direkt entscheiden
# lässt, weil gesonderte Angaben (Tests, Archivierung) nötig sind. Für die
# „Fachliche Abnahme" hängt die Entscheidung zusätzlich an der Zellschutz-
# Akzeptanz — das wird dynamisch geprüft.
_LOGIN_ONLY_SCHRITTE = frozenset({
    "Fachlicher Test",
    "Technischer Test",
    "Archivierung Originaldatei",
})


def _fachliche_abnahme_braucht_login(db, idv_db_id: int) -> bool:
    """True, wenn es verlinkte Excel-Dateien ohne Zell-/Blattschutz gibt,
    die der Fachverantwortliche bisher noch nicht einzeln akzeptiert hat.
    In diesem Fall muss die Fachliche Abnahme im Voll-Formular erfolgen
    (siehe ``_unprotected_excel_files_for_idv`` in routes/freigaben.py)."""
    _EXCEL_OOXML_EXTS = (".xlsx", ".xlsm", ".xlsb", ".xltx", ".xltm")
    placeholders = ",".join("?" * len(_EXCEL_OOXML_EXTS))
    row = db.execute(f"""
        SELECT 1 FROM idv_files f
         WHERE f.status = 'active'
           AND LOWER(f.extension) IN ({placeholders})
           AND COALESCE(f.has_sheet_protection, 0) = 0
           AND COALESCE(f.workbook_protected, 0) = 0
           AND (
                f.id = (SELECT file_id FROM idv_register WHERE id = ?)
             OR f.id IN (SELECT file_id FROM idv_file_links WHERE idv_db_id = ?)
           )
           AND NOT EXISTS (
                SELECT 1 FROM idv_zellschutz_akzeptanz az
                 WHERE az.file_id   = f.id
                   AND az.idv_db_id = ?
           )
         LIMIT 1
    """, (*_EXCEL_OOXML_EXTS, idv_db_id, idv_db_id, idv_db_id)).fetchone()
    return row is not None


def _quick_action_enabled(db) -> bool:
    """Master-Schalter aus ``app_settings``. Default: an (Key nicht gesetzt).

    Die bestehende Admin-Speicherlogik (``request.form.get(key, '')``) legt für
    eine nicht-gecheckte Checkbox einen leeren String an. Der Schalter gilt
    nur bei explizitem ``'1'`` als aktiv — alles andere (leer, ``'0'``) zählt
    als ausgeschaltet. Noch nie gespeicherte Installationen haben keinen
    Key und bekommen damit den Default.
    """
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key='quick_action_freigabe_enabled'"
        ).fetchone()
    except Exception:
        return True
    if row is None:
        return True
    return row["value"] == "1"


def _load_freigabe_context(db, freigabe_id: int, person_id: int | None) -> dict | None:
    """Lädt Freigabe + IDV + Entscheidbarkeits-Informationen für die
    Quick-Action-Seite. Gibt ``None`` zurück, wenn der Schritt nicht mehr
    aussteht oder die Person nicht zuständig ist.
    """
    row = db.execute(
        """
        SELECT f.id           AS freigabe_id,
               f.idv_db_id    AS idv_db_id,
               f.schritt,
               f.status,
               f.pool_id,
               f.zugewiesen_an_id,
               r.idv_id,
               r.bezeichnung,
               r.kurzbeschreibung,
               r.idv_entwickler_id
          FROM idv_freigaben f
          JOIN idv_register r ON r.id = f.idv_db_id
         WHERE f.id = ?
        """,
        (freigabe_id,),
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "Ausstehend":
        return {"row": row, "eligible": False, "reason": "bereits_erledigt"}

    # Funktionstrennung: der IDV-Entwickler darf nicht abschließen.
    if person_id is not None and int(row["idv_entwickler_id"] or 0) == int(person_id):
        return {"row": row, "eligible": False, "reason": "funktionstrennung"}

    # Zuständigkeit prüfen – analog zum Dashboard-„Meine Schritte".
    if person_id is None:
        return {"row": row, "eligible": False, "reason": "kein_person_binding"}

    zugewiesen = row["zugewiesen_an_id"]
    pool_id    = row["pool_id"]
    if zugewiesen and int(zugewiesen) == int(person_id):
        return {"row": row, "eligible": True, "reason": None}

    # Pool-Mitglied?
    if pool_id:
        m = db.execute(
            "SELECT 1 FROM freigabe_pool_members "
            "WHERE pool_id=? AND person_id=?",
            (pool_id, person_id),
        ).fetchone()
        if m is not None:
            return {"row": row, "eligible": True, "reason": None}

    # Aktiver Stellvertreter der zugewiesenen Person?
    if zugewiesen:
        stv = db.execute(
            "SELECT 1 FROM persons "
            "WHERE id = ? AND stellvertreter_id = ? "
            "  AND abwesend_bis IS NOT NULL AND abwesend_bis >= date('now')",
            (zugewiesen, person_id),
        ).fetchone()
        if stv is not None:
            return {"row": row, "eligible": True, "reason": None}

    return {"row": row, "eligible": False, "reason": "nicht_zustaendig"}


@bp.route("/freigabe/<int:freigabe_id>", methods=["GET"])
@limiter.limit("60 per minute; 300 per hour")
def freigabe(freigabe_id: int):
    token = request.args.get("token", "")
    if not token:
        abort(400)

    from ..tokens import verify_freigabe_token
    payload = verify_freigabe_token(current_app.config["SECRET_KEY"], token)
    if payload is None or payload.get("f") != freigabe_id:
        abort(400)

    dest = url_for("freigaben.erledigt_seite", freigabe_id=freigabe_id)

    # Eingeloggt → ins Voll-Formular weiterleiten (bestehendes Verhalten).
    if session.get("user_id"):
        return redirect(dest)

    db = get_db()
    person_id = payload.get("p")

    # Master-Schalter aus oder Token ohne person-Binding: alter
    # Login-Shortcut.
    if not _quick_action_enabled(db) or person_id is None:
        session["_quick_next"] = dest
        return redirect(url_for("auth.login"))

    ctx = _load_freigabe_context(db, freigabe_id, int(person_id))
    if ctx is None:
        return render_template(
            "quick_actions/freigabe_aktion.html",
            state="nicht_gefunden",
            freigabe_id=freigabe_id,
            token=token,
        ), 404

    row    = ctx["row"]
    schritt = row["schritt"]
    state  = "ok"
    if not ctx["eligible"]:
        state = ctx["reason"] or "nicht_zustaendig"
    elif schritt in _LOGIN_ONLY_SCHRITTE:
        state = "login_erforderlich"
    elif schritt == "Fachliche Abnahme" and _fachliche_abnahme_braucht_login(
        db, row["idv_db_id"]
    ):
        state = "login_erforderlich"

    return render_template(
        "quick_actions/freigabe_aktion.html",
        state=state,
        freigabe=row,
        token=token,
        freigabe_id=freigabe_id,
        login_weiterleitung=url_for("auth.login"),
    )


@bp.route("/freigabe/<int:freigabe_id>/aktion", methods=["POST"])
@limiter.limit("10 per minute; 60 per hour")
def freigabe_aktion(freigabe_id: int):
    """POST-Endpunkt der anonymen Quick-Action-Seite. Erwartet den Token als
    Hidden-Field und entscheidet den Schritt auf ``Erledigt`` oder
    ``Abgelehnt``.
    """
    token = request.form.get("token", "")
    if not token:
        abort(400)

    from ..tokens import verify_freigabe_token
    payload = verify_freigabe_token(current_app.config["SECRET_KEY"], token)
    if payload is None or payload.get("f") != freigabe_id:
        abort(400)

    person_id = payload.get("p")
    if person_id is None:
        abort(403)

    db = get_db()
    if not _quick_action_enabled(db):
        abort(404)

    # Fehler-Rückleitung: zurück zur GET-Seite mit Token (die POST-URL
    # selbst hat keinen GET-Handler und würde 405 liefern).
    back_url = url_for(
        "quick_actions.freigabe", freigabe_id=freigabe_id
    ) + f"?token={token}"

    ctx = _load_freigabe_context(db, freigabe_id, int(person_id))
    if ctx is None or not ctx["eligible"]:
        flash("Der Freigabe-Schritt ist nicht mehr offen oder Sie sind dafür "
              "nicht zuständig.", "error")
        return redirect(back_url)

    row     = ctx["row"]
    schritt = row["schritt"]
    needs_login = schritt in _LOGIN_ONLY_SCHRITTE or (
        schritt == "Fachliche Abnahme"
        and _fachliche_abnahme_braucht_login(db, row["idv_db_id"])
    )
    if needs_login:
        flash("Dieser Schritt erfordert zusätzliche Nachweise. Bitte im "
              "System anmelden.", "info")
        return redirect(back_url)

    aktion = request.form.get("aktion", "").strip()
    if aktion not in ("freigeben", "ablehnen"):
        abort(400)

    begruendung = (request.form.get("begruendung") or "").strip()
    if aktion == "ablehnen" and len(begruendung) < 10:
        flash("Für die Ablehnung ist eine Begründung (min. 10 Zeichen) "
              "erforderlich.", "error")
        return redirect(back_url)

    # Person-Anzeigename für Audit-Zeile; fällt auf die person_id zurück,
    # wenn keine persons-Zeile gefunden wird.
    person = db.execute(
        "SELECT id, nachname || ', ' || vorname AS name "
        "FROM persons WHERE id = ? AND aktiv = 1",
        (person_id,),
    ).fetchone()
    person_name = person["name"] if person else f"person_id={person_id}"

    idv_db_id = row["idv_db_id"]
    # Status + History-Aktion an bestehendes State-Vokabular ausrichten:
    # ``ablehnen``-Route schreibt ``Nicht erledigt`` / ``freigabe_abgelehnt``;
    # ``abschliessen``-Route schreibt ``Erledigt`` / ``freigabe_schritt_erledigt``.
    new_status = "Erledigt" if aktion == "freigeben" else "Nicht erledigt"
    history_aktion = (
        "freigabe_schritt_erledigt"
        if aktion == "freigeben"
        else "freigabe_abgelehnt"
    )
    kommentar = (
        f"{schritt} {'erledigt' if aktion == 'freigeben' else 'abgelehnt'} "
        f"per Quick-Action-Magic-Link"
        + (f": {begruendung}" if begruendung else "")
    )

    # Importe für Post-Completion-Hook (nur bei Freigabe, nicht bei
    # Ablehnung) – lokaler Import wegen Blueprint-Zyklus.
    if aktion == "freigeben":
        from .freigaben import (
            _PHASE_2,
            _phase2_komplett_erledigt,
            _ensure_archiv_schritt,
            _finalisiere_freigabe_wenn_komplett,
        )
    else:
        _PHASE_2 = ()

        def _phase2_komplett_erledigt(*_a, **_kw): return False

        def _ensure_archiv_schritt(*_a, **_kw):    return False

        def _finalisiere_freigabe_wenn_komplett(*_a, **_kw): return False

    def _do(c, _fid=freigabe_id, _idv=idv_db_id, _ns=new_status,
            _ha=history_aktion, _k=kommentar, _pid=int(person_id),
            _pn=person_name, _begr=begruendung or None, _schritt=schritt):
        with write_tx(c):
            # Nur ausstehende Schritte; die WHERE-Klausel verhindert, dass
            # parallele Login-Freigaben doppelt zählen.
            cur = c.execute(
                """
                UPDATE idv_freigaben
                   SET status = ?,
                       durchgefuehrt_von_id = ?,
                       durchgefuehrt_am     = datetime('now','utc'),
                       kommentar            = COALESCE(kommentar, ?),
                       bearbeitet_von_id    = COALESCE(bearbeitet_von_id, ?)
                 WHERE id = ? AND status = 'Ausstehend'
                """,
                (_ns, _pid, _begr, _pid, _fid),
            )
            updated = cur.rowcount > 0
            if updated:
                c.execute(
                    "INSERT INTO idv_history "
                    "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                    "VALUES (?,?,?,?,?)",
                    (_idv, _ha, _k, _pid, _pn),
                )
            # Post-Completion-Hook analog zu routes/freigaben.py::abschliessen:
            # schließt Phase 2 ab, legt den Archiv-Schritt an und setzt das
            # IDV auf ``Freigegeben``, sobald alle Phasen erledigt sind.
            freigegeben_flag = False
            archiv_neu_flag = False
            if updated and _schritt in _PHASE_2 and _phase2_komplett_erledigt(c, _idv):
                archiv_neu_flag = _ensure_archiv_schritt(
                    c, _idv, _pid, bearbeiter_name=_pn,
                )
                freigegeben_flag = _finalisiere_freigabe_wenn_komplett(
                    c, _idv, _pid, bearbeiter_name=_pn,
                )
        return freigegeben_flag, archiv_neu_flag

    freigegeben, archiv_neu = get_writer().submit(_do, wait=True)

    # Mail-Benachrichtigungen ausserhalb des Writer-Threads; Fehler werden
    # geloggt, stören aber die Quick-Action-Antwort nicht.
    try:
        if archiv_neu and not freigegeben:
            from .freigaben import _notify_schritte, _PHASE_3
            _notify_schritte(
                db, idv_db_id, [_PHASE_3[0]], {_PHASE_3[0]: None},
            )
    except Exception:
        log.exception("Quick-Action: Archiv-Schritt-Benachrichtigung fehlgeschlagen")

    try:
        if freigegeben:
            from .freigaben import _notify_freigabe_erteilt
            _notify_freigabe_erteilt(db, idv_db_id)
    except Exception:
        log.exception("Quick-Action: Benachrichtigung 'Freigabe erteilt' fehlgeschlagen")

    if aktion == "freigeben":
        flash("Freigabe erteilt. Vielen Dank!", "success")
    else:
        flash("Schritt abgelehnt. Die Koordination wurde informiert.", "success")
    return render_template(
        "quick_actions/freigabe_aktion.html",
        state="fertig",
        aktion=aktion,
        freigabe=row,
        freigabe_id=freigabe_id,
    )
