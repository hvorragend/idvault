import hashlib
import logging
from flask import Blueprint, render_template, request, session, redirect, url_for, flash, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from . import get_db
from ..ldap_auth import ldap_is_enabled, ldap_authenticate, ldap_sync_person
from ..login_logger import log_attempt

bp = Blueprint("auth", __name__)

# Standard-Algorithmus für neue Passwort-Hashes (VULN-001).
# werkzeug nutzt pbkdf2:sha256 mit Salt und 600.000 Iterationen (Default seit
# werkzeug 2.3). Die alten SHA-256-Hashes (64 Hex-Zeichen ohne "$"-Präfix)
# werden beim Login erkannt und in das moderne Format rehasht.
_MODERN_HASH_METHOD = "pbkdf2:sha256"

# ---------------------------------------------------------------------------
# Demo-Fallback-Benutzer (für Erstinstallation / wenn keine Persons-Einträge).
# Für Produktion: Mitarbeiter über Admin → Import anlegen und Passwort setzen.
# ---------------------------------------------------------------------------
_DEMO_USERS = {
    "admin": {
        "password": "idvault2025",
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
    """Erzeugt einen modernen Passwort-Hash (pbkdf2:sha256 mit Salt).

    Hinweis: Die bisherige Implementierung nutzte SHA-256 ohne Salt und ohne
    Key-Stretching (VULN-001). Neue Passwörter werden ab sofort mit dem
    werkzeug-Standard (pbkdf2:sha256, 600k Iterationen, zufälliges Salt)
    gespeichert. Bestands-Hashes werden beim nächsten erfolgreichen Login
    transparent migriert (siehe _check_person_login).
    """
    return generate_password_hash(pw, method=_MODERN_HASH_METHOD)


def _legacy_sha256(pw: str) -> str:
    """Legacy-Hashing (SHA-256 ohne Salt) – nur zum Abgleich mit alten Hashes."""
    return hashlib.sha256(pw.encode()).hexdigest()


def _is_legacy_hash(stored: str) -> bool:
    """True, wenn der DB-Hash noch im alten SHA-256-Format (64 Hex) vorliegt."""
    if not stored:
        return False
    # werkzeug-Hashes beginnen mit "pbkdf2:" / "scrypt:" / "argon2:" etc.
    # Alte Hashes sind 64 Hex-Zeichen ohne Trenner.
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower())


def _verify_password(stored: str, password: str) -> bool:
    """Prüft Passwort gegen einen gespeicherten Hash.

    Unterstützt beide Formate: neue werkzeug-Hashes (mit ``method$salt$hash``)
    und legacy SHA-256-Hashes (reine 64 Hex-Zeichen).
    """
    if not stored or not password:
        return False
    if _is_legacy_hash(stored):
        return _legacy_sha256(password) == stored
    try:
        return check_password_hash(stored, password)
    except Exception:
        return False


def _check_person_login(db, username: str, password: str):
    """Sucht einen Personen-Eintrag mit passendem user_id + password_hash.

    Bei erfolgreichem Match und altem Hash-Format wird der Hash transparent
    in das moderne pbkdf2:sha256-Format migriert (VULN-001 Remediation).
    Gibt das Row-Objekt zurück oder None.
    """
    row = db.execute(
        "SELECT * FROM persons WHERE user_id = ? AND aktiv = 1",
        (username,)
    ).fetchone()
    if not row:
        return None
    stored = row["password_hash"]
    if not _verify_password(stored, password):
        return None

    # Rehash-on-Login: Legacy-Hashes bei erfolgreicher Anmeldung migrieren
    if _is_legacy_hash(stored):
        try:
            new_hash = _hash_pw(password)
            db.execute(
                "UPDATE persons SET password_hash = ? WHERE id = ?",
                (new_hash, row["id"]),
            )
            db.commit()
            logging.getLogger(__name__).info(
                "Passwort-Hash für user_id=%s auf modernes Format migriert.", username
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Rehash fehlgeschlagen für user_id=%s: %s", username, exc
            )
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
