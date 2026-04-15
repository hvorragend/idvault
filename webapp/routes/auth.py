import logging
from flask import Blueprint, render_template, request, session, redirect, url_for, flash, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from . import get_db
from ..ldap_auth import ldap_is_enabled, ldap_authenticate, ldap_sync_person
from ..login_logger import log_attempt

bp = Blueprint("auth", __name__)

# Standard-Algorithmus für Passwort-Hashes: pbkdf2:sha256 mit Salt und
# 600.000 Iterationen (werkzeug-Default seit 2.3).
_MODERN_HASH_METHOD = "pbkdf2:sha256"

# ---------------------------------------------------------------------------
# Demo-Fallback-Benutzer (für Erstinstallation / wenn keine Persons-Einträge).
# Für Produktion: Mitarbeiter über Admin → Import anlegen und Passwort setzen.
# ---------------------------------------------------------------------------
_DEMO_USERS = {
    "admin": {
        "password": "idvault2026",
        "name": "Administrator",
        "role": "IDV-Administrator",
        "person_id": None,
    },
    "koordinator": {
        "password": "demo",
        "name": "Max Mustermann",
        "role": "IDV-Koordinator",
        "person_id": 1,
    },
    "fachverantwortlicher": {
        "password": "demo",
        "name": "Anna Beispiel",
        "role": "Fachverantwortlicher",
        "person_id": 2,
    },
}


def _hash_pw(pw: str) -> str:
    """Erzeugt einen Passwort-Hash (pbkdf2:sha256 mit Salt, 600k Iterationen)."""
    return generate_password_hash(pw, method=_MODERN_HASH_METHOD)


def _verify_password(stored: str, password: str) -> bool:
    """Prüft Passwort gegen einen gespeicherten pbkdf2:sha256-Hash."""
    if not stored or not password:
        return False
    try:
        return check_password_hash(stored, password)
    except Exception:
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


def _local_login_enabled(db) -> bool:
    """Lokaler Login ist immer verfügbar (Einstellung wird nicht mehr ausgewertet)."""
    return True


def _do_local_login(db, username: str, password: str):
    """Versucht lokalen Login (Personen-DB, dann Demo-Fallback). Gibt Session-Dict oder None zurück."""
    if db is not None:
        try:
            row = _check_person_login(db, username, password)
            if row:
                return {
                    "user_id":   username,
                    "user_name": f"{row['vorname']} {row['nachname']}".strip() or username,
                    "user_role": row["rolle"] or "Fachverantwortlicher",
                    "person_id": row["id"],
                }
        except Exception:
            pass
    user = _DEMO_USERS.get(username)
    if user and user["password"] == password:
        return {
            "user_id":   username,
            "user_name": user["name"],
            "user_role": user["role"],
            "person_id": user.get("person_id"),
        }
    return None


@bp.route("/login", methods=["GET", "POST"])
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
                        if person_data.get("rolle") is None:
                            log_attempt(username, ip, "LDAP", False,
                                        "Authentifizierung OK – keine idvault-Berechtigung zugewiesen")
                            flash(
                                "Anmeldung erfolgreich, aber Ihr AD-Konto hat noch keine "
                                "idvault-Berechtigung. Bitte wenden Sie sich an den Administrator.",
                                "error",
                            )
                            return render_template("auth/login.html", ldap_active=True)
                        person_id = ldap_sync_person(db, person_data)
                        # User-ID aus DB verwenden (= Kürzel für neu importierte Personen)
                        db_person = db.execute(
                            "SELECT user_id FROM persons WHERE id = ?", (person_id,)
                        ).fetchone()
                        uid = db_person["user_id"] if db_person and db_person["user_id"] else username
                        session.clear()
                        session["user_id"]   = uid
                        session["user_name"] = f"{person_data['vorname']} {person_data['nachname']}".strip() or username
                        session["user_role"] = person_data["rolle"]
                        session["person_id"] = person_id
                        session["ldap_auth"] = True
                        log_attempt(username, ip, "LDAP", True,
                                    f"Rolle: {person_data['rolle']}  User-ID: {uid}")
                        return redirect(url_for("dashboard.index"))
                    # LDAP aktiv, aber Credentials passen nicht → lokalen Login versuchen
                    log_attempt(username, ip, "LDAP", False,
                                "Credentials abgelehnt (falsches Passwort oder Benutzer nicht gefunden)")
            except Exception as e:
                logging.getLogger(__name__).error("LDAP-Login-Fehler: %s", e)
                log_attempt(username, ip, "LDAP", False, f"Verbindungsfehler: {e}")
                # Bei LDAP-Fehler (Server nicht erreichbar): weiter mit lokalem Login

        # ── 2. Lokaler Login (immer als Fallback verfügbar) ──────────────────
        result = _do_local_login(db, username, password)
        if result:
            method = "Demo" if username in _DEMO_USERS else "lokal"
            session.clear()
            session.update(result)
            log_attempt(username, ip, method, True, f"Rolle: {result.get('user_role', '–')}")
            return redirect(url_for("dashboard.index"))

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


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
