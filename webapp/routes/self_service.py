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

import json
import logging
from datetime import date, datetime, timezone
from flask import (
    Blueprint, request, render_template, redirect, url_for, flash,
    current_app, session, abort,
)

from .. import limiter
from ..db_flask import get_db
from ..db_writer import get_writer
from ..tokens import verify_self_service_token
from db import generate_idv_id
from db_write_tx import write_tx

log = logging.getLogger("idvscope.self_service")

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
    idv_files.file_owner (case-insensitive) über user_id/ad_name ab.
    """
    clauses = []
    params: list = []
    for col in ("user_id", "ad_name"):
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
        "SELECT id, user_id, ad_name, email, vorname, nachname "
        "FROM persons WHERE id = ?",
        (person_id,),
    ).fetchone()
    if person is None:
        return None, [], []

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

    # Offene Zuordnungs-Vorschläge (mittlere Konfidenz aus der Auto-Zuordnung)
    # zu Dateien desselben Owners. Das Self-Service-Formular bietet dem Owner
    # hier „Bestätigen" / „Ablehnen" an.
    vorschlaege = db.execute(f"""
        SELECT s.id          AS suggestion_id,
               s.score,
               f.id           AS file_id,
               f.file_name,
               f.full_path,
               r.id           AS idv_db_id,
               r.idv_id,
               r.bezeichnung  AS idv_bezeichnung
          FROM idv_match_suggestions s
          JOIN idv_files    f ON f.id = s.file_id
          JOIN idv_register r ON r.id = s.idv_db_id
         WHERE s.decision IS NULL
           AND f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND r.status NOT IN ('Archiviert')
           AND NOT EXISTS (SELECT 1 FROM idv_file_links l
                            WHERE l.file_id = f.id AND l.idv_db_id = r.id)
           AND {owner_sql}
         ORDER BY s.score DESC, s.created_at ASC
    """, owner_params).fetchall()
    return dict(person), funde, vorschlaege


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

    person, funde, vorschlaege = _load_funde(db, ctx["person_id"])
    if person is None:
        abort(404)

    return render_template(
        "self_service/meine_funde.html",
        person=person,
        funde=funde,
        vorschlaege=vorschlaege,
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
        "SELECT id, user_id, ad_name FROM persons WHERE id = ?",
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


@bp.route("/vorschlag/<int:suggestion_id>/entscheiden", methods=["POST"])
@limiter.limit(_ss_rate_limit)
def vorschlag_entscheiden(suggestion_id: int):
    """POST-Endpoint für einen Zuordnungs-Vorschlag: ``bestaetigen`` verknüpft
    die Datei mit der vorgeschlagenen Eigenentwicklung und markiert sie als
    ``Registriert``; ``ablehnen`` setzt den Vorschlag auf ``rejected`` und
    lässt die Datei im Eingangskorb."""
    db = get_db()
    if not _self_service_master_enabled(db):
        abort(404)

    ctx = _resolve_session(db)
    if ctx is None:
        flash("Sitzung abgelaufen. Bitte Link aus der E-Mail erneut öffnen.", "error")
        return redirect(url_for("self_service.meine_funde"))

    aktion = request.form.get("aktion", "").strip()
    if aktion not in ("bestaetigen", "ablehnen"):
        flash("Unbekannte Aktion.", "error")
        return redirect(url_for("self_service.meine_funde"))

    person = db.execute(
        "SELECT id, user_id, ad_name FROM persons WHERE id = ?",
        (ctx["person_id"],),
    ).fetchone()
    if person is None:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("self_service.meine_funde"))

    # Vorschlag + Ownership in einem Zug prüfen: der Vorschlag muss offen sein,
    # die Datei aktiv/neu, und der Owner muss mit der Self-Service-Person
    # zusammenfallen (identische Filter wie in _owner_expressions).
    owner_sql, owner_params = _owner_expressions(dict(person))
    row = db.execute(
        f"""
        SELECT s.id AS suggestion_id, s.score,
               f.id AS file_id, f.file_name,
               r.id AS idv_db_id, r.idv_id
          FROM idv_match_suggestions s
          JOIN idv_files    f ON f.id = s.file_id
          JOIN idv_register r ON r.id = s.idv_db_id
         WHERE s.id = ?
           AND s.decision IS NULL
           AND f.status = 'active'
           AND f.bearbeitungsstatus = 'Neu'
           AND r.status NOT IN ('Archiviert')
           AND {owner_sql}
        """,
        [suggestion_id] + owner_params,
    ).fetchone()
    if row is None:
        flash("Der Vorschlag ist nicht mehr gültig oder steht nicht in Ihrer "
              "Zuständigkeit.", "error")
        return redirect(url_for("self_service.meine_funde"))

    jti       = ctx["jti"]
    person_id = ctx["person_id"]
    decision  = "confirmed" if aktion == "bestaetigen" else "rejected"
    audit_aktion = (
        "vorschlag_bestaetigt" if decision == "confirmed" else "vorschlag_abgelehnt"
    )
    history_aktion = (
        "scan_user_confirmed" if decision == "confirmed" else "scan_user_rejected"
    )
    history_kommentar = (
        f"Scanner-Fund '{row['file_name']}' (file_id={row['file_id']}) "
        f"vom Fachbereich "
        + ("bestätigt" if decision == "confirmed" else "abgelehnt")
        + f" (Score {row['score']}, Self-Service)"
    )

    def _do(c,
            _sid=row["suggestion_id"], _fid=row["file_id"], _idv=row["idv_db_id"],
            _decision=decision, _pid=person_id, _jti=jti,
            _audit=audit_aktion, _hist=history_aktion, _hk=history_kommentar):
        with write_tx(c):
            c.execute(
                "UPDATE idv_match_suggestions "
                "SET decision = ?, decided_at = datetime('now','utc'), "
                "    decided_by_person_id = ? "
                "WHERE id = ? AND decision IS NULL",
                (_decision, _pid, _sid),
            )
            if _decision == "confirmed":
                c.execute(
                    "INSERT OR IGNORE INTO idv_file_links "
                    "(idv_db_id, file_id) VALUES (?, ?)",
                    (_idv, _fid),
                )
                c.execute(
                    "UPDATE idv_files SET bearbeitungsstatus='Registriert' "
                    "WHERE id = ? AND bearbeitungsstatus = 'Neu'",
                    (_fid,),
                )
            c.execute(
                "INSERT INTO idv_history "
                "(idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name) "
                "VALUES (?,?,?,?,?)",
                (_idv, _hist, _hk, _pid, None),
            )
            c.execute(
                "INSERT INTO self_service_audit "
                "(person_id, file_id, aktion, quelle, jti) "
                "VALUES (?, ?, ?, 'mail-link', ?)",
                (_pid, _fid, _audit, _jti),
            )

    get_writer().submit(_do, wait=True)
    if decision == "confirmed":
        flash(
            f"Datei „{row['file_name']}“ mit {row['idv_id']} verknüpft.",
            "success",
        )
    else:
        flash(
            f"Vorschlag für „{row['file_name']}“ abgelehnt. Die Datei bleibt "
            "im Eingang.",
            "success",
        )
    return redirect(url_for("self_service.meine_funde"))


# Liste der für die Self-Service-Bulk-Registrierung erlaubten idv_typ-Werte.
# Bewusst knapp gehalten – der IDV-Koordinator verfeinert später.
_SS_IDV_TYP_OPTIONS = (
    "Excel-Tabelle",
    "Excel-Makro",
    "Excel-Modell",
    "Access-Datenbank",
    "Python-Skript",
    "SQL-Skript",
    "Power-BI-Bericht",
    "Sonstige",
    "unklassifiziert",
)

# Einfache Default-Abbildung Klassifikation → Entwicklungsart / Prüfintervall.
# Spiegelt die _TYP_DEFAULTS aus eigenentwicklung.py, ohne Import-Zyklus.
_SS_TYP_DEFAULTS = {
    "Excel-Makro":      {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Excel-Modell":     {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Access-Datenbank": {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Python-Skript":    {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "SQL-Skript":       {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Power-BI-Bericht": {"entwicklungsart": "idv",          "pruefintervall_monate": 12},
    "Excel-Tabelle":    {"entwicklungsart": "arbeitshilfe", "pruefintervall_monate": 24},
    "Sonstige":         {"entwicklungsart": "arbeitshilfe", "pruefintervall_monate": 24},
    "unklassifiziert":  {"entwicklungsart": "arbeitshilfe", "pruefintervall_monate": 12},
}


def _bezeichnung_vorschlag(dateien) -> str:
    """Sinnvoller Default-Vorschlag für die Bezeichnung: Namensstamm der
    längsten gemeinsamen Präfix-Datei, sonst erster Dateiname."""
    names = [d["file_name"] or "" for d in dateien]
    if not names:
        return ""
    # Gemeinsamen Präfix (case-insensitive) bestimmen; stoppe bei "_" / "-" /
    # Ziffer, damit Versionssuffixe wie "_2025-01" abgeschnitten werden.
    first = names[0]
    prefix = ""
    for i in range(len(first)):
        ch = first[i].lower()
        if any(i >= len(n) or n[i].lower() != ch for n in names):
            break
        prefix += first[i]
    prefix = prefix.rstrip(" _-.0123456789")
    return prefix if len(prefix) >= 3 else first.rsplit(".", 1)[0]


@bp.route("/bulk-register", methods=["POST"])
@limiter.limit("10 per minute; 60 per hour")
def bulk_register():
    """Bulk-Registrierung: mehrere Scanner-Funde zu einer Eigenentwicklung.

    Ablauf (ein Endpoint, zwei Phasen):

    1. POST mit ``file_ids`` → Formular rendern
       (Bezeichnung + Klassifikation).
    2. POST mit ``file_ids`` + ``confirm=1`` + Formularfeldern → anlegen,
       Dateien via ``idv_file_links`` verknüpfen, ``bearbeitungsstatus``
       auf ``Registriert`` setzen, Audit-Eintrag schreiben.
    """
    db = get_db()
    if not _self_service_master_enabled(db):
        abort(404)

    ctx = _resolve_session(db)
    if ctx is None:
        flash("Sitzung abgelaufen. Bitte Link aus der E-Mail erneut öffnen.", "error")
        return redirect(url_for("self_service.meine_funde"))

    person_row = db.execute(
        "SELECT id, user_id, ad_name, email, vorname, nachname "
        "FROM persons WHERE id = ?",
        (ctx["person_id"],),
    ).fetchone()
    if person_row is None:
        abort(404)
    person = dict(person_row)

    raw_ids = request.form.getlist("file_ids")
    try:
        file_ids = [int(i) for i in raw_ids if i]
    except ValueError:
        flash("Ungültige Datei-IDs.", "error")
        return redirect(url_for("self_service.meine_funde"))

    # Duplikate entfernen, Reihenfolge stabil
    seen: set[int] = set()
    file_ids = [fid for fid in file_ids if not (fid in seen or seen.add(fid))]

    if len(file_ids) < 1:
        flash("Bitte mindestens eine Datei auswählen.", "warning")
        return redirect(url_for("self_service.meine_funde"))

    # Eigentümer-Prüfung je Datei – hart, nicht überspringbar.
    for fid in file_ids:
        if not _file_belongs_to_person(db, fid, person):
            flash("Mindestens eine Datei steht nicht in Ihrer Zuständigkeit.",
                  "error")
            return redirect(url_for("self_service.meine_funde"))

    # Für beide Phasen: die Dateien für die Anzeige bzw. Audit-Texte laden.
    placeholders = ",".join(["?"] * len(file_ids))
    dateien = db.execute(
        f"SELECT id, file_name, full_path, extension, has_macros, formula_count "
        f"  FROM idv_files WHERE id IN ({placeholders})",
        file_ids,
    ).fetchall()
    # Reihenfolge wie in file_ids (rank-map)
    rank = {fid: i for i, fid in enumerate(file_ids)}
    dateien = sorted(dateien, key=lambda r: rank.get(r["id"], 0))

    # Phase 1: Formular zeigen
    if request.form.get("confirm") != "1":
        # Default-Klassifikation: Vorschlag der ersten Datei
        first = dateien[0] if dateien else None
        typ_vorschlag = ""
        if first is not None:
            ext = (first["extension"] or "").lower()
            if ext in (".xlsx", ".xls", ".xltx") and first["has_macros"]:
                typ_vorschlag = "Excel-Makro"
            else:
                typ_vorschlag = {
                    ".xlsx": "Excel-Tabelle",
                    ".xls":  "Excel-Tabelle",
                    ".xltx": "Excel-Tabelle",
                    ".xlsm": "Excel-Makro",
                    ".xlsb": "Excel-Makro",
                    ".xltm": "Excel-Makro",
                    ".accdb": "Access-Datenbank",
                    ".mdb":   "Access-Datenbank",
                    ".accde": "Access-Datenbank",
                    ".accdr": "Access-Datenbank",
                    ".py":    "Python-Skript",
                    ".sql":   "SQL-Skript",
                    ".pbix":  "Power-BI-Bericht",
                    ".pbit":  "Power-BI-Bericht",
                }.get(ext, "unklassifiziert")
        return render_template(
            "self_service/bulk_register.html",
            person=person,
            dateien=dateien,
            bezeichnung_vorschlag=_bezeichnung_vorschlag(dateien),
            idv_typ_options=_SS_IDV_TYP_OPTIONS,
            idv_typ_vorschlag=typ_vorschlag,
        )

    # Phase 2: anlegen
    bezeichnung = (request.form.get("bezeichnung") or "").strip()
    idv_typ     = (request.form.get("idv_typ") or "").strip()

    if not bezeichnung:
        flash("Bitte eine Bezeichnung angeben.", "error")
        return render_template(
            "self_service/bulk_register.html",
            person=person,
            dateien=dateien,
            bezeichnung_vorschlag=bezeichnung,
            idv_typ_options=_SS_IDV_TYP_OPTIONS,
            idv_typ_vorschlag=idv_typ,
        ), 400
    if idv_typ not in _SS_IDV_TYP_OPTIONS:
        flash("Bitte eine gültige Klassifikation auswählen.", "error")
        return render_template(
            "self_service/bulk_register.html",
            person=person,
            dateien=dateien,
            bezeichnung_vorschlag=bezeichnung,
            idv_typ_options=_SS_IDV_TYP_OPTIONS,
            idv_typ_vorschlag="",
        ), 400
    if len(bezeichnung) > 200:
        bezeichnung = bezeichnung[:200]

    defaults = _SS_TYP_DEFAULTS.get(idv_typ, _SS_TYP_DEFAULTS["unklassifiziert"])
    intervall = int(defaults["pruefintervall_monate"])

    # naechste_pruefung inline berechnen (Monats-Addition mit Tag-Clamp)
    today = date.today()
    m = today.month - 1 + intervall
    y = today.year + m // 12
    m = m % 12 + 1
    import calendar as _cal
    d = min(today.day, _cal.monthrange(y, m)[1])
    naechste_pruefung = date(y, m, d).isoformat()

    now = datetime.now(timezone.utc).isoformat()
    person_id = ctx["person_id"]
    jti       = ctx["jti"]

    bearbeiter_name = (
        f"{person.get('vorname') or ''} {person.get('nachname') or ''}".strip()
        or (person.get("user_id") or person.get("ad_name") or "")
    )

    def _do(c,
            _bez=bezeichnung, _typ=idv_typ,
            _entwart=defaults["entwicklungsart"],
            _intervall=intervall, _np=naechste_pruefung, _now=now,
            _pid=person_id, _name=bearbeiter_name,
            _fids=list(file_ids), _jti=jti):
        with write_tx(c):
            idv_id = generate_idv_id(c)
            cur = c.execute(
                """
                INSERT INTO idv_register (
                    idv_id, bezeichnung, version, idv_typ, entwicklungsart,
                    fachverantwortlicher_id, idv_entwickler_id,
                    status, teststatus,
                    pruefintervall_monate, naechste_pruefung,
                    erfasst_von_id, erstellt_am, aktualisiert_am,
                    tags
                ) VALUES (
                    ?, ?, '1.0', ?, ?,
                    ?, ?,
                    'Entwurf', 'Wertung ausstehend',
                    ?, ?,
                    ?, ?, ?,
                    ?
                )
                """,
                (idv_id, _bez, _typ, _entwart,
                 _pid, _pid,
                 _intervall, _np,
                 _pid, _now, _now,
                 json.dumps(["self-service"], ensure_ascii=False)),
            )
            new_id = cur.lastrowid

            c.execute(
                """
                INSERT INTO idv_history
                  (idv_id, aktion, kommentar, durchgefuehrt_von_id, bearbeiter_name)
                VALUES (?, 'erstellt', ?, ?, ?)
                """,
                (new_id,
                 f"IDV {idv_id} via Self-Service aus {len(_fids)} Fund(en) erstellt",
                 _pid, _name or None),
            )

            for fid in _fids:
                c.execute(
                    "INSERT OR IGNORE INTO idv_file_links (idv_db_id, file_id) "
                    "VALUES (?, ?)",
                    (new_id, fid),
                )
                c.execute(
                    "UPDATE idv_files SET bearbeitungsstatus='Registriert' "
                    "WHERE id = ?",
                    (fid,),
                )

            # Audit-Einträge: pro file_id eine Zeile für die vorhandenen
            # Indizes, zusätzlich eine Sammelzeile mit JSON-Liste zur
            # schnellen Nachvollziehbarkeit (file_id=NULL dort).
            for fid in _fids:
                c.execute(
                    "INSERT INTO self_service_audit "
                    "(person_id, file_id, aktion, quelle, jti) "
                    "VALUES (?, ?, 'self_service_bulk_registered', "
                    "        'mail-link', ?)",
                    (_pid, fid, _jti),
                )
            c.execute(
                "INSERT INTO self_service_audit "
                "(person_id, file_id, aktion, quelle, jti) "
                "VALUES (?, NULL, ?, 'mail-link', ?)",
                (_pid,
                 "self_service_bulk_registered:"
                 + json.dumps({"idv_db_id": new_id,
                               "idv_id": idv_id,
                               "file_ids": _fids},
                              ensure_ascii=False),
                 _jti),
            )
            return new_id, idv_id

    try:
        new_id, new_idv_id = get_writer().submit(_do, wait=True)
    except Exception:
        log.exception("Self-Service: Bulk-Registrierung fehlgeschlagen")
        flash("Beim Anlegen ist ein Fehler aufgetreten. Bitte erneut versuchen.",
              "error")
        return redirect(url_for("self_service.meine_funde"))

    return render_template(
        "self_service/bulk_registered.html",
        idv_id=new_idv_id,
        bezeichnung=bezeichnung,
        anzahl=len(file_ids),
    )


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
