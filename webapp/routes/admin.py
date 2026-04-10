"""Admin-Blueprint: Stammdaten verwalten"""
import csv
import io
import hashlib
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from . import login_required, admin_required, get_db
from datetime import datetime, timezone

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Übersicht ──────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def index():
    db = get_db()
    org_units        = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    persons          = db.execute("""
        SELECT p.*, o.bezeichnung AS org
        FROM persons p LEFT JOIN org_units o ON p.org_unit_id=o.id
        ORDER BY p.nachname
    """).fetchall()
    geschaeftsprozesse = db.execute("SELECT * FROM geschaeftsprozesse ORDER BY gp_nummer").fetchall()
    plattformen      = db.execute("SELECT * FROM plattformen ORDER BY bezeichnung").fetchall()
    settings         = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM app_settings").fetchall()}

    return render_template("admin/index.html",
        org_units=org_units, persons=persons,
        geschaeftsprozesse=geschaeftsprozesse, plattformen=plattformen,
        settings=settings)


# ── Personen ───────────────────────────────────────────────────────────────

@bp.route("/person/neu", methods=["POST"])
@login_required
def new_person():
    db = get_db()
    db.execute("""
        INSERT INTO persons (kuerzel, nachname, vorname, email, rolle, org_unit_id,
                             user_id, ad_name, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        request.form.get("kuerzel", "").strip().upper(),
        request.form.get("nachname", "").strip(),
        request.form.get("vorname", "").strip(),
        request.form.get("email") or None,
        request.form.get("rolle") or None,
        request.form.get("org_unit_id") or None,
        request.form.get("user_id") or None,
        request.form.get("ad_name") or None,
        _now()
    ))
    db.commit()
    flash("Person angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/person/<int:pid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_person(pid):
    db = get_db()
    person = db.execute("SELECT * FROM persons WHERE id = ?", (pid,)).fetchone()
    if not person:
        flash("Person nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    org_units = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()

    if request.method == "POST":
        new_pw = request.form.get("password", "").strip()
        pw_hash = _hash_pw(new_pw) if new_pw else person["password_hash"]

        db.execute("""
            UPDATE persons SET
                kuerzel=?, nachname=?, vorname=?, email=?, rolle=?,
                org_unit_id=?, user_id=?, ad_name=?, password_hash=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("kuerzel", "").strip().upper(),
            request.form.get("nachname", "").strip(),
            request.form.get("vorname", "").strip(),
            request.form.get("email") or None,
            request.form.get("rolle") or None,
            request.form.get("org_unit_id") or None,
            request.form.get("user_id") or None,
            request.form.get("ad_name") or None,
            pw_hash,
            1 if request.form.get("aktiv") else 0,
            pid
        ))
        db.commit()
        flash("Person gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/person_edit.html", person=person, org_units=org_units)


@bp.route("/person/<int:pid>/loeschen", methods=["POST"])
@admin_required
def delete_person(pid):
    db = get_db()
    db.execute("UPDATE persons SET aktiv=0 WHERE id=?", (pid,))
    db.commit()
    flash("Person deaktiviert.", "success")
    return redirect(url_for("admin.index"))


# ── Organisationseinheiten ─────────────────────────────────────────────────

@bp.route("/oe/neu", methods=["POST"])
@login_required
def new_oe():
    db = get_db()
    db.execute("""
        INSERT INTO org_units (kuerzel, bezeichnung, ebene, parent_id, created_at)
        VALUES (?,?,?,?,?)
    """, (
        request.form.get("kuerzel", "").strip().upper(),
        request.form.get("bezeichnung", "").strip(),
        request.form.get("ebene") or None,
        request.form.get("parent_id") or None,
        _now()
    ))
    db.commit()
    flash("Organisationseinheit angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/oe/<int:oid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_oe(oid):
    db = get_db()
    oe = db.execute("SELECT * FROM org_units WHERE id=?", (oid,)).fetchone()
    if not oe:
        flash("OE nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    all_oe = db.execute("SELECT * FROM org_units WHERE id!=? ORDER BY bezeichnung", (oid,)).fetchall()

    if request.method == "POST":
        db.execute("""
            UPDATE org_units SET kuerzel=?, bezeichnung=?, ebene=?, parent_id=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("kuerzel", "").strip().upper(),
            request.form.get("bezeichnung", "").strip(),
            request.form.get("ebene") or None,
            request.form.get("parent_id") or None,
            1 if request.form.get("aktiv") else 0,
            oid
        ))
        db.commit()
        flash("Organisationseinheit gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/oe_edit.html", oe=oe, all_oe=all_oe)


@bp.route("/oe/<int:oid>/loeschen", methods=["POST"])
@admin_required
def delete_oe(oid):
    db = get_db()
    db.execute("UPDATE org_units SET aktiv=0 WHERE id=?", (oid,))
    db.commit()
    flash("Organisationseinheit deaktiviert.", "success")
    return redirect(url_for("admin.index"))


# ── Geschäftsprozesse ──────────────────────────────────────────────────────

@bp.route("/gp/neu", methods=["POST"])
@login_required
def new_gp():
    db = get_db()
    now = _now()
    db.execute("""
        INSERT INTO geschaeftsprozesse
          (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, updated_at, created_at)
        VALUES (?,?,?,?,?,?,?)
    """, (
        request.form.get("gp_nummer", "").strip(),
        request.form.get("bezeichnung", "").strip(),
        request.form.get("bereich") or None,
        1 if request.form.get("ist_kritisch") else 0,
        1 if request.form.get("ist_wesentlich") else 0,
        now, now
    ))
    db.commit()
    flash("Geschäftsprozess angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/gp/<int:gid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_gp(gid):
    db = get_db()
    gp = db.execute("SELECT * FROM geschaeftsprozesse WHERE id=?", (gid,)).fetchone()
    if not gp:
        flash("Geschäftsprozess nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        db.execute("""
            UPDATE geschaeftsprozesse SET
                gp_nummer=?, bezeichnung=?, bereich=?, ist_kritisch=?, ist_wesentlich=?,
                beschreibung=?, aktiv=?, updated_at=?
            WHERE id=?
        """, (
            request.form.get("gp_nummer", "").strip(),
            request.form.get("bezeichnung", "").strip(),
            request.form.get("bereich") or None,
            1 if request.form.get("ist_kritisch") else 0,
            1 if request.form.get("ist_wesentlich") else 0,
            request.form.get("beschreibung") or None,
            1 if request.form.get("aktiv") else 0,
            _now(), gid
        ))
        db.commit()
        flash("Geschäftsprozess gespeichert.", "success")
        return redirect(url_for("admin.index"))

    org_units = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    return render_template("admin/gp_edit.html", gp=gp, org_units=org_units)


@bp.route("/gp/<int:gid>/loeschen", methods=["POST"])
@admin_required
def delete_gp(gid):
    db = get_db()
    db.execute("UPDATE geschaeftsprozesse SET aktiv=0 WHERE id=?", (gid,))
    db.commit()
    flash("Geschäftsprozess deaktiviert.", "success")
    return redirect(url_for("admin.index"))


# ── Plattformen ────────────────────────────────────────────────────────────

@bp.route("/plattform/neu", methods=["POST"])
@login_required
def new_plattform():
    db = get_db()
    db.execute("""
        INSERT INTO plattformen (bezeichnung, typ, hersteller)
        VALUES (?,?,?)
    """, (
        request.form.get("bezeichnung", "").strip(),
        request.form.get("typ") or None,
        request.form.get("hersteller") or None,
    ))
    db.commit()
    flash("Plattform angelegt.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/plattform/<int:plid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_plattform(plid):
    db = get_db()
    pl = db.execute("SELECT * FROM plattformen WHERE id=?", (plid,)).fetchone()
    if not pl:
        flash("Plattform nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        db.execute("""
            UPDATE plattformen SET bezeichnung=?, typ=?, hersteller=?, aktiv=?
            WHERE id=?
        """, (
            request.form.get("bezeichnung", "").strip(),
            request.form.get("typ") or None,
            request.form.get("hersteller") or None,
            1 if request.form.get("aktiv") else 0,
            plid
        ))
        db.commit()
        flash("Plattform gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/plattform_edit.html", pl=pl)


@bp.route("/plattform/<int:plid>/loeschen", methods=["POST"])
@admin_required
def delete_plattform(plid):
    db = get_db()
    db.execute("UPDATE plattformen SET aktiv=0 WHERE id=?", (plid,))
    db.commit()
    flash("Plattform deaktiviert.", "success")
    return redirect(url_for("admin.index"))


# ── App-Einstellungen (SMTP etc.) ──────────────────────────────────────────

@bp.route("/einstellungen", methods=["POST"])
@admin_required
def save_settings():
    db = get_db()
    keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password",
            "smtp_from", "smtp_tls", "notify_new_file"]
    for k in keys:
        val = request.form.get(k, "")
        db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)", (k, val))
    db.commit()
    flash("Einstellungen gespeichert.", "success")
    return redirect(url_for("admin.index"))


# ── Mitarbeiter-Import ─────────────────────────────────────────────────────

@bp.route("/import/personen", methods=["POST"])
@admin_required
def import_persons():
    """CSV-Import: user_id, email (SMTP-Adresse), ad_name, oe_kuerzel,
       nachname, vorname, kuerzel, rolle  (Trennzeichen ; oder ,)"""
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Keine Datei ausgewählt.", "error")
        return redirect(url_for("admin.index"))

    db      = get_db()
    content = f.read().decode("utf-8-sig")  # BOM-sicher
    dialect = "excel" if "," in content.split("\n")[0] else "excel-tab"
    # Erkenne Semikolon als Trenner
    first_line = content.split("\n")[0]
    delimiter  = ";" if first_line.count(";") >= first_line.count(",") else ","

    reader  = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    created = updated = errors = 0
    now     = _now()

    for row in reader:
        try:
            # Spalten-Aliase normalisieren (case-insensitive)
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}

            user_id  = r.get("user_id") or r.get("userid") or r.get("benutzername") or ""
            email    = r.get("email") or r.get("smtp") or r.get("smtp_adresse") or r.get("mailadresse") or ""
            ad_name  = r.get("ad_name") or r.get("adname") or r.get("ad") or ""
            oe_k     = (r.get("oe") or r.get("oe_kuerzel") or r.get("abteilung") or "").upper()
            nachname = r.get("nachname") or r.get("name") or ""
            vorname  = r.get("vorname") or ""
            kuerzel  = (r.get("kuerzel") or user_id[:3]).upper()
            rolle    = r.get("rolle") or "Fachverantwortlicher"

            if not (nachname or user_id):
                errors += 1
                continue

            # OE auflösen
            org_unit_id = None
            if oe_k:
                oe_row = db.execute("SELECT id FROM org_units WHERE kuerzel=?", (oe_k,)).fetchone()
                if oe_row:
                    org_unit_id = oe_row["id"]

            # Prüfen ob user_id schon existiert
            existing = None
            if user_id:
                existing = db.execute("SELECT id FROM persons WHERE user_id=?", (user_id,)).fetchone()
            if not existing and kuerzel:
                existing = db.execute("SELECT id FROM persons WHERE kuerzel=?", (kuerzel,)).fetchone()

            if existing:
                db.execute("""
                    UPDATE persons SET
                        email=COALESCE(NULLIF(?,''), email),
                        ad_name=COALESCE(NULLIF(?,''), ad_name),
                        org_unit_id=COALESCE(?,org_unit_id),
                        user_id=COALESCE(NULLIF(?,''), user_id),
                        rolle=COALESCE(NULLIF(?,''), rolle)
                    WHERE id=?
                """, (email, ad_name, org_unit_id, user_id, rolle, existing["id"]))
                updated += 1
            else:
                db.execute("""
                    INSERT INTO persons
                        (kuerzel, nachname, vorname, email, rolle, org_unit_id,
                         user_id, ad_name, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (kuerzel, nachname, vorname, email or None, rolle,
                      org_unit_id, user_id or None, ad_name or None, now))
                created += 1
        except Exception as exc:
            errors += 1

    db.commit()
    flash(f"Import abgeschlossen: {created} neu, {updated} aktualisiert, {errors} Fehler.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/import/vorlage")
@login_required
def import_template():
    """CSV-Vorlage herunterladen."""
    content = "user_id;email;ad_name;oe_kuerzel;nachname;vorname;kuerzel;rolle\n"
    content += "mmu;max.mustermann@bank.de;DOMAIN\\mmu;KRE;Mustermann;Max;MMU;Fachverantwortlicher\n"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mitarbeiter_vorlage.csv"}
    )
