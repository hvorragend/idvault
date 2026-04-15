"""
idvault – Flask Web Application
================================
IDV-Register für MaRisk AT 7.2 / DORA-konforme Verwaltung
von Eigenentwicklungen (Individuelle Datenverarbeitung).
"""

import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Flask
from .db_flask import get_db, close_db, init_app_db


# Hinweis zu Sicherheits-Remediationen (VULN-004/005/008/013):
# Die folgenden Konstanten werden in create_app() ausgewertet.
_DEFAULT_DEV_SECRET = "dev-change-in-production-!"

# HTTP-Security-Header, die bei jeder Antwort gesetzt werden (VULN-008).
# Content-Security-Policy bewusst konservativ, kompatibel zu Bootstrap/CDN:
#   'self' + inline styles (Bootstrap/Toasts verwenden style=)
#   'unsafe-inline' für Script bleibt zunächst bestehen, weil Templates
#   inline onclick-Handler und kleine Script-Blöcke nutzen. Eine spätere
#   Härtung auf nonce-basiertes CSP ist dokumentiert in docs/09.
_SECURITY_HEADERS = {
    "X-Content-Type-Options":  "nosniff",
    "X-Frame-Options":         "DENY",
    "Referrer-Policy":         "strict-origin-when-cross-origin",
    "Permissions-Policy":      "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
}


def create_app(db_path: str = None) -> Flask:
    # Absoluter Projektpfad – von run.py gesetzt, bevor irgendein Modul
    # importiert wird. Dadurch bleiben alle Pfade korrekt, auch wenn
    # webapp/__init__.py aus dem Sidecar-Override (updates/) geladen wurde.
    _project_root = os.environ.get(
        'IDV_PROJECT_ROOT',
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    if getattr(sys, 'frozen', False):
        _tpl = str(Path(sys._MEIPASS) / 'webapp' / 'templates')
        _instance_default = os.path.join(os.path.dirname(sys.executable), 'instance')
    else:
        # Absoluter Pfad – unabhängig davon, von wo __init__.py geladen wurde
        _tpl = os.path.join(_project_root, 'webapp', 'templates')
        _instance_default = os.path.join(_project_root, 'instance')

    app = Flask(__name__, instance_relative_config=True, template_folder=_tpl)

    # Sidecar-Template-Override: templates aus updates/ haben Vorrang
    from jinja2 import ChoiceLoader, FileSystemLoader as _FSL
    _ovr_tpl = os.path.join(_project_root, 'updates', 'templates')
    if os.path.isdir(_ovr_tpl):
        app.jinja_loader = ChoiceLoader([_FSL(_ovr_tpl), app.jinja_loader])

    _instance_path = os.environ.get("IDV_INSTANCE_PATH", _instance_default)
    upload_folder = os.path.join(_instance_path, "uploads", "freigaben")

    # VULN-004: SECRET_KEY-Enforcement
    #   - Ist die Umgebungsvariable SECRET_KEY nicht gesetzt, fällt die
    #     Anwendung auf einen statischen Dev-Key zurück. Im DEBUG-Modus ist
    #     das tolerierbar, im Produktivbetrieb nicht. Wir markieren diesen
    #     Zustand im app.config, damit er oben sichtbar wird (Konsole, Banner)
    #     und prüfen ihn unten per Startup-Check in run.py.
    _debug_mode = os.environ.get("DEBUG", "0") == "1"
    _secret_key = os.environ.get("SECRET_KEY")
    _secret_key_is_default = False
    if not _secret_key:
        _secret_key = _DEFAULT_DEV_SECRET
        _secret_key_is_default = True

    app.config.update(
        SECRET_KEY=_secret_key,
        SECRET_KEY_IS_DEFAULT=_secret_key_is_default,
        DEBUG_MODE_ACTIVE=_debug_mode,
        DATABASE=db_path or os.environ.get(
            "IDV_DB_PATH",
            os.path.join(_instance_path, "idvault.db")
        ),
        UPLOAD_FOLDER=upload_folder,
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,   # 32 MB max upload
        APP_NAME="idvault",
        BUNDLED_VERSION=os.environ.get('BUNDLED_VERSION', '0.1.0'),
        APP_VERSION=os.environ.get('IDV_ACTIVE_VERSION') or os.environ.get('BUNDLED_VERSION', '0.1.0'),
    )

    # VULN-013: Session-Hardening
    #   - Idle-Timeout von 4 Stunden
    #   - HttpOnly + SameSite=Lax standardmäßig
    #   - Secure-Cookie-Flag automatisch, sobald HTTPS aktiv ist
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=(os.environ.get("IDV_HTTPS", "0") == "1"),
    )

    os.makedirs(_instance_path, exist_ok=True)
    os.makedirs(upload_folder, exist_ok=True)

    # Login-Logger einrichten (instance/login.log)
    from .login_logger import setup_login_logger
    setup_login_logger(_instance_path)

    # Datei-Logging: WARNING+ → instance/idvault.log
    # RotatingFileHandler: 1 MB pro Datei, 7 Backups (idvault.log … idvault.log.7)
    # Die Crash-/stderr-Umleitung in run.py schreibt in idvault_crash.log
    # (separate Datei), damit dieser Handler die Datei sperr-frei rotieren kann.
    import logging
    from logging.handlers import RotatingFileHandler as _RFH
    _fh = _RFH(
        os.path.join(_instance_path, 'idvault.log'),
        maxBytes=1 * 1024 * 1024,   # 1 MB pro Segment
        backupCount=7,              # idvault.log + .1 … .7
        encoding='utf-8',
        delay=True,                 # Datei erst anlegen wenn der erste Eintrag kommt
    )
    _fh.setLevel(logging.WARNING)
    _fh.setFormatter(logging.Formatter(
        '[%(asctime)s] %(levelname)s %(name)s: %(message)s'
    ))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.WARNING)
    logging.getLogger().addHandler(_fh)  # Root-Logger: werkzeug, sqlalchemy etc.

    # Datenbank
    init_app_db(app)

    # Blueprints registrieren
    from .routes.auth       import bp as auth_bp
    from .routes.dashboard  import bp as dash_bp
    from .routes.idv        import bp as idv_bp
    from .routes.reviews    import bp as rev_bp
    from .routes.measures   import bp as meas_bp
    from .routes.admin      import bp as admin_bp
    from .routes.funde      import bp as funde_bp
    from .routes.reports    import bp as reports_bp
    from .routes.freigaben  import bp as freigaben_bp
    from .routes.tests      import bp as tests_bp
    from .routes.cognos     import bp as cognos_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(idv_bp)
    app.register_blueprint(rev_bp)
    app.register_blueprint(meas_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(funde_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(freigaben_bp)
    app.register_blueprint(tests_bp)
    app.register_blueprint(cognos_bp)

    # VULN-008: HTTP-Security-Header bei jeder Antwort setzen
    @app.after_request
    def _add_security_headers(response):
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # HSTS nur senden, wenn HTTPS aktiv ist – sonst sperren wir HTTP
        # unbeabsichtigt aus.
        if os.environ.get("IDV_HTTPS", "0") == "1":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    # VULN-013: Session als "permanent" kennzeichnen, damit PERMANENT_SESSION_LIFETIME greift
    @app.before_request
    def _make_session_permanent():
        from flask import session
        session.permanent = True

    # Context Processor: Scanner-Eingang Badge-Count für alle Templates
    @app.context_processor
    def inject_scanner_badge():
        from flask import has_request_context
        if not has_request_context():
            return {}
        try:
            db = get_db()
            count = db.execute(
                "SELECT COUNT(*) FROM idv_files "
                "WHERE status='active' AND bearbeitungsstatus='Neu'"
            ).fetchone()[0]
        except Exception:
            count = 0
        return {"scanner_eingang_count": count}

    # Template-Filter
    @app.template_filter("datefmt")
    def datefmt(value, fmt="%d.%m.%Y"):
        if not value:
            return "–"
        try:
            if "T" in str(value):
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                # UTC-Zeitstempel in lokale Systemzeit umwandeln
                if dt.tzinfo is not None:
                    dt = dt.astimezone()
            else:
                dt = datetime.strptime(str(value)[:10], "%Y-%m-%d")
            return dt.strftime(fmt)
        except Exception:
            return str(value)

    @app.template_filter("datetimefmt")
    def datetimefmt(value, fmt="%d.%m.%Y %H:%M"):
        if not value:
            return "–"
        try:
            s = str(value)
            if "T" in s:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone()
            else:
                for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(s[:len(pattern)], pattern)
                        break
                    except ValueError:
                        continue
                else:
                    return s
            return dt.strftime(fmt)
        except Exception:
            return str(value)

    @app.template_filter("mb")
    def to_mb(value):
        if value is None:
            return "–"
        return f"{value / 1024 / 1024:.2f} MB"

    @app.template_filter("yesno")
    def yesno(value, labels="Ja,Nein"):
        pos, neg = labels.split(",")
        return pos if value else neg

    return app
