import logging
import sqlite3
from urllib.parse import urlparse
from flask import Blueprint, render_template, request, session, redirect, url_for, flash, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from . import get_db
from ..ldap_auth import ldap_is_enabled, ldap_authenticate, ldap_sync_person
from ..login_logger import log_attempt
from .. import limiter

bp = Blueprint("auth", __name__)


def _safe_next(value: str | None) -> str | None:
    """#406-1: Validiert ein gespeichertes ``next``-Ziel fuer den Post-Login-
    Redirect.

    Akzeptiert nur Same-Origin-Pfade ohne Scheme/Netloc und ohne
    Protocol-Relative-Praefix (``//evil.example/...``). Letzteres wuerde
    der bisherige ``startswith('/')``-Check faelschlich passieren lassen.
    Stand heute kommt der Wert ausschliesslich aus url_for(), aber sobald
    eine externe Quelle (z. B. ?next=…) angeschlossen wird, schuetzt der
    Filter zuverlaessig vor Open-Redirect-Phishing."""
    if not value or not isinstance(value, str):
        return None
    # Schon hier kuerzen, um '//evil/...' nicht als netloc-frei einzustufen.
    if not value.startswith("/") or value.startswith("//"):
        return None
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return None
    return value

# Standard-Algorithmus für Passwort-Hashes: pbkdf2:sha256 mit Salt und
# 600.000 Iterationen (werkzeug-Default seit 2.3).
_MODERN_HASH_METHOD = "pbkdf2:sha256"

# ---------------------------------------------------------------------------
# VULN-F: Demo-Benutzer wurden entfernt.
# Lokale Benutzer werden ausschließlich über ``config.json`` (Feld
# ``IDV_LOCAL_USERS``, Liste von Objekten mit ``username``/``password_hash``)
# konfiguriert und landen in ``current_app.config["IDV_LOCAL_USERS"]``.
# Das verhindert statische, im Quellcode dokumentierte Default-Passwörter.
# ---------------------------------------------------------------------------


def _hash_pw(pw: str) -> str:
    """Erzeugt einen Passwort-Hash (pbkdf2:sha256 mit Salt, 600k Iterationen)."""
    return generate_password_hash(pw, method=_MODERN_HASH_METHOD)


def _verify_password(stored: str, password: str) -> bool:
    """Prüft Passwort gegen einen gespeicherten pbkdf2:sha256-Hash.

    Werkzeug wirft bei Legacy-/unbekannten Hash-Formaten ``ValueError``.
    VULN-011: Andere Exceptions lassen wir durch, damit echte Fehler
    (I/O, MemoryError) sichtbar bleiben.
    """
    if not stored or not password:
        return False
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError):
        logging.getLogger(__name__).info(
            "Passwort-Hash im unbekannten Format – Login abgelehnt"
        )
        return False


def _check_person_login(db, username: str, password: str):
    """Sucht einen Personen-Eintrag mit passendem user_id + password_hash.

    Gibt das Row-Objekt zurück oder None.
    """
    row = db.execute(
        "SELECT * FROM persons WHERE user_id = ? AND aktiv = 1",
        (username,)
    ).fetchone()
    if not row:
        return None
    if not _verify_password(row["password_hash"], password):
        return None
    return row


def _check_config_user(username: str, password: str, db=None):
    """Prüft Benutzer aus ``config.json``-Sektion ``IDV_LOCAL_USERS`` (VULN-F).

    Gibt Session-Dict zurück oder ``None``.

    Ist im Config-Eintrag kein ``person_id`` gepflegt, wird in ``persons``
    nach einem aktiven Datensatz mit passendem ``user_id`` gesucht und das
    Binding daraus übernommen. Damit erbt jeder lokal konfigurierte User
    automatisch sein Person-Binding und behält Ownership-Schreibrechte
    auf eigene IDVs (LDAP-Login löst dies bereits via ``ldap_sync_person``).
    """
    users = current_app.config.get("IDV_LOCAL_USERS") or {}
    user  = users.get(username)
    if not user:
        return None
    if not _verify_password(user.get("password_hash", ""), password):
        return None
    person_id = user.get("person_id")
    if person_id is None and db is not None:
        try:
            row = db.execute(
                "SELECT id FROM persons WHERE user_id = ? AND aktiv = 1",
                (username,),
            ).fetchone()
            if row:
                person_id = row["id"]
        except sqlite3.DatabaseError as e:
            logging.getLogger(__name__).warning(
                "persons-Lookup für Config-User '%s' fehlgeschlagen: %s",
                username, e,
            )
    return {
        "user_id":   username,
        "user_name": user.get("name") or username,
        "user_role": user.get("role") or "",
        "person_id": person_id,
    }


def _do_local_login(db, username: str, password: str):
    """Versucht lokalen Login.

    Reihenfolge:
      1. ``config.json``-Sektion ``IDV_LOCAL_USERS`` (deklarative, versionier-
         bare Betriebs­konfiguration). Ein dort gepflegter Eintrag gewinnt
         bewusst gegen einen gleichnamigen ``persons``-Datensatz – sonst
         überschattet eine (ggf. veraltete) DB-Zeile still die im
         Deployment festgelegte Rolle. Das Shadowing wird geloggt.
      2. ``persons``-Tabelle (LDAP-provisionierte oder manuell angelegte
         Benutzer) als Fallback.
    """
    cfg_result = _check_config_user(username, password, db)
    if cfg_result is not None:
        if db is not None:
            try:
                shadow = db.execute(
                    "SELECT id, rolle FROM persons WHERE user_id = ? AND aktiv = 1",
                    (username,),
                ).fetchone()
                if shadow:
                    logging.getLogger(__name__).info(
                        "Lokaler Login '%s': config.json-Eintrag hat Vorrang "
                        "vor persons-Zeile (DB-Rolle: %s, config-Rolle: %s)",
                        username,
                        shadow["rolle"],
                        cfg_result.get("user_role"),
                    )
            except sqlite3.DatabaseError:
                pass  # nur informative Warnung, kein Abbruchgrund
        return cfg_result

    if db is not None:
        try:
            row = _check_person_login(db, username, password)
            if row:
                return {
                    "user_id":   username,
                    "user_name": f"{row['vorname']} {row['nachname']}".strip() or username,
                    "user_role": row["rolle"] or "",
                    "person_id": row["id"],
                }
        except sqlite3.DatabaseError as e:
            # VULN-011: DB-Fehler nicht schlucken – Admin muss Defekte im
            # persons-Schema bemerken.
            logging.getLogger(__name__).warning(
                "Lokaler DB-Login für '%s' fehlgeschlagen: %s", username, e
            )
    return None


def _login_rate_limit():
    """Liefert das aktuell in ``app_settings['login_rate_limit']`` konfigurierte
    Rate-Limit für /login. Wird zur Request-Zeit gelesen, damit Admin-Änderungen
    ohne Neustart greifen."""
    try:
        from .. import app_settings as _aps
        return _aps.get_login_rate_limit(get_db())
    except Exception:
        return "5 per minute;30 per hour"


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(_login_rate_limit, methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip       = request.remote_addr or "–"

        db = None
        try:
            db = get_db()
        except Exception:
            pass

        # ── 1. LDAP-Login (wenn aktiviert) ───────────────────────────────────
        if db is not None:
            try:
                if ldap_is_enabled(db):
                    secret_key  = current_app.config.get("SECRET_KEY", "")
                    person_data = ldap_authenticate(db, username, password, secret_key)
                    if person_data is not None:
                        person_id = ldap_sync_person(db, person_data)
                        db_person = db.execute(
                            "SELECT user_id FROM persons WHERE id = ?", (person_id,)
                        ).fetchone()
                        uid = db_person["user_id"] if db_person and db_person["user_id"] else username
                        rolle = person_data.get("rolle") or ""
                        session.clear()
                        session["user_id"]   = uid
                        session["user_name"] = f"{person_data['vorname']} {person_data['nachname']}".strip() or username
                        session["user_role"] = rolle
                        session["person_id"] = person_id
                        session["ldap_auth"] = True
                        log_attempt(username, ip, "LDAP", True,
                                    f"Rolle: {rolle or '(ohne)'}  User-ID: {uid}  persons.id={person_id}")
                        _next = _safe_next(session.pop("_quick_next", None))
                        return redirect(_next or url_for("dashboard.index"))
                    # LDAP aktiv, aber Credentials passen nicht → lokalen Login versuchen
                    log_attempt(username, ip, "LDAP", False,
                                "Credentials abgelehnt (falsches Passwort oder Benutzer nicht gefunden)")
            except Exception as e:
                logging.getLogger(__name__).error("LDAP-Login-Fehler: %s", e)
                log_attempt(username, ip, "LDAP", False, f"Verbindungsfehler: {e}")
                # Bei LDAP-Fehler (Server nicht erreichbar): weiter mit lokalem Login

        # ── 2. Lokaler Login (DB-Person oder config.json-Fallback) ───────────
        result = _do_local_login(db, username, password)
        if result:
            _next = _safe_next(session.pop("_quick_next", None))
            session.clear()
            session.update(result)
            log_attempt(username, ip, "lokal", True, f"Rolle: {result.get('user_role', '–')}")
            return redirect(_next or url_for("dashboard.index"))

        log_attempt(username, ip, "lokal", False, "Benutzername oder Passwort falsch")
        flash("Benutzername oder Passwort falsch.", "error")

    # GET
    ldap_active = False
    try:
        db = get_db()
        ldap_active = ldap_is_enabled(db)
    except Exception:
        pass

    return render_template("auth/login.html", ldap_active=ldap_active)


@bp.route("/logout", methods=["POST"])
def logout():
    """Logout ausschließlich per POST (VULN-O), damit GET-basierte
    Cross-Site-Requests (z. B. ``<img src="/logout">`` auf einer bösartigen
    Seite) keinen unbeabsichtigten Logout auslösen. Das Logout-Formular
    in base.html sendet den CSRF-Token mit."""
    session.clear()
    return redirect(url_for("auth.login"))
