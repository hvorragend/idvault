"""Admin-Blueprint: Stammdaten verwalten"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from . import login_required, get_db
from datetime import datetime, timezone

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/")
@login_required
def index():
    db = get_db()
    org_units        = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    persons          = db.execute("SELECT p.*, o.bezeichnung AS org FROM persons p LEFT JOIN org_units o ON p.org_unit_id=o.id ORDER BY p.nachname").fetchall()
    geschaeftsprozesse = db.execute("SELECT * FROM geschaeftsprozesse ORDER BY gp_nummer").fetchall()
    plattformen      = db.execute("SELECT * FROM plattformen ORDER BY bezeichnung").fetchall()

    return render_template("admin/index.html",
        org_units=org_units, persons=persons,
        geschaeftsprozesse=geschaeftsprozesse, plattformen=plattformen)


@bp.route("/person/neu", methods=["POST"])
@login_required
def new_person():
    db  = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO persons (kuerzel, nachname, vorname, email, rolle, org_unit_id, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (
        request.form.get("kuerzel","").strip().upper(),
        request.form.get("nachname","").strip(),
        request.form.get("vorname","").strip(),
        request.form.get("email") or None,
        request.form.get("rolle") or None,
        request.form.get("org_unit_id") or None,
        now
    ))
    db.commit()
    flash("Person angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/gp/neu", methods=["POST"])
@login_required
def new_gp():
    db  = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO geschaeftsprozesse
          (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, updated_at, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (
        request.form.get("gp_nummer","").strip(),
        request.form.get("bezeichnung","").strip(),
        request.form.get("bereich") or None,
        1 if request.form.get("ist_kritisch") else 0,
        1 if request.form.get("ist_wesentlich") else 0,
        now, now
    ))
    db.commit()
    flash("Geschäftsprozess angelegt.", "success")
    return redirect(url_for("admin.index"))
