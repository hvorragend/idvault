"""Stammdaten: Personen, OE, Geschäftsprozesse, Plattformen, Klassifizierungen,
Wesentlichkeit, Mitarbeiter- und Geschäftsprozess-Import."""
import csv
import io
import sqlite3

from flask import render_template, request, redirect, url_for, flash, Response, current_app

from . import bp, _upload_rate_limit, _hash_pw, _now, _KLASSIFIZIERUNGS_BEREICHE
from .. import login_required, admin_required, get_db
from ...security import in_clause
from ...db_writer import get_writer
from ... import limiter

from db_write_tx import write_tx


# ── Personen ───────────────────────────────────────────────────────────────

@bp.route("/person/neu", methods=["POST"])
@login_required
def new_person():
    params = (
        request.form.get("kuerzel", "").strip().upper(),
        request.form.get("nachname", "").strip(),
        request.form.get("vorname", "").strip(),
        request.form.get("email") or None,
        request.form.get("rolle") or None,
        request.form.get("org_unit_id") or None,
        request.form.get("user_id") or None,
        request.form.get("ad_name") or None,
        _now(),
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO persons (kuerzel, nachname, vorname, email, rolle, org_unit_id,
                                     user_id, ad_name, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, params)
    try:
        get_writer().submit(_do, wait=True)
    except sqlite3.OperationalError as exc:
        current_app.logger.warning("new_person: Datenbank gesperrt: %s", exc)
        flash("Datenbank vorübergehend gesperrt, bitte in wenigen Sekunden erneut versuchen.", "error")
        return redirect(url_for("admin.index"))
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

        params = (
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
            pid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE persons SET
                        kuerzel=?, nachname=?, vorname=?, email=?, rolle=?,
                        org_unit_id=?, user_id=?, ad_name=?, password_hash=?, aktiv=?
                    WHERE id=?
                """, params)
        try:
            get_writer().submit(_do, wait=True)
        except sqlite3.OperationalError as exc:
            current_app.logger.warning("edit_person (pid=%s): Datenbank gesperrt: %s", pid, exc)
            flash("Datenbank vorübergehend gesperrt, bitte in wenigen Sekunden erneut versuchen.", "error")
            return redirect(url_for("admin.index"))
        flash("Person gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/person_edit.html", person=person, org_units=org_units)


@bp.route("/person/<int:pid>/loeschen", methods=["POST"])
@admin_required
def delete_person(pid):
    def _do(c):
        with write_tx(c):
            c.execute("UPDATE persons SET aktiv=0 WHERE id=?", (pid,))
    get_writer().submit(_do, wait=True)
    flash("Person deaktiviert.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/person/<int:pid>/endgueltig-loeschen", methods=["POST"])
@admin_required
def delete_person_hard(pid):
    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM persons WHERE id=?", (pid,))
    try:
        get_writer().submit(_do, wait=True)
        flash("Person gelöscht.", "success")
    except Exception:
        flash("Person konnte nicht gelöscht werden (noch Eigenentwicklungen zugeordnet) – bitte zuerst deaktivieren.", "warning")
    return redirect(url_for("admin.index"))


@bp.route("/personen/bulk", methods=["POST"])
@admin_required
def bulk_persons():
    """Bulk-Aktion auf mehrere Personen: deactivate oder delete."""
    db     = get_db()
    action = request.form.get("action", "")
    raw    = request.form.getlist("person_ids")
    ids    = [int(i) for i in raw if i.isdigit()]

    if not ids:
        flash("Keine Personen ausgewählt.", "warning")
        return redirect(url_for("admin.index"))

    if action == "deactivate":
        ph, ph_params = in_clause(ids)
        def _do(c):
            with write_tx(c):
                c.execute(f"UPDATE persons SET aktiv=0 WHERE id IN ({ph})", ph_params)
        get_writer().submit(_do, wait=True)
        flash(f"{len(ids)} Person(en) deaktiviert.", "success")

    elif action == "delete":
        import sqlite3 as _sq
        deleted = skipped = 0
        for pid in ids:
            def _do(c, _pid=pid):
                with write_tx(c):
                    c.execute("DELETE FROM persons WHERE id=?", (_pid,))
            try:
                get_writer().submit(_do, wait=True)
                deleted += 1
            except _sq.IntegrityError as exc:
                skipped += 1
                current_app.logger.info(
                    "Person %s nicht löschbar (FK-Constraint): %s", pid, exc
                )
            except _sq.DatabaseError as exc:
                skipped += 1
                current_app.logger.warning(
                    "Person %s: Datenbankfehler beim Löschen: %s", pid, exc
                )
        msg = f"{deleted} Person(en) gelöscht."
        if skipped:
            msg += f" {skipped} konnte(n) nicht gelöscht werden (noch IDVs zugeordnet) → bitte zuerst deaktivieren."
        flash(msg, "success" if not skipped else "warning")

    else:
        flash("Unbekannte Aktion.", "error")

    return redirect(url_for("admin.index"))


# ── Organisationseinheiten ─────────────────────────────────────────────────

@bp.route("/oe/neu", methods=["POST"])
@login_required
def new_oe():
    params = (
        request.form.get("bezeichnung", "").strip(),
        request.form.get("parent_id") or None,
        _now(),
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO org_units (bezeichnung, parent_id, created_at)
                VALUES (?,?,?)
            """, params)
    get_writer().submit(_do, wait=True)
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
        params = (
            request.form.get("bezeichnung", "").strip(),
            request.form.get("parent_id") or None,
            oid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE org_units SET bezeichnung=?, parent_id=?
                    WHERE id=?
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Organisationseinheit gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/oe_edit.html", oe=oe, all_oe=all_oe)


@bp.route("/oe/<int:oid>/loeschen", methods=["POST"])
@admin_required
def delete_oe(oid):
    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM org_units WHERE id=?", (oid,))
    get_writer().submit(_do, wait=True)
    flash("Organisationseinheit gelöscht.", "success")
    return redirect(url_for("admin.index"))


# ── Geschäftsprozesse ──────────────────────────────────────────────────────

@bp.route("/gp/neu", methods=["POST"])
@login_required
def new_gp():
    now = _now()
    params = (
        request.form.get("gp_nummer", "").strip(),
        request.form.get("bezeichnung", "").strip(),
        request.form.get("bereich") or None,
        1 if request.form.get("ist_kritisch") else 0,
        1 if request.form.get("ist_wesentlich") else 0,
        now, now,
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO geschaeftsprozesse
                  (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich, updated_at, created_at)
                VALUES (?,?,?,?,?,?,?)
            """, params)
    get_writer().submit(_do, wait=True)
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
        params = (
            request.form.get("gp_nummer", "").strip(),
            request.form.get("bezeichnung", "").strip(),
            1 if request.form.get("ist_kritisch") else 0,
            1 if request.form.get("ist_wesentlich") else 0,
            request.form.get("beschreibung") or None,
            request.form.get("schutzbedarf_a") or None,
            request.form.get("schutzbedarf_c") or None,
            request.form.get("schutzbedarf_i") or None,
            request.form.get("schutzbedarf_n") or None,
            1 if request.form.get("aktiv") else 0,
            _now(), gid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE geschaeftsprozesse SET
                        gp_nummer=?, bezeichnung=?, ist_kritisch=?, ist_wesentlich=?,
                        beschreibung=?,
                        schutzbedarf_a=?, schutzbedarf_c=?, schutzbedarf_i=?, schutzbedarf_n=?,
                        aktiv=?, updated_at=?
                    WHERE id=?
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Geschäftsprozess gespeichert.", "success")
        return redirect(url_for("admin.index"))

    org_units = db.execute("SELECT * FROM org_units ORDER BY bezeichnung").fetchall()
    return render_template("admin/gp_edit.html", gp=gp, org_units=org_units)


@bp.route("/gp/<int:gid>/loeschen", methods=["POST"])
@admin_required
def delete_gp(gid):
    def _do(c):
        with write_tx(c):
            c.execute("UPDATE geschaeftsprozesse SET aktiv=0 WHERE id=?", (gid,))
    get_writer().submit(_do, wait=True)
    flash("Geschäftsprozess deaktiviert.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/gp/alle-loeschen", methods=["POST"])
@admin_required
def delete_all_gp():
    """Löscht alle Geschäftsprozesse unwiderruflich.
    Verknüpfungen in idv_register.gp_id werden dabei auf NULL gesetzt."""
    def _do(c):
        with write_tx(c):
            c.execute("UPDATE idv_register SET gp_id=NULL WHERE gp_id IS NOT NULL")
            c.execute("DELETE FROM geschaeftsprozesse")
    get_writer().submit(_do, wait=True)
    flash("Alle Geschäftsprozesse wurden gelöscht.", "success")
    return redirect(url_for("admin.index") + "#geschaeftsprozesse")


@bp.route("/gps/bulk", methods=["POST"])
@admin_required
def bulk_gps():
    """Bulk-Aktion auf mehrere Geschäftsprozesse: deactivate oder delete."""
    db     = get_db()
    action = request.form.get("action", "")
    raw    = request.form.getlist("gp_ids")
    ids    = [int(i) for i in raw if i.isdigit()]

    if not ids:
        flash("Keine Geschäftsprozesse ausgewählt.", "warning")
        return redirect(url_for("admin.index") + "#geschaeftsprozesse")

    if action == "deactivate":
        ph, ph_params = in_clause(ids)
        def _do(c):
            with write_tx(c):
                c.execute(f"UPDATE geschaeftsprozesse SET aktiv=0 WHERE id IN ({ph})", ph_params)
        get_writer().submit(_do, wait=True)
        flash(f"{len(ids)} Geschäftsprozess(e) deaktiviert.", "success")

    elif action == "delete":
        import sqlite3 as _sq
        deleted = skipped = 0
        for gid in ids:
            def _do(c, _gid=gid):
                with write_tx(c):
                    c.execute("UPDATE idv_register SET gp_id=NULL WHERE gp_id=?", (_gid,))
                    c.execute("DELETE FROM geschaeftsprozesse WHERE id=?", (_gid,))
            try:
                get_writer().submit(_do, wait=True)
                deleted += 1
            except _sq.DatabaseError as exc:
                skipped += 1
                current_app.logger.warning(
                    "Geschäftsprozess %s nicht löschbar: %s", gid, exc
                )
        msg = f"{deleted} Geschäftsprozess(e) gelöscht."
        if skipped:
            msg += f" {skipped} konnte(n) nicht gelöscht werden."
        flash(msg, "success" if not skipped else "warning")

    else:
        flash("Unbekannte Aktion.", "error")

    return redirect(url_for("admin.index") + "#geschaeftsprozesse")


# ── Plattformen ────────────────────────────────────────────────────────────

@bp.route("/plattform/neu", methods=["POST"])
@login_required
def new_plattform():
    params = (
        request.form.get("bezeichnung", "").strip(),
        request.form.get("typ") or None,
        request.form.get("hersteller") or None,
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO plattformen (bezeichnung, typ, hersteller)
                VALUES (?,?,?)
            """, params)
    get_writer().submit(_do, wait=True)
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
        params = (
            request.form.get("bezeichnung", "").strip(),
            request.form.get("typ") or None,
            request.form.get("hersteller") or None,
            1 if request.form.get("aktiv") else 0,
            plid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE plattformen SET bezeichnung=?, typ=?, hersteller=?, aktiv=?
                    WHERE id=?
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Plattform gespeichert.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/plattform_edit.html", pl=pl)


@bp.route("/plattform/<int:plid>/loeschen", methods=["POST"])
@admin_required
def delete_plattform(plid):
    def _do(c):
        with write_tx(c):
            c.execute("UPDATE plattformen SET aktiv=0 WHERE id=?", (plid,))
    get_writer().submit(_do, wait=True)
    flash("Plattform deaktiviert.", "success")
    return redirect(url_for("admin.index"))

# ── Mitarbeiter-Import ─────────────────────────────────────────────────────

@bp.route("/import/personen", methods=["POST"])
@admin_required
@limiter.limit(_upload_rate_limit, methods=["POST"])
def import_persons():
    """CSV-Import: user_id, email (SMTP-Adresse), ad_name, oe_bezeichnung,
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
    now     = _now()

    prepared = []  # Liste von ("update", params) oder ("insert", params)
    errors = 0

    for row in reader:
        try:
            # Spalten-Aliase normalisieren (case-insensitive)
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}

            user_id       = r.get("user_id") or r.get("userid") or r.get("benutzername") or ""
            email         = r.get("email") or r.get("smtp") or r.get("smtp_adresse") or r.get("mailadresse") or ""
            ad_name       = r.get("ad_name") or r.get("adname") or r.get("ad") or ""
            oe_bezeichnung = r.get("oe_bezeichnung") or r.get("oe") or r.get("abteilung") or ""
            nachname      = r.get("nachname") or r.get("name") or ""
            vorname       = r.get("vorname") or ""
            kuerzel       = (r.get("kuerzel") or user_id[:3]).upper()
            rolle         = r.get("rolle") or "Fachverantwortlicher"

            if not (nachname or user_id):
                errors += 1
                continue

            org_unit_id = None
            if oe_bezeichnung:
                oe_row = db.execute(
                    "SELECT id FROM org_units WHERE LOWER(bezeichnung)=LOWER(?)", (oe_bezeichnung,)
                ).fetchone()
                if oe_row:
                    org_unit_id = oe_row["id"]

            existing = None
            if user_id:
                existing = db.execute("SELECT id FROM persons WHERE user_id=?", (user_id,)).fetchone()
            if not existing and kuerzel:
                existing = db.execute("SELECT id FROM persons WHERE kuerzel=?", (kuerzel,)).fetchone()

            if existing:
                prepared.append(("update", (email, ad_name, org_unit_id, user_id, rolle, existing["id"])))
            else:
                prepared.append(("insert", (kuerzel, nachname, vorname, email or None, rolle,
                                            org_unit_id, user_id or None, ad_name or None, now)))
        except Exception:
            errors += 1

    def _do(c):
        created = updated = 0
        with write_tx(c):
            for op, params in prepared:
                if op == "update":
                    c.execute("""
                        UPDATE persons SET
                            email=COALESCE(NULLIF(?,''), email),
                            ad_name=COALESCE(NULLIF(?,''), ad_name),
                            org_unit_id=COALESCE(?,org_unit_id),
                            user_id=COALESCE(NULLIF(?,''), user_id),
                            rolle=COALESCE(NULLIF(?,''), rolle)
                        WHERE id=?
                    """, params)
                    updated += 1
                else:
                    c.execute("""
                        INSERT INTO persons
                            (kuerzel, nachname, vorname, email, rolle, org_unit_id,
                             user_id, ad_name, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """, params)
                    created += 1
        return created, updated
    created, updated = get_writer().submit(_do, wait=True)
    flash(f"Import abgeschlossen: {created} neu, {updated} aktualisiert, {errors} Fehler.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/import/vorlage")
@login_required
def import_template():
    """CSV-Vorlage herunterladen."""
    content = "user_id;email;ad_name;oe_bezeichnung;nachname;vorname;kuerzel;rolle\n"
    content += "mmu;max.mustermann@bank.de;DOMAIN\\mmu;Kreditabteilung;Mustermann;Max;MMU;Fachverantwortlicher\n"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mitarbeiter_vorlage.csv"}
    )
# ── Klassifizierungen ──────────────────────────────────────────────────────

@bp.route("/klassifizierungen/<bereich>/neu", methods=["POST"])
@login_required
def new_klassifizierung(bereich):
    db  = get_db()
    wert = request.form.get("wert", "").strip()
    if not wert:
        flash("Wert darf nicht leer sein.", "error")
        return redirect(url_for("admin.index") + f"#klass-{bereich}")

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM klassifizierungen WHERE bereich=?", (bereich,)
    ).fetchone()[0]

    params = (
        bereich,
        wert,
        request.form.get("bezeichnung") or None,
        request.form.get("beschreibung") or None,
        max_order + 1,
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO klassifizierungen (bereich, wert, bezeichnung, beschreibung, sort_order, aktiv)
                VALUES (?,?,?,?,?,1)
                ON CONFLICT(bereich, wert) DO UPDATE SET
                    bezeichnung=excluded.bezeichnung,
                    beschreibung=excluded.beschreibung,
                    aktiv=1
            """, params)
    get_writer().submit(_do, wait=True)
    flash(f"Eintrag '{wert}' in '{bereich}' angelegt.", "success")
    return redirect(url_for("admin.index") + f"#klass-{bereich}")


@bp.route("/klassifizierungen/<int:kid>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit_klassifizierung(kid):
    db  = get_db()
    row = db.execute("SELECT * FROM klassifizierungen WHERE id=?", (kid,)).fetchone()
    if not row:
        flash("Eintrag nicht gefunden.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        params = (
            request.form.get("wert", "").strip(),
            request.form.get("bezeichnung") or None,
            request.form.get("beschreibung") or None,
            int(request.form.get("sort_order", row["sort_order"])),
            1 if request.form.get("aktiv") else 0,
            kid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE klassifizierungen
                    SET wert=?, bezeichnung=?, beschreibung=?, sort_order=?, aktiv=?
                    WHERE id=?
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Eintrag gespeichert.", "success")
        return redirect(url_for("admin.index") + f"#klass-{row['bereich']}")

    bereich_label = dict(_KLASSIFIZIERUNGS_BEREICHE).get(row["bereich"], row["bereich"])
    return render_template("admin/klassifizierung_edit.html",
                           row=row, bereich_label=bereich_label)


@bp.route("/klassifizierungen/<int:kid>/loeschen", methods=["POST"])
@admin_required
def delete_klassifizierung(kid):
    db  = get_db()
    row = db.execute("SELECT bereich FROM klassifizierungen WHERE id=?", (kid,)).fetchone()
    def _do(c):
        with write_tx(c):
            c.execute("UPDATE klassifizierungen SET aktiv=0 WHERE id=?", (kid,))
    get_writer().submit(_do, wait=True)
    flash("Eintrag deaktiviert.", "success")
    bereich = row["bereich"] if row else ""
    return redirect(url_for("admin.index") + f"#klass-{bereich}")


# ── Wesentlichkeitskriterien ───────────────────────────────────────────────

@bp.route("/wesentlichkeit/neu", methods=["POST"])
@admin_required
def new_wesentlichkeitskriterium():
    db = get_db()
    bezeichnung = request.form.get("bezeichnung", "").strip()
    if not bezeichnung:
        flash("Bezeichnung darf nicht leer sein.", "error")
        return redirect(url_for("admin.index") + "#wesentlichkeit")

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM wesentlichkeitskriterien"
    ).fetchone()[0]

    params = (
        bezeichnung,
        request.form.get("beschreibung") or None,
        1 if request.form.get("begruendung_pflicht") else 0,
        max_order + 1,
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO wesentlichkeitskriterien
                    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
                VALUES (?, ?, ?, ?, 1)
            """, params)
    get_writer().submit(_do, wait=True)
    flash(f"Kriterium '{bezeichnung}' angelegt.", "success")
    return redirect(url_for("admin.index") + "#wesentlichkeit")


@bp.route("/wesentlichkeit/<int:kid>/bearbeiten", methods=["GET", "POST"])
@admin_required
def edit_wesentlichkeitskriterium(kid):
    db  = get_db()
    row = db.execute("SELECT * FROM wesentlichkeitskriterien WHERE id=?", (kid,)).fetchone()
    if not row:
        flash("Kriterium nicht gefunden.", "error")
        return redirect(url_for("admin.index") + "#wesentlichkeit")

    if request.method == "POST":
        params = (
            request.form.get("bezeichnung", "").strip(),
            request.form.get("beschreibung") or None,
            1 if request.form.get("begruendung_pflicht") else 0,
            int(request.form.get("sort_order", row["sort_order"])),
            1 if request.form.get("aktiv") else 0,
            kid,
        )
        def _do(c):
            with write_tx(c):
                c.execute("""
                    UPDATE wesentlichkeitskriterien
                    SET bezeichnung=?, beschreibung=?, begruendung_pflicht=?, sort_order=?, aktiv=?
                    WHERE id=?
                """, params)
        get_writer().submit(_do, wait=True)
        flash("Kriterium gespeichert.", "success")
        return redirect(url_for("admin.edit_wesentlichkeitskriterium", kid=kid))

    details = db.execute("""
        SELECT id, bezeichnung, sort_order, aktiv
        FROM wesentlichkeitskriterium_details
        WHERE kriterium_id=?
        ORDER BY sort_order, id
    """, (kid,)).fetchall()
    return render_template("admin/wesentlichkeit_edit.html", row=row, details=details)


@bp.route("/wesentlichkeit/<int:kid>/loeschen", methods=["POST"])
@admin_required
def delete_wesentlichkeitskriterium(kid):
    db = get_db()
    in_use = db.execute(
        "SELECT 1 FROM idv_wesentlichkeit WHERE kriterium_id=? LIMIT 1", (kid,)
    ).fetchone()
    if in_use:
        def _do(c):
            with write_tx(c):
                c.execute("UPDATE wesentlichkeitskriterien SET aktiv=0 WHERE id=?", (kid,))
        get_writer().submit(_do, wait=True)
        flash("Kriterium deaktiviert. Vorhandene Antworten bleiben erhalten.", "success")
    else:
        def _do(c):
            with write_tx(c):
                c.execute("DELETE FROM wesentlichkeitskriterien WHERE id=?", (kid,))
        get_writer().submit(_do, wait=True)
        flash("Kriterium gelöscht.", "success")
    return redirect(url_for("admin.index") + "#wesentlichkeit")


# ── Details (Checkbox-Optionen) zu einem Kriterium ──────────────────────────

@bp.route("/wesentlichkeit/<int:kid>/detail/neu", methods=["POST"])
@admin_required
def new_wesentlichkeit_detail(kid):
    db = get_db()
    if not db.execute("SELECT 1 FROM wesentlichkeitskriterien WHERE id=?", (kid,)).fetchone():
        flash("Kriterium nicht gefunden.", "error")
        return redirect(url_for("admin.index") + "#wesentlichkeit")

    bezeichnung = request.form.get("bezeichnung", "").strip()
    if not bezeichnung:
        flash("Bezeichnung darf nicht leer sein.", "error")
        return redirect(url_for("admin.edit_wesentlichkeitskriterium", kid=kid))

    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM wesentlichkeitskriterium_details WHERE kriterium_id=?",
        (kid,),
    ).fetchone()[0]
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO wesentlichkeitskriterium_details
                    (kriterium_id, bezeichnung, sort_order, aktiv)
                VALUES (?, ?, ?, 1)
            """, (kid, bezeichnung, max_order + 1))
    try:
        get_writer().submit(_do, wait=True)
        flash(f"Detail '{bezeichnung}' hinzugefügt.", "success")
    except Exception as exc:
        flash(f"Detail konnte nicht angelegt werden: {exc}", "error")
    return redirect(url_for("admin.edit_wesentlichkeitskriterium", kid=kid))


@bp.route("/wesentlichkeit/<int:kid>/detail/<int:did>/bearbeiten", methods=["POST"])
@admin_required
def edit_wesentlichkeit_detail(kid, did):
    db = get_db()
    bezeichnung = request.form.get("bezeichnung", "").strip()
    if not bezeichnung:
        flash("Bezeichnung darf nicht leer sein.", "error")
        return redirect(url_for("admin.edit_wesentlichkeitskriterium", kid=kid))
    params = (
        bezeichnung,
        int(request.form.get("sort_order") or 0),
        1 if request.form.get("aktiv") else 0,
        did, kid,
    )
    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE wesentlichkeitskriterium_details
                SET bezeichnung=?, sort_order=?, aktiv=?
                WHERE id=? AND kriterium_id=?
            """, params)
    get_writer().submit(_do, wait=True)
    flash("Detail gespeichert.", "success")
    return redirect(url_for("admin.edit_wesentlichkeitskriterium", kid=kid))


@bp.route("/wesentlichkeit/<int:kid>/detail/<int:did>/loeschen", methods=["POST"])
@admin_required
def delete_wesentlichkeit_detail(kid, did):
    def _do(c):
        with write_tx(c):
            c.execute(
                "UPDATE wesentlichkeitskriterium_details SET aktiv=0 WHERE id=? AND kriterium_id=?",
                (did, kid),
            )
    get_writer().submit(_do, wait=True)
    flash("Detail deaktiviert. Vorhandene Antworten bleiben erhalten.", "success")
    return redirect(url_for("admin.edit_wesentlichkeitskriterium", kid=kid))
# ── Geschäftsprozess-Import ────────────────────────────────────────────────

@bp.route("/import/geschaeftsprozesse/vorlage")
@login_required
def import_gp_template():
    """CSV-Vorlage für GP-Import herunterladen."""
    content  = "gp_nummer;bezeichnung;bereich;ist_kritisch;ist_wesentlich;beschreibung\n"
    content += "GP-XXX-001;Mein Prozess;Steuerung;1;1;Kurzbeschreibung\n"
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=geschaeftsprozesse_vorlage.csv"}
    )


@bp.route("/import/geschaeftsprozesse", methods=["POST"])
@admin_required
@limiter.limit(_upload_rate_limit, methods=["POST"])
def import_geschaeftsprozesse():
    """
    CSV-Import für Geschäftsprozesse – zwei Formate werden unterstützt:

    Prozess-Export-Format (Spalten):
        Nummer; Prozess_ID; Prozess_Titel; Beschreibung; Ebene; Zustand; Herkunft;
        Version; Prozesswesentlichkeit; Zeitkritikalitaet; Schutzbedarf_A;
        Schutzbedarf_C; Schutzbedarf_I; Schutzbedarf_N; Kritisch_Wichtig;
        Begründung_Kritisch_Wichtig; RTO; RPO; Auswirkung_Unterbrechung;
        Vorgaenger; Nummer_Bestandsprozess; Kommentare
    Upsert-Schlüssel: Prozess_ID → gp_nummer

    Standard-Format (Spalten):
        gp_nummer; bezeichnung; bereich; ist_kritisch (0/1); ist_wesentlich (0/1);
        beschreibung
    Upsert-Schlüssel: gp_nummer
    """
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Keine Datei ausgewählt.", "error")
        return redirect(url_for("admin.index") + "#geschaeftsprozesse")

    db      = get_db()
    content = f.read().decode("utf-8-sig")
    first_line = content.split("\n")[0]
    delimiter  = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader     = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    errors = 0
    now     = _now()

    # Format-Erkennung anhand der Header-Zeile
    raw_fields   = [k.strip() for k in (reader.fieldnames or []) if k and k.strip()]
    fields_lower = [f.lower() for f in raw_fields]
    is_prozess_export = "prozess_id" in fields_lower

    prepared = []   # Liste von ("update"|"insert", "prozess"|"standard", params)

    if is_prozess_export:
        for row in reader:
            try:
                r = {k.strip(): (v or "").strip() for k, v in row.items() if k and k.strip()}

                gp_nummer   = r.get("Prozess_ID", "").strip()
                bezeichnung = r.get("Prozess_Titel", "").strip()
                if not gp_nummer or not bezeichnung:
                    errors += 1
                    continue

                beschreibung   = r.get("Beschreibung") or None
                wesentl_raw    = r.get("Prozesswesentlichkeit", "").strip().lower()
                ist_wesentlich = 1 if wesentl_raw == "wesentlich" else 0
                kritisch_raw   = r.get("Kritisch_Wichtig", "Nein").strip().lower()
                ist_kritisch   = 1 if kritisch_raw == "ja" else 0
                sb_a = r.get("Schutzbedarf_A") or None
                sb_c = r.get("Schutzbedarf_C") or None
                sb_i = r.get("Schutzbedarf_I") or None
                sb_n = r.get("Schutzbedarf_N") or None

                existing = db.execute(
                    "SELECT id FROM geschaeftsprozesse WHERE gp_nummer=?", (gp_nummer,)
                ).fetchone()

                if existing:
                    prepared.append(("update", "prozess", (
                        bezeichnung, beschreibung,
                        ist_kritisch, ist_wesentlich,
                        sb_a, sb_c, sb_i, sb_n,
                        now, gp_nummer,
                    )))
                else:
                    prepared.append(("insert", "prozess", (
                        gp_nummer, bezeichnung, beschreibung,
                        ist_kritisch, ist_wesentlich,
                        sb_a, sb_c, sb_i, sb_n,
                        now, now,
                    )))
            except Exception:
                errors += 1

    else:
        for row in reader:
            try:
                r = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
                gp_nummer   = r.get("gp_nummer", "").strip()
                bezeichnung = r.get("bezeichnung", "").strip()
                if not gp_nummer or not bezeichnung:
                    errors += 1
                    continue

                bereich        = r.get("bereich") or None
                ist_kritisch   = 1 if r.get("ist_kritisch", "0") in ("1", "ja", "true", "x") else 0
                ist_wesentlich = 1 if r.get("ist_wesentlich", "0") in ("1", "ja", "true", "x") else 0
                beschreibung   = r.get("beschreibung") or None

                existing = db.execute(
                    "SELECT id FROM geschaeftsprozesse WHERE gp_nummer=?", (gp_nummer,)
                ).fetchone()

                if existing:
                    prepared.append(("update", "standard", (
                        bezeichnung, bereich, ist_kritisch, ist_wesentlich,
                        beschreibung, now, gp_nummer,
                    )))
                else:
                    prepared.append(("insert", "standard", (
                        gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich,
                        beschreibung, now, now,
                    )))
            except Exception:
                errors += 1

    def _do(c):
        created = updated = 0
        with write_tx(c):
            for op, fmt, params in prepared:
                if op == "update" and fmt == "prozess":
                    c.execute("""
                        UPDATE geschaeftsprozesse
                        SET bezeichnung=?,
                            beschreibung=COALESCE(?,beschreibung),
                            ist_kritisch=?,
                            ist_wesentlich=?,
                            schutzbedarf_a=COALESCE(?,schutzbedarf_a),
                            schutzbedarf_c=COALESCE(?,schutzbedarf_c),
                            schutzbedarf_i=COALESCE(?,schutzbedarf_i),
                            schutzbedarf_n=COALESCE(?,schutzbedarf_n),
                            aktiv=1,
                            updated_at=?
                        WHERE gp_nummer=?
                    """, params)
                    updated += 1
                elif op == "insert" and fmt == "prozess":
                    c.execute("""
                        INSERT INTO geschaeftsprozesse
                            (gp_nummer, bezeichnung, beschreibung,
                             ist_kritisch, ist_wesentlich,
                             schutzbedarf_a, schutzbedarf_c, schutzbedarf_i, schutzbedarf_n,
                             created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, params)
                    created += 1
                elif op == "update" and fmt == "standard":
                    c.execute("""
                        UPDATE geschaeftsprozesse
                        SET bezeichnung=?, bereich=COALESCE(?,bereich),
                            ist_kritisch=?, ist_wesentlich=?,
                            beschreibung=COALESCE(?,beschreibung),
                            aktiv=1, updated_at=?
                        WHERE gp_nummer=?
                    """, params)
                    updated += 1
                else:
                    c.execute("""
                        INSERT INTO geschaeftsprozesse
                            (gp_nummer, bezeichnung, bereich, ist_kritisch, ist_wesentlich,
                             beschreibung, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, params)
                    created += 1
        return created, updated
    created, updated = get_writer().submit(_do, wait=True)
    flash(f"GP-Import: {created} neu, {updated} aktualisiert, {errors} Fehler.", "success")
    return redirect(url_for("admin.index") + "#geschaeftsprozesse")
