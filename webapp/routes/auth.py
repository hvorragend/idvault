import hashlib
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from . import get_db

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


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # 1. Personen-basierter Login (Produktionsmodus)
        try:
            db  = get_db()
            row = _check_person_login(db, username, password)
            if row:
                session.clear()
                session["user_id"]   = username
                session["user_name"] = f"{row['vorname']} {row['nachname']}"
                session["user_role"] = row["rolle"] or "Fachverantwortlicher"
                session["person_id"] = row["id"]
                return redirect(url_for("dashboard.index"))
        except Exception:
            pass  # DB noch nicht initialisiert → Demo-Fallback

        # 2. Demo-Fallback
        user = _DEMO_USERS.get(username)
        if user and user["password"] == password:
            session.clear()
            session["user_id"]   = username
            session["user_name"] = user["name"]
            session["user_role"] = user["role"]
            session["person_id"] = user.get("person_id")
            return redirect(url_for("dashboard.index"))

        flash("Benutzername oder Passwort falsch.", "error")

    return render_template("auth/login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
