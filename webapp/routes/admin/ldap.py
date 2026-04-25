"""Admin-Sub-Modul: LDAP-Konfiguration, Gruppen-Mapping, LDAP-Import."""
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash, jsonify, current_app

from db_write_tx import write_tx

from .. import admin_required, get_db
from ...db_writer import get_writer
from ...ldap_auth import ldap_ssl_verify_effective
from . import bp


_LDAP_ROLLEN = [
    "IDV-Administrator",
    "IDV-Koordinator",
    "Fachverantwortlicher",
    "IT-Sicherheit",
    "Revision",
]


@bp.route("/ldap-config", methods=["GET", "POST"])
@admin_required
def ldap_config():
    from ...ldap_auth import get_ldap_config, encrypt_password
    db = get_db()

    if request.method == "POST":
        db_row = db.execute("SELECT * FROM ldap_config WHERE id = 1").fetchone()
        db_cfg = dict(db_row) if db_row else {}

        enabled = 1 if request.form.get("enabled") else 0
        server_url = request.form.get("server_url", "").strip()
        try:
            port = int(request.form.get("port") or 636)
        except (TypeError, ValueError):
            port = 636
        base_dn = request.form.get("base_dn", "").strip()
        bind_dn = request.form.get("bind_dn", "").strip()
        user_attr = request.form.get("user_attr", "sAMAccountName")
        # #403: ``ssl_verify`` ist nicht mehr ueber die UI deaktivierbar.
        # Der Wert in der DB-Zeile wird zwar weiterhin gepflegt (Bestand),
        # die effektive Pruefung beim Bind kommt aus
        # ``ldap_ssl_verify_effective`` und kann nur ueber den
        # config.json-Override ``IDV_LDAP_INSECURE_TLS=1`` deaktiviert
        # werden. Die UI darf den Wert deshalb nicht mehr stillschweigend
        # auf 0 senken.
        ssl_verify = 1

        bind_password_plain = request.form.get("bind_password", "").strip()
        if bind_password_plain:
            secret_key = current_app.config["SECRET_KEY"]
            bind_password_enc = encrypt_password(bind_password_plain, secret_key)
        else:
            bind_password_enc = db_cfg.get("bind_password", "")

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        params = (enabled, server_url, port, base_dn, bind_dn,
                  bind_password_enc, user_attr, ssl_verify, updated_at)
        def _do(c):
            with write_tx(c):
                c.execute("""
                    INSERT INTO ldap_config
                        (id, enabled, server_url, port, base_dn, bind_dn,
                         bind_password, user_attr, ssl_verify, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        enabled=excluded.enabled,
                        server_url=excluded.server_url,
                        port=excluded.port,
                        base_dn=excluded.base_dn,
                        bind_dn=excluded.bind_dn,
                        bind_password=excluded.bind_password,
                        user_attr=excluded.user_attr,
                        ssl_verify=excluded.ssl_verify,
                        updated_at=excluded.updated_at
                """, params)
        get_writer().submit(_do, wait=True)
        flash("LDAP-Konfiguration gespeichert.", "success")
        # #403: Wenn der config.json-Override ``IDV_LDAP_INSECURE_TLS=1``
        # die TLS-Pruefung deaktiviert, weisen wir den Admin nach jeder
        # Speicherung erneut darauf hin – das ist nur fuer Pilotbetriebe
        # mit Self-signed-CA gedacht und sollte sichtbar bleiben.
        if enabled and not ldap_ssl_verify_effective():
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "LDAP-Konfiguration gespeichert; TLS-Zertifikatspruefung "
                "ist via config.json-Override (IDV_LDAP_INSECURE_TLS=1) "
                "deaktiviert – Man-in-the-Middle-Angriffe auf LDAPS moeglich."
            )
            flash(
                "Hinweis: TLS-Zertifikatspruefung ist via "
                "config.json-Override IDV_LDAP_INSECURE_TLS=1 deaktiviert. "
                "Im Produktivbetrieb bitte deaktivieren (Eintrag entfernen "
                "oder auf 0 setzen) und das Server-Zertifikat aus der "
                "internen CA als vertrauenswuerdig hinterlegen.",
                "warning",
            )
        return redirect(url_for("admin.ldap_config"))

    cfg = get_ldap_config(db)
    return render_template(
        "admin/ldap_config.html",
        cfg=cfg,
        ssl_verify_effective=ldap_ssl_verify_effective(),
    )


@bp.route("/ldap-test", methods=["POST"])
@admin_required
def ldap_test():
    from ...ldap_auth import get_ldap_config, ldap_test_connection
    db = get_db()
    cfg = get_ldap_config(db)
    if not cfg or not cfg["server_url"]:
        return jsonify(ok=False, msg="Keine LDAP-Konfiguration gespeichert.")
    secret_key = current_app.config["SECRET_KEY"]
    ok, msg = ldap_test_connection(dict(cfg), secret_key)
    return jsonify(ok=ok, msg=msg)


@bp.route("/ldap-gruppen", methods=["GET"])
@admin_required
def ldap_gruppen():
    db = get_db()
    mappings = db.execute(
        "SELECT * FROM ldap_group_role_mapping ORDER BY sort_order, id"
    ).fetchall()
    return render_template("admin/ldap_gruppen.html",
                           mappings=mappings, rollen=_LDAP_ROLLEN)


@bp.route("/ldap-gruppe/neu", methods=["POST"])
@admin_required
def ldap_gruppe_neu():
    db = get_db()
    group_dn   = request.form.get("group_dn", "").strip()
    group_name = request.form.get("group_name", "").strip()
    rolle      = request.form.get("rolle", "").strip()
    sort_order = int(request.form.get("sort_order") or 99)

    if not group_dn or not rolle:
        flash("Gruppen-DN und Rolle sind Pflichtfelder.", "danger")
        return redirect(url_for("admin.ldap_gruppen"))

    params = (group_dn, group_name or None, rolle, sort_order)
    def _do(c):
        with write_tx(c):
            c.execute("""
                INSERT INTO ldap_group_role_mapping (group_dn, group_name, rolle, sort_order)
                VALUES (?, ?, ?, ?)
            """, params)
    try:
        get_writer().submit(_do, wait=True)
        flash("Gruppen-Mapping angelegt.", "success")
    except Exception:
        flash("Fehler: Gruppen-DN ist bereits vorhanden.", "danger")
    return redirect(url_for("admin.ldap_gruppen"))


@bp.route("/ldap-gruppe/<int:mid>/bearbeiten", methods=["POST"])
@admin_required
def ldap_gruppe_bearbeiten(mid):
    db = get_db()
    group_dn   = request.form.get("group_dn", "").strip()
    group_name = request.form.get("group_name", "").strip()
    rolle      = request.form.get("rolle", "").strip()
    sort_order = int(request.form.get("sort_order") or 99)

    if not group_dn or not rolle:
        flash("Gruppen-DN und Rolle sind Pflichtfelder.", "danger")
        return redirect(url_for("admin.ldap_gruppen"))

    params = (group_dn, group_name or None, rolle, sort_order, mid)
    def _do(c):
        with write_tx(c):
            c.execute("""
                UPDATE ldap_group_role_mapping
                SET group_dn=?, group_name=?, rolle=?, sort_order=?
                WHERE id=?
            """, params)
    get_writer().submit(_do, wait=True)
    flash("Gruppen-Mapping aktualisiert.", "success")
    return redirect(url_for("admin.ldap_gruppen"))


@bp.route("/ldap-gruppe/<int:mid>/loeschen", methods=["POST"])
@admin_required
def ldap_gruppe_loeschen(mid):
    def _do(c):
        with write_tx(c):
            c.execute("DELETE FROM ldap_group_role_mapping WHERE id=?", (mid,))
    get_writer().submit(_do, wait=True)
    flash("Gruppen-Mapping gelöscht.", "success")
    return redirect(url_for("admin.ldap_gruppen"))


@bp.route("/ldap-import", methods=["GET", "POST"])
@admin_required
def ldap_import():
    from ...ldap_auth import get_ldap_config, ldap_list_users, ldap_sync_person
    db = get_db()
    cfg = get_ldap_config(db)
    secret_key = current_app.config["SECRET_KEY"]

    if request.method == "POST":
        action       = request.form.get("action", "import")
        selected_ids = request.form.getlist("user_ids")

        if not selected_ids:
            flash("Keine Benutzer ausgewählt.", "warning")
            return redirect(url_for("admin.ldap_import"))

        # ── Aktion: Löschen (Deaktivieren) ───────────────────────────────────
        if action == "delete":
            deactivated = skipped = 0
            ids_to_deactivate = []
            for uid in selected_ids:
                row = db.execute(
                    "SELECT id FROM persons WHERE ad_name=? OR user_id=?", (uid, uid)
                ).fetchone()
                if row:
                    ids_to_deactivate.append(row["id"])
                    deactivated += 1
                else:
                    skipped += 1
            if ids_to_deactivate:
                def _do(c, _ids=tuple(ids_to_deactivate)):
                    with write_tx(c):
                        for _pid in _ids:
                            c.execute("UPDATE persons SET aktiv=0 WHERE id=?", (_pid,))
                get_writer().submit(_do, wait=True)
            msg = f"{deactivated} Person(en) deaktiviert."
            if skipped:
                msg += f" {skipped} nicht gefunden (noch nicht importiert)."
            flash(msg, "success" if deactivated else "warning")
            return redirect(url_for("admin.ldap_import"))

        # ── Aktion: Importieren ───────────────────────────────────────────────
        extra_filter = request.form.get("extra_filter", "").strip()
        ok, msg, users = ldap_list_users(db, secret_key, extra_filter)
        if not ok:
            flash(f"LDAP-Fehler: {msg}", "danger")
            return redirect(url_for("admin.ldap_import"))

        selected_set = set(selected_ids)
        neu = geaendert = 0
        for u in users:
            if u["user_id"] not in selected_set:
                continue
            existing = db.execute(
                "SELECT id FROM persons WHERE ad_name=? OR user_id=?",
                (u["user_id"], u["user_id"])
            ).fetchone()
            ldap_sync_person(db, u)
            if existing:
                geaendert += 1
            else:
                neu += 1

        flash(f"Import abgeschlossen: {neu} neu angelegt, {geaendert} aktualisiert.", "success")
        return redirect(url_for("admin.ldap_import"))

    # GET: LDAP-Benutzer laden und Vorschau zeigen
    extra_filter = request.args.get("extra_filter", "").strip()
    if not cfg or not cfg["server_url"]:
        users = []
        ldap_msg = "LDAP nicht konfiguriert. Bitte zuerst die LDAP-Konfiguration einrichten."
        ldap_ok  = False
    else:
        ldap_ok, ldap_msg, users = ldap_list_users(db, secret_key, extra_filter)

    # Vorhandene user_ids für Markierung im UI
    existing_ids = {
        r["user_id"] for r in db.execute(
            "SELECT user_id FROM persons WHERE user_id IS NOT NULL AND user_id != ''"
        ).fetchall()
    }

    return render_template("admin/ldap_import.html",
                           cfg=cfg, users=users, ldap_ok=ldap_ok, ldap_msg=ldap_msg,
                           extra_filter=extra_filter, existing_ids=existing_ids,
                           rollen=_LDAP_ROLLEN)
