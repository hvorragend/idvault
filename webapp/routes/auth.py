import hashlib
from flask import Blueprint, render_template, request, session, redirect, url_for, flash, current_app
from . import get_db
from ..ldap_auth import ldap_is_enabled, ldap_authenticate, ldap_sync_person

bp = Blueprint("auth", __name__)

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
    return hashlib.sha256(pw.encode()).hexdigest()


def _check_person_login(db, username: str, password: str):
    """Sucht einen Personen-Eintrag mit passendem user_id + password_hash.
    Gibt das Row-Objekt zurück oder None."""
    row = db.execute(
        "SELECT * FROM persons WHERE user_id = ? AND aktiv = 1",
        (username,)
    ).fetchone()
    if row and row["password_hash"] and row["password_hash"] == _hash_pw(password):
        return row
    return None


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
                        session.clear()
                        session["user_id"]   = (db_person["user_id"] if db_person and db_person["user_id"] else username)
                        session["user_name"] = f"{person_data['vorname']} {person_data['nachname']}".strip() or username
                        session["user_role"] = person_data["rolle"]
                        session["person_id"] = person_id
                        session["ldap_auth"] = True
                        return redirect(url_for("dashboard.index"))
                    # LDAP aktiv, aber Credentials passen nicht → lokalen Login versuchen
            except Exception as e:
                import logging
                logging.getLogger(__name__).error("LDAP-Login-Fehler: %s", e)
                # Bei LDAP-Fehler (Server nicht erreichbar): weiter mit lokalem Login

        # ── 2. Lokaler Login (immer als Fallback verfügbar) ──────────────────
        result = _do_local_login(db, username, password)
        if result:
            session.clear()
            session.update(result)
            return redirect(url_for("dashboard.index"))

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
