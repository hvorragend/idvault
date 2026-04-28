"""Admin-Sub-Modul: SMTP-Konfiguration, E-Mail-Templates, Test-Versand."""
import re

from flask import render_template, request, redirect, url_for, flash, jsonify

from db_write_tx import write_tx

from .. import admin_required, get_db
from ...db_writer import get_writer
from . import bp, _encrypt_smtp_password


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("/mail", methods=["GET", "POST"])
@admin_required
def mail():
    db = get_db()
    if request.method == "POST":
        # VULN-007: SMTP-Passwort gesondert behandeln (Fernet-Verschlüsselung)
        from ...email_service import EMAIL_TEMPLATES, encrypt_smtp_password
        smtp_pw_enc = _encrypt_smtp_password(
            request.form.get("smtp_password", ""), encrypt_smtp_password
        )

        keys = ["smtp_host", "smtp_port", "smtp_user",
                "smtp_from", "smtp_tls", "app_base_url",
                "notify_schedule_enabled", "notify_schedule_time",
                "self_service_enabled", "self_service_frequency_days",
                "owner_digest_burst_threshold",
                "quick_action_freigabe_enabled",
                "silent_release_enabled",
                "escalation_reminder_days",
                "escalation_to_lead_days",
                "escalation_to_coordinator_days"]
        for tpl_key in EMAIL_TEMPLATES:
            keys.append(f"notify_enabled_{tpl_key}")
            keys.append(f"email_tpl_{tpl_key}_subject")
            keys.append(f"email_tpl_{tpl_key}_body")
            keys.append(f"email_tpl_{tpl_key}_mode")
        kv = [
            (k, request.form.get(k, "").replace("\r\n", "\n").replace("\r", "\n")
                if k.endswith("_body") else request.form.get(k, ""))
            for k in keys
        ]
        if smtp_pw_enc is not None:
            kv.append(("smtp_password", smtp_pw_enc))
        def _do(c):
            with write_tx(c):
                for _k, _v in kv:
                    c.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?,?)",
                              (_k, _v))
        get_writer().submit(_do, wait=True)
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("admin.mail") + "#email-vorlagen")

    settings = {
        k: v.replace("\r\n", "\n").replace("\r", "\n") if k.endswith("_body") else v
        for k, v in (
            (r["key"], r["value"])
            for r in db.execute("SELECT key, value FROM app_settings").fetchall()
        )
    }
    smtp_log  = db.execute(
        "SELECT sent_at, recipients, subject, success, error_msg "
        "FROM smtp_log ORDER BY id DESC LIMIT 50"
    ).fetchall()
    from ...email_service import EMAIL_TEMPLATES as _email_tpls, _DEFAULTS as _email_defaults, _strip_html_tags
    email_defaults_text = {
        k: (_strip_html_tags(v) if k.endswith("_body") else v)
        for k, v in _email_defaults.items()
    }
    return render_template("admin/mail.html",
        settings=settings,
        email_templates=_email_tpls,
        email_defaults=_email_defaults,
        email_defaults_text=email_defaults_text,
        smtp_log=smtp_log)


@bp.route("/mail/test", methods=["POST"])
@admin_required
def mail_test():
    """AJAX-Endpunkt: Sendet eine Test-E-Mail und gibt JSON zurück.

    Liest die SMTP-Felder aus dem POST-Body (aktuelle Formularwerte),
    sodass der Test auch mit noch nicht gespeicherten Einstellungen funktioniert.
    Leeres Passwort-Feld bedeutet: gespeichertes DB-Passwort verwenden.
    """
    from ...email_service import send_smtp_test
    db       = get_db()
    to_email = request.form.get("to_email", "").strip()

    f_host  = request.form.get("smtp_host", "").strip() or None
    f_port  = request.form.get("smtp_port", "").strip()
    f_user  = request.form.get("smtp_user", "").strip()  # leer = kein Auth
    f_pw    = request.form.get("smtp_password", "")      # leer = DB-Wert behalten
    f_from  = request.form.get("smtp_from", "").strip() or None
    f_tls   = request.form.get("smtp_tls", None)         # 'starttls'|'ssl'|'none'

    ok, msg = send_smtp_test(
        db, to_email,
        host      = f_host,
        port      = int(f_port) if f_port else None,
        user      = f_user,
        password  = f_pw if f_pw else None,
        smtp_from = f_from,
        tls_mode  = f_tls if f_tls in ("starttls", "ssl", "none") else None,
    )
    return jsonify({"success": ok, "message": msg})


@bp.route("/owner-digest/run", methods=["POST"])
@admin_required
def owner_digest_run():
    """Manueller Versand der Owner-Sammelbenachrichtigung aus dem Admin-UI.

    Modi:
      * ``test`` – Testversand an eine vom Admin angegebene Adresse
        (max. 3 Empfänger pro Klick), keine Token, keine
        ``notification_log``-Einträge, ignoriert Master-Switch und
        Dedup-Gates.
      * ``live`` – echter Sofortversand an die regulären Empfänger;
        ignoriert Tageslimit und Intervall-Dedup, schreibt
        ``notification_log`` wie ein regulärer Lauf. Setzt voraus, dass
        Self-Service aktiviert ist.

    Antwort: JSON mit ``success``, ``message``, ``sent``, ``candidates``.
    """
    from datetime import datetime
    from ...notification_scheduler import _dispatch_owner_digest

    db   = get_db()
    mode = request.form.get("mode", "").strip()
    if mode not in ("test", "live"):
        return jsonify({"success": False, "message": "Ungültiger Modus."}), 400

    if mode == "test":
        to_email = request.form.get("test_recipient", "").strip()
        if not to_email or not _EMAIL_RE.match(to_email):
            return jsonify({
                "success": False,
                "message": "Bitte eine gültige Empfänger-Adresse angeben.",
            }), 400

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if mode == "test":
            result = _dispatch_owner_digest(
                db, today, test_recipient=to_email,
            )
        else:
            result = _dispatch_owner_digest(db, today, force=True)
    except Exception as exc:
        from flask import current_app
        current_app.logger.exception("Manueller Owner-Digest-Versand fehlgeschlagen")
        return jsonify({
            "success": False,
            "message": f"Fehler beim Versand: {exc}",
        }), 500

    sent       = int(result.get("sent", 0))
    candidates = result.get("candidates", [])
    skipped    = int(result.get("skipped_test_limit", 0))

    if mode == "test":
        if sent == 0 and not candidates:
            msg = "Keine offenen Funde – nichts zu senden."
        elif sent == 0:
            msg = "Versand fehlgeschlagen (siehe SMTP-Versandlog)."
        else:
            msg = f"{sent} Test-Mail(s) an {to_email} gesendet."
            if skipped:
                msg += f" {skipped} weitere Empfänger wegen Test-Limit übersprungen."
    else:
        if sent == 0 and not candidates:
            msg = "Keine offenen Funde – nichts zu senden."
        elif sent == 0:
            msg = "Versand fehlgeschlagen (siehe SMTP-Versandlog)."
        else:
            msg = f"{sent} Sammelbenachrichtigung(en) versendet."

    return jsonify({
        "success":    sent > 0 or not candidates,
        "message":    msg,
        "sent":       sent,
        "skipped":    skipped,
        "candidates": candidates,
    })
