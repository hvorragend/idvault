"""Self-Service-Blueprint (Issue #315)

Öffentliche, Login-freie Minimalansicht „Meine Funde" für Fachbereichs-
Mitarbeiter. Zugriff ausschließlich über einen signierten Magic-Link, der
per Owner-Digest-Mail verschickt wird.

Sicherheits-Eckpunkte (siehe docs/05-sicherheitskonzept.md):
- HMAC-signierter Token (itsdangerous) + serverseitiger jti mit One-Use-
  Semantik und 7-Tage-TTL.
- Master-Schalter: app_settings["self_service_enabled"] (Admin-UI,
  Default aus).
- Rate-Limit pro Client-IP auf den Actions (kein Brute-Force des jti).
- Keine Anzeige fremder Dateien: strikter Filter auf die owner-Person.
- Audit-Eintrag bei jeder Aktion (self_service_audit, Quelle „mail-link").
"""
from __future__ import annotations

import logging
from flask import (
    Blueprint, request, render_template, redirect, url_for, flash,
    current_app, session, abort,
)

from .. import limiter
from ..db_flask import get_db
from ..db_writer import get_writer
from ..tokens import verify_self_service_token
from db_write_tx import write_tx

log = logging.getLogger("idvault.self_service")

bp = Blueprint("self_service", __name__, url_prefix="/selbst")


# Sitzungs-Keys für den tokengestützten, loginfreien Zugriff. Diese Keys
# werden unabhängig von einer regulären Login-Session gehalten, damit ein
# Mailklick nicht das bestehende Benutzer-Login überschreibt.
_SS_SESSION_PERSON = "_ss_person_id"
_SS_SESSION_JTI    = "_ss_jti"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _self_service_master_enabled(db) -> bool:
    """True nur, wenn der Admin-UI-Schalter ``self_service_enabled``
    in app_settings gesetzt ist (Default: aus)."""
    try:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key='self_service_enabled'"
        ).fetchone()
    except Exception:
        return False
    return bool(row and row["value"] == "1")


def _resolve_session(db) -> dict | None:
    """Validiert die aktuelle Self-Service-Session (Token + DB).

    Gibt ``{"person_id": …, "jti": …}`` zurück oder None, wenn der Token
    nicht mehr gültig ist.
    """
    person_id = session.get(_SS_SESSION_PERSON)
    jti       = session.get(_SS_SESSION_JTI)
    if not person_id or not jti:
        return None
    row = db.execute(
        "SELECT person_id, revoked_at, expires_at "
        "FROM self_service_tokens WHERE jti = ?",
        (jti,),
    ).fetchone()
    if row is None:
        return None
    if row["revoked_at"]:
        return None
    if int(row["person_id"]) != int(person_id):
        return None
    # expires_at ist UTC ohne TZ-Suffix → lexikografischer Vergleich reicht
    now_iso = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if row["expires_at"] and row["expires_at"] < now_iso:
        return None
    return {"person_id": int(person_id), "jti": jti}


def _owner_expressions(person: dict) -> tuple[str, list]:
    """Liefert das WHERE-Fragment und die Parameter für „Funde meines
    Fachbereichs-Mitarbeiters". Gleicht die persons-Zeile gegen
    idv_files.file_owner (case-insensitive) über user_id/kuerzel/ad_name ab.
    """
    clauses = []
    params: list = []
    for col in ("user_id", "kuerzel", "ad_name"):
        val = person.get(col) or ""
        if val:
            clauses.append("LOWER(f.file_owner) = LOWER(?)")
            params.append(val)
    if not clauses:
        # Kein vergleichbares Identifikationsmerkmal → keine Funde.
        return "1 = 0", []
    return "(" + " OR ".join(clauses) + ")", params


def _load_funde(db, person_id: int):
    person = db.execute(
        "SELECT id, user_id, kuerzel, ad_name, email, vorname, nachname "
        "FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()
    if person is None:
        return None, []

    owner_sql, owner_params = _owner_expressions(dict(person))
    funde = db.execute(f"""
        SELECT f.id, f.file_name, f.full_path, f.file_owner,
               f.bearbeitungsstatus, f.has_macros, f.formula_count,
               f.first_seen_at
          FROM idv_files f
         WHERE f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND {owner_sql}
           AND NOT EXISTS (SELECT 1 FROM idv_register r   WHERE r.file_id = f.id)
           AND NOT EXISTS (SELECT 1 FROM idv_file_links l WHERE l.file_id = f.id)
         ORDER BY f.has_macros DESC, f.formula_count DESC, f.first_seen_at ASC
    """, owner_params).fetchall()
    return dict(person), funde


def _file_belongs_to_person(db, file_id: int, person: dict) -> bool:
    owner_sql, owner_params = _owner_expressions(person)
    row = db.execute(
        f"SELECT 1 FROM idv_files f WHERE f.id = ? "
        f"  AND f.status='active' AND f.bearbeitungsstatus='Neu' "
        f"  AND {owner_sql}",
        [file_id] + owner_params,
    ).fetchone()
    return row is not None


def _ss_rate_limit():
    """Rate-Limit für Self-Service-Aktionen. Absichtlich eng, damit der
    Endpoint sich nicht zum Scan-Werkzeug für jti-Werte missbrauchen lässt.
    """
    return "30 per minute; 200 per hour"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/meine-funde", methods=["GET"])
@limiter.limit(_ss_rate_limit)
def meine_funde():
    """Einstieg per Magic-Link (?token=...) oder über bestehende Session."""
    db = get_db()

    if not _self_service_master_enabled(db):
        abort(404)

    token = request.args.get("token", "").strip()
    if token:
        payload = verify_self_service_token(
            current_app.config.get("SECRET_KEY", ""), token
        )
        if not payload or "p" not in payload or "j" not in payload:
            return render_template(
                "self_service/invalid.html",
                grund="Der Link ist ungültig oder abgelaufen."
            ), 400

        jti = str(payload["j"])
        person_id = int(payload["p"])

        row = db.execute(
            "SELECT person_id, revoked_at, expires_at "
            "FROM self_service_tokens WHERE jti = ?",
            (jti,),
        ).fetchone()
        if row is None or row["revoked_at"] or int(row["person_id"]) != person_id:
            return render_template(
                "self_service/invalid.html",
                grund="Der Link wurde bereits entwertet oder ist nicht mehr gültig."
            ), 400

        # Ersten Klick protokollieren (reine Buchführung – der Token bleibt
        # bis zum expliziten „Fertig"/Revoke oder TTL-Ablauf nutzbar, damit
        # Aktionen auf derselben Seite möglich sind).
        def _mark_used(c, _jti=jti):
            with write_tx(c):
                c.execute(
                    "UPDATE self_service_tokens "
                    "SET first_used_at = COALESCE(first_used_at, datetime('now','utc')) "
                    "WHERE jti = ?",
                    (_jti,),
                )
        try:
            get_writer().submit(_mark_used, wait=True)
        except Exception:
            log.exception("Self-Service: first_used_at konnte nicht gesetzt werden")

        # Session für die weiteren POST-Aktionen etablieren. Keys sind
        # getrennt von den regulären Login-Keys – eine bereits angemeldete
        # Person wird nicht ausgeloggt.
        session[_SS_SESSION_PERSON] = person_id
        session[_SS_SESSION_JTI]    = jti
        # Saubere URL ohne Token (Verhindert das Mitloggen in Reverse-Proxies)
        return redirect(url_for("self_service.meine_funde"))

    # Kein Token im Query → Session erforderlich
    ctx = _resolve_session(db)
    if ctx is None:
        return render_template(
            "self_service/invalid.html",
            grund="Die Sitzung ist abgelaufen. Bitte erneut den Link aus der "
                  "E-Mail öffnen.",
        ), 400

    person, funde = _load_funde(db, ctx["person_id"])
    if person is None:
        abort(404)

    return render_template(
        "self_service/meine_funde.html",
        person=person,
        funde=funde,
    )


@bp.route("/fund/<int:file_id>/aktion", methods=["POST"])
@limiter.limit(_ss_rate_limit)
def fund_aktion(file_id: int):
    """POST-Endpoint für „Ignorieren" und „Zur Registrierung vormerken"."""
    db = get_db()
    if not _self_service_master_enabled(db):
        abort(404)

    ctx = _resolve_session(db)
    if ctx is None:
        flash("Sitzung abgelaufen. Bitte Link aus der E-Mail erneut öffnen.", "error")
        return redirect(url_for("self_service.meine_funde"))

    aktion = request.form.get("aktion", "").strip()
    if aktion not in ("ignorieren", "zur_registrierung"):
        flash("Unbekannte Aktion.", "error")
        return redirect(url_for("self_service.meine_funde"))

    person = db.execute(
        "SELECT id, user_id, kuerzel, ad_name FROM persons WHERE id = ?",
        (ctx["person_id"],),
    ).fetchone()
    if person is None or not _file_belongs_to_person(db, file_id, dict(person)):
        flash("Diese Datei steht nicht in Ihrer Zuständigkeit.", "error")
        return redirect(url_for("self_service.meine_funde"))

    neuer_status = (
        "Ignoriert" if aktion == "ignorieren" else "Zur Registrierung"
    )
    audit_aktion = (
        "ignoriert" if aktion == "ignorieren" else "zur_registrierung"
    )
    jti = ctx["jti"]
    person_id = ctx["person_id"]

    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE idv_files SET bearbeitungsstatus = ? "
                "WHERE id = ? AND bearbeitungsstatus = 'Neu'",
                (neuer_status, file_id),
            )
            c.execute(
                "INSERT INTO self_service_audit "
                "(person_id, file_id, aktion, quelle, jti) "
                "VALUES (?, ?, ?, 'mail-link', ?)",
                (person_id, file_id, audit_aktion, jti),
            )

    get_writer().submit(_do, wait=True)
    if aktion == "ignorieren":
        flash('Datei als „Ignoriert" markiert.', "success")
    else:
        flash("Datei zur Registrierung vorgemerkt.", "success")
    return redirect(url_for("self_service.meine_funde"))


@bp.route("/abmelden", methods=["POST"])
def abmelden():
    """Token revoken und Session-Keys löschen (Benutzer-ausgelöste
    „Fertig"-Aktion). Der Link wird dadurch ungültig."""
    db = get_db()
    jti = session.pop(_SS_SESSION_JTI, None)
    session.pop(_SS_SESSION_PERSON, None)
    if jti:
        def _do(c, _jti=jti):
            with write_tx(c):
                c.execute(
                    "UPDATE self_service_tokens "
                    "SET revoked_at = datetime('now','utc') "
                    "WHERE jti = ? AND revoked_at IS NULL",
                    (_jti,),
                )
        try:
            get_writer().submit(_do, wait=True)
        except Exception:
            log.exception("Self-Service: Abmeldung konnte Token nicht revoken")
    return render_template("self_service/fertig.html")
