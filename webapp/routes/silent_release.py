"""Stille Freigabe (Issue #351) — verkuerztes Verfahren fuer
nicht-wesentliche Eigenentwicklungen.

Drei Schritte ohne Koordinator, ohne Pool, ohne separate technische
Pruefung:

1. Selbstzertifizierung des Entwicklers (1 Klick)
   POST /freigaben/eigenentwicklung/<id>/stille-freigabe-starten

2. Sicht-Freigabe Fachverantwortlicher per Magic-Link (2 Klicks)
   GET  /selbst/sicht-freigabe/<token>
   POST /selbst/sicht-freigabe/<token>/bestaetigen

3. Automatische Archivierung mit SHA-256
   intern aufgerufen aus Schritt 2

Vollstaendiger Audit-Trail wird in ``idv_history`` gefuehrt:
``silent_release_self_certified``, ``silent_release_supervisor_acknowledged``,
``silent_release_archived``.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, session)

from . import (login_required, own_write_required, get_db,
               current_person_id)
from ..db_writer import get_writer
from ..security import ensure_can_write_idv
from ..tokens import make_silent_release_token, verify_silent_release_token
from db_write_tx import write_tx


bp_internal = Blueprint("silent_release", __name__, url_prefix="/freigaben")
bp_self     = Blueprint("silent_release_self", __name__, url_prefix="/selbst")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setting_enabled(db) -> bool:
    row = db.execute(
        "SELECT value FROM app_settings WHERE key='silent_release_enabled'"
    ).fetchone()
    return bool(row) and (row["value"] or "").strip() in ("1", "true", "yes")


def _ist_wesentlich(db, idv_db_id: int) -> bool:
    row = db.execute(
        "SELECT 1 FROM idv_wesentlichkeit WHERE idv_db_id=? AND erfuellt=1 LIMIT 1",
        (idv_db_id,),
    ).fetchone()
    return row is not None


def _idv(db, idv_db_id: int):
    return db.execute(
        "SELECT id, idv_id, bezeichnung, status, fachverantwortlicher_id, "
        "       file_id, freigabe_verfahren "
        "  FROM idv_register WHERE id=?",
        (idv_db_id,),
    ).fetchone()


def _add_history(c, idv_db_id: int, aktion: str, kommentar: str,
                 person_id: int | None) -> None:
    c.execute(
        "INSERT INTO idv_history (idv_id, aktion, kommentar, "
        "durchgefuehrt_von_id) VALUES (?,?,?,?)",
        (idv_db_id, aktion, kommentar, person_id),
    )


def _file_sha256(db, idv_db_id: int) -> tuple[str | None, str | None]:
    """Liefert SHA-256 + Dateiname fuer die Hauptdatei der IDV (optional)."""
    row = db.execute(
        "SELECT f.full_path, f.file_name, f.file_hash "
        "  FROM idv_register r "
        "  LEFT JOIN idv_files f ON f.id = r.file_id "
        " WHERE r.id = ?",
        (idv_db_id,),
    ).fetchone()
    if not row:
        return None, None
    if row["file_hash"]:
        return row["file_hash"], row["file_name"]
    if row["full_path"] and os.path.isfile(row["full_path"]):
        h = hashlib.sha256()
        with open(row["full_path"], "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest(), row["file_name"]
    return None, row["file_name"] if row else None


# ── Schritt 1: Entwickler zertifiziert ──────────────────────────────

@bp_internal.route("/eigenentwicklung/<int:idv_db_id>/stille-freigabe-starten",
                   methods=["POST"])
@own_write_required
def stille_freigabe_starten(idv_db_id):
    db = get_db()
    if not _setting_enabled(db):
        flash("Stille Freigabe ist nicht aktiviert.", "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    ensure_can_write_idv(db, idv_db_id)
    idv = _idv(db, idv_db_id)
    if not idv:
        abort(404)
    if _ist_wesentlich(db, idv_db_id):
        flash("Stille Freigabe ist nur fuer nicht-wesentliche Eigenentwicklungen zulaessig.",
              "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
    if not idv["fachverantwortlicher_id"]:
        flash("Kein Fachverantwortlicher hinterlegt — bitte zuerst zuweisen.", "error")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))
    if idv["status"] in ("Freigegeben", "Freigegeben mit Auflagen",
                         "Freigegeben (Stille Freigabe)"):
        flash("Eigenentwicklung ist bereits freigegeben.", "info")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    person_id = current_person_id()
    user_name = session.get("user_name") or ""
    now = _now()

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_register "
                "   SET freigabe_verfahren='Stille Freigabe', "
                "       teststatus='Selbstzertifiziert', "
                "       aktualisiert_am=? "
                " WHERE id=?",
                (now, idv_db_id),
            )
            _add_history(
                c, idv_db_id,
                "silent_release_self_certified",
                f"Entwickler {user_name} bestaetigt Funktion und Korrektheit.",
                person_id,
            )

    get_writer().submit(_do, wait=True)

    # Magic-Link versenden
    secret_key = current_app.config["SECRET_KEY"]
    token = make_silent_release_token(secret_key, idv_db_id,
                                      idv["fachverantwortlicher_id"])
    base = current_app.config.get("APP_BASE_URL") or request.host_url.rstrip("/")
    magic_link = f"{base}/selbst/sicht-freigabe/{token}"

    try:
        from ..email_service import notify_silent_release_supervisor
        notify_silent_release_supervisor(
            db,
            idv_db_id=idv_db_id,
            magic_link=magic_link,
            entwickler_name=user_name,
        )
    except Exception:
        current_app.logger.exception("Versand der Sicht-Freigabe-Mail fehlgeschlagen")
        flash("Selbstzertifizierung gespeichert; Mail konnte aber nicht versendet werden.",
              "warning")
        return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))

    flash("Selbstzertifizierung gespeichert. Sicht-Freigabe per Mail an den Fachverantwortlichen versendet.",
          "success")
    return redirect(url_for("eigenentwicklung.detail_idv", idv_db_id=idv_db_id))


# ── Schritt 2: Fachverantwortlicher quittiert via Magic-Link ────────

def _resolve_token(db, token: str):
    secret_key = current_app.config["SECRET_KEY"]
    payload = verify_silent_release_token(secret_key, token)
    if not payload:
        return None, None
    idv = _idv(db, int(payload["i"]))
    if not idv:
        return None, None
    person = db.execute(
        "SELECT id, vorname, nachname, email FROM persons WHERE id=?",
        (int(payload["p"]),),
    ).fetchone()
    if person is None or person["id"] != idv["fachverantwortlicher_id"]:
        return None, None
    return idv, person


@bp_self.route("/sicht-freigabe/<token>", methods=["GET"])
def sicht_freigabe_seite(token: str):
    db = get_db()
    if not _setting_enabled(db):
        abort(404)
    idv, person = _resolve_token(db, token)
    if not idv:
        return render_template("self_service/sicht_freigabe_fehler.html"), 404
    return render_template(
        "self_service/sicht_freigabe.html",
        idv=idv, person=person, token=token,
        bereits_freigegeben=(idv["status"] == "Freigegeben (Stille Freigabe)"),
    )


@bp_self.route("/sicht-freigabe/<token>/bestaetigen", methods=["POST"])
def sicht_freigabe_bestaetigen(token: str):
    db = get_db()
    if not _setting_enabled(db):
        abort(404)
    idv, person = _resolve_token(db, token)
    if not idv:
        return render_template("self_service/sicht_freigabe_fehler.html"), 404
    if idv["status"] == "Freigegeben (Stille Freigabe)":
        return render_template("self_service/sicht_freigabe_fertig.html",
                               idv=idv, sha256=None, dateiname=None)

    sha256, dateiname = _file_sha256(db, idv["id"])
    now = _now()
    person_id = person["id"]
    person_name = f"{person['vorname']} {person['nachname']}".strip()

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_register "
                "   SET status='Freigegeben (Stille Freigabe)', "
                "       freigabe_verfahren='Stille Freigabe', "
                "       teststatus='Freigegeben', "
                "       status_geaendert_am=?, status_geaendert_von_id=?, "
                "       aktualisiert_am=? "
                " WHERE id=?",
                (now, person_id, now, idv["id"]),
            )
            _add_history(
                c, idv["id"],
                "silent_release_supervisor_acknowledged",
                f"Sicht-Freigabe durch {person_name}.",
                person_id,
            )
            arch_kommentar = (
                f"Archivierung mit SHA-256 {sha256}" if sha256
                else "Archivierung (keine Hauptdatei zur Hash-Bildung)"
            )
            _add_history(
                c, idv["id"],
                "silent_release_archived",
                arch_kommentar,
                person_id,
            )

    get_writer().submit(_do, wait=True)
    return render_template("self_service/sicht_freigabe_fertig.html",
                           idv=idv, sha256=sha256, dateiname=dateiname)
