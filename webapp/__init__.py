"""
idvault – Flask Web Application
================================
IDV-Register für MaRisk AT 7.2 / DORA-konforme Verwaltung
von Eigenentwicklungen (Individuelle Datenverarbeitung).
"""

import os
import re
import secrets
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Flask, g, request
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from .db_flask import get_db, close_db, init_app_db


# Hinweis zu Sicherheits-Remediationen (VULN-004/005/008/013):
# Die folgenden Konstanten werden in create_app() ausgewertet.
_DEFAULT_DEV_SECRET = "dev-change-in-production-!"

# Flask-Erweiterungen (als Modul-Singletons, damit sie von Tests importiert
# und von Blueprints referenziert werden können).
csrf    = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
    strategy="fixed-window",
)

# HTTP-Security-Header, die bei jeder Antwort gesetzt werden (VULN-008).
# Content-Security-Policy bewusst konservativ und **offline-tauglich**:
#   - Alle Frontend-Assets (Bootstrap, Bootstrap Icons, QuillJS) werden lokal
#     unter webapp/static/vendor/ ausgeliefert. Deshalb keine CDN-Freigabe.
#   - Inline ``<script>``- und ``<style>``-Blöcke laufen nur noch mit dem
#     Request-spezifischen CSP-Nonce (VULN-M). Injizierte ``<script>``-Tags
#     aus z. B. Rich-Text-Feldern sind damit blockiert.
#   - Inline-Event-Handler (``onclick=…``) verbleiben in den Templates;
#     dafür erlaubt CSP Level 3 ``script-src-attr 'unsafe-inline'``.
_SECURITY_HEADERS_STATIC = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options":        "DENY",
    "Referrer-Policy":        "strict-origin-when-cross-origin",
    "Permissions-Policy":     "geolocation=(), microphone=(), camera=()",
}


def _build_csp(nonce: str) -> str:
    # VULN-M: Vollständige CSP ohne ``unsafe-inline`` für Scripts.
    # Alle Inline-<script>/<style>-Blöcke erhalten den Request-Nonce
    # serverseitig (``_inject_nonces``) – Inline-Event-Handler wurden aus
    # den Templates entfernt und laufen über globale Event-Delegation in
    # base.html. ``style-src-attr 'unsafe-inline'`` bleibt, weil Bootstrap-
    # Utility-Klassen weiterhin einige ``style="…"``-Attribute nutzen.
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline'; "
        "style-src-attr 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-src 'none'; "
        "worker-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


# Regex erkennt öffnende ``<script>``-/``<style>``-Tags (mit oder ohne
# Attribute), aber nicht abschließende (``</script>``) oder verwandte
# Tag-Namen (``<scripts>``). Der Lookahead ``(?=\s|>|/>)`` erzwingt, dass
# unmittelbar nach dem Tag-Namen ein Whitespace, ein ``/`` oder das
# schließende ``>`` steht.
_INLINE_SCRIPT_TAG = re.compile(
    rb"<script(?P<attrs>(?:\s[^>]*)?)\s*>", re.IGNORECASE
)
_INLINE_STYLE_TAG = re.compile(
    rb"<style(?P<attrs>(?:\s[^>]*)?)\s*>", re.IGNORECASE
)


def _env_bool(name: str, default: bool) -> bool:
    """Liest einen Env-Wert ``0/1`` oder ``true/false`` als Bool."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _load_local_users_from_env() -> dict:
    """Liest lokale Benutzer aus der ``IDV_LOCAL_USERS``-Umgebungsvariable.

    VULN-F: Demo-Logins sind entfernt. Wer lokale Kennungen braucht, kann sie
    über ``config.json`` (``"IDV_LOCAL_USERS"``-Key, JSON-Array) konfigurieren
    – ``run.py`` schreibt den JSON-String in die Umgebung.

    Zwei Formate werden akzeptiert – pro Eintrag **eines** von beiden:

    1. Werkzeug-Hash (empfohlen)::

            {
                "username":      "admin",
                "password_hash": "pbkdf2:sha256:600000$…$…",
                "name":          "Administrator",
                "role":          "IDV-Administrator",
                "person_id":     null
            }

    2. Klartext-Passwort (bequemer für Erstinstallationen / abgeschottete
       Testumgebungen; **in Produktion nicht empfohlen**)::

            {
                "username": "admin",
                "password": "mein-geheim",
                "name":     "Administrator",
                "role":     "IDV-Administrator"
            }

       Das Klartextpasswort wird beim Start in einen pbkdf2-Hash
       konvertiert (niemals im Speicher länger als nötig gehalten) und
       dann wie ein gehashter Eintrag behandelt. Das ``password``-Feld
       selbst wird verworfen, sodass nachgelagerter Code keinen Zugriff
       mehr auf den Klartext hat.

    Einträge ohne ``username`` oder ohne Passwortangabe werden ignoriert.
    Liegt beides vor, gewinnt ``password_hash``.
    """
    import json
    from werkzeug.security import generate_password_hash
    raw = os.environ.get("IDV_LOCAL_USERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    out: dict = {}
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        username = str(entry.get("username") or "").strip()
        if not username:
            continue

        pw_hash = str(entry.get("password_hash") or "").strip()
        if pw_hash and ":" not in pw_hash:
            # Kein gültiges werkzeug-Format → ignorieren (nicht heimlich
            # akzeptieren, weil hash-Feld sonst als Klartext genutzt würde).
            pw_hash = ""

        if not pw_hash:
            pw_plain = entry.get("password")
            if isinstance(pw_plain, str) and pw_plain:
                # Klartext in einen Hash überführen; das Original-Feld
                # bleibt in ``entry`` zwar bestehen (wir mutieren die
                # Config nicht), aber das Ergebnis-Dict enthält nur den
                # Hash.
                pw_hash = generate_password_hash(pw_plain, method="pbkdf2:sha256")

        if not pw_hash:
            continue  # weder gültiger Hash noch Klartext → Eintrag ignorieren

        out[username] = {
            "password_hash": pw_hash,
            "name":          str(entry.get("name") or username),
            "role":          str(entry.get("role") or "Fachverantwortlicher"),
            "person_id":     entry.get("person_id"),
        }
    return out


def _inject_nonces(body: bytes, nonce: str) -> bytes:
    """Fügt ``nonce="…"`` in Inline-``<script>``/``<style>``-Tags ein."""
    nonce_bytes = nonce.encode("ascii")

    def _inject(tag: bytes, match) -> bytes:
        attrs = match.group("attrs") or b""
        # ``src=``-Attribut → externe Ressource, kein Nonce nötig.
        if tag == b"script" and b"src=" in attrs.lower():
            return match.group(0)
        if b"nonce=" in attrs.lower():
            return match.group(0)
        return b"<" + tag + b' nonce="' + nonce_bytes + b'"' + attrs + b">"

    body = _INLINE_SCRIPT_TAG.sub(lambda m: _inject(b"script", m), body)
    body = _INLINE_STYLE_TAG.sub(lambda m: _inject(b"style", m), body)
    return body


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
        _static = str(Path(sys._MEIPASS) / 'webapp' / 'static')
        _instance_default = os.path.join(os.path.dirname(sys.executable), 'instance')
    else:
        # Absoluter Pfad – unabhängig davon, von wo __init__.py geladen wurde
        _tpl = os.path.join(_project_root, 'webapp', 'templates')
        _static = os.path.join(_project_root, 'webapp', 'static')
        _instance_default = os.path.join(_project_root, 'instance')

    # Instance-Pfad vor der App-Erzeugung bestimmen und Flask explizit
    # übergeben. Sonst leitet Flask ``app.instance_path`` selbst her (z.B.
    # aus dem Package-Verzeichnis) und unsere Upload-Routen, die über
    # ``current_app.instance_path`` speichern, landen nicht im erwarteten
    # Projekt-``instance/``-Ordner – besonders fatal bei PyInstaller-Builds,
    # wo Flasks Default im (temporären) _MEIPASS-Verzeichnis liegen kann.
    #
    # Flask verlangt einen absoluten Pfad. ``IDV_INSTANCE_PATH`` darf trotzdem
    # relativ angegeben werden – wir lösen relative Werte zum EXE-Verzeichnis
    # (frozen) bzw. zur Projektwurzel (Dev) auf, NICHT zur CWD. Das ist
    # wichtig, weil ``idvault.exe`` als Dienst oder per Doppelklick häufig
    # mit einer fremden CWD (z.B. ``C:\Windows\System32``) gestartet wird.
    _anchor = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
              else _project_root
    _raw_instance = os.environ.get("IDV_INSTANCE_PATH", _instance_default)
    _instance_path = (
        _raw_instance if os.path.isabs(_raw_instance)
        else os.path.normpath(os.path.join(_anchor, _raw_instance))
    )

    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=_instance_path,
        template_folder=_tpl,
        static_folder=_static,
        static_url_path='/static',
    )

    # Sidecar-Template-Override: templates aus updates/ haben Vorrang
    from jinja2 import ChoiceLoader, FileSystemLoader as _FSL
    _ovr_tpl = os.path.join(_project_root, 'updates', 'templates')
    if os.path.isdir(_ovr_tpl):
        app.jinja_loader = ChoiceLoader([_FSL(_ovr_tpl), app.jinja_loader])

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

    # IDV_DB_PATH darf (wie IDV_INSTANCE_PATH) relativ angegeben werden —
    # etwa weil die config.json ab Auslieferung "instance/idvault.db" setzt.
    # Relative Pfade gegen die CWD aufzulösen ist fatal: idvault.exe wird als
    # Dienst mit CWD=C:\Windows\System32 gestartet und per Doppelklick aus
    # beliebigen Verzeichnissen — sqlite3.connect schlägt dann mit
    # "unable to open database file" fehl. Deshalb: relative Werte an den
    # EXE-Anker binden (wie oben für _instance_path).
    _raw_db = db_path or os.environ.get("IDV_DB_PATH")
    if _raw_db:
        _db_path = _raw_db if os.path.isabs(_raw_db) \
                   else os.path.normpath(os.path.join(_anchor, _raw_db))
    else:
        _db_path = os.path.join(_instance_path, "idvault.db")

    app.config.update(
        SECRET_KEY=_secret_key,
        SECRET_KEY_IS_DEFAULT=_secret_key_is_default,
        DEBUG_MODE_ACTIVE=_debug_mode,
        DATABASE=_db_path,
        UPLOAD_FOLDER=upload_folder,
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,   # 32 MB max upload
        APP_NAME="idvault",
        BUNDLED_VERSION=os.environ.get('BUNDLED_VERSION', '0.1.0'),
        APP_VERSION=os.environ.get('IDV_ACTIVE_VERSION') or os.environ.get('BUNDLED_VERSION', '0.1.0'),
        # VULN-A: CSRFProtect
        WTF_CSRF_TIME_LIMIT=None,          # Token für gesamte Session gültig
        WTF_CSRF_SSL_STRICT=False,          # Referer-Check würde bei HTTP-Proxy scheitern
        # VULN-B: Sidecar-Update-Upload per config.json abschaltbar.
        IDV_ALLOW_SIDECAR_UPDATES=_env_bool("IDV_ALLOW_SIDECAR_UPDATES", True),
        # VULN-F: Lokale Benutzer werden ausschließlich aus config.json gelesen.
        IDV_LOCAL_USERS=_load_local_users_from_env(),
        # VULN-J: Login-Rate-Limit konfigurierbar.
        IDV_LOGIN_RATE_LIMIT=os.environ.get(
            "IDV_LOGIN_RATE_LIMIT", "5 per minute;30 per hour"
        ),
        # VULN-009: Rate-Limit für Admin-Uploads (ZIP, CSV-Importe).
        IDV_UPLOAD_RATE_LIMIT=os.environ.get(
            "IDV_UPLOAD_RATE_LIMIT", "10 per minute;60 per hour"
        ),
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
    os.makedirs(os.path.join(_instance_path, 'logs'), exist_ok=True)

    # Login-Logger einrichten (instance/logs/login.log)
    from .login_logger import setup_login_logger
    setup_login_logger(_instance_path)

    # Datei-Logging: WARNING+ → instance/logs/idvault.log
    # RotatingFileHandler: 1 MB pro Datei, 7 Backups (idvault.log … idvault.log.7)
    # Die Crash-/stderr-Umleitung in run.py schreibt in idvault_crash.log
    # (separate Datei), damit dieser Handler die Datei sperr-frei rotieren kann.
    import logging
    from logging.handlers import RotatingFileHandler as _RFH
    _fh = _RFH(
        os.path.join(_instance_path, 'logs', 'idvault.log'),
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

    # VULN-A / VULN-J: Flask-Erweiterungen initialisieren
    csrf.init_app(app)
    limiter.init_app(app)

    # CSP-Nonce pro Request bereitstellen (VULN-M)
    @app.before_request
    def _set_csp_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    # VULN-010: Eingabelängen-/Format-Validierung für POST-Formulare.
    # Greift vor der Route-Logik und weist zu lange oder steuerzeichen-
    # behaftete Eingaben mit HTTP 400 ab. Multipart-Uploads (Dateien!) und
    # JSON-APIs bleiben unberührt.
    @app.before_request
    def _validate_post_lengths():
        if request.method != "POST":
            return
        ct = (request.content_type or "").lower()
        if ct.startswith("application/x-www-form-urlencoded") or ct.startswith("multipart/form-data"):
            from .security import validate_form_lengths
            validate_form_lengths(request.form)

    @app.context_processor
    def _csp_nonce_ctx():
        return {"csp_nonce": lambda: getattr(g, "csp_nonce", "")}

    # CSRF-Token in Templates bequem verfügbar machen (z. B. für AJAX)
    @app.context_processor
    def _csrf_ctx():
        return {"csrf_token": generate_csrf}

    # Pfad-Mappings (UNC → Laufwerksbuchstabe) aus config.json laden
    import json as _json_mod
    _raw_mappings = os.environ.get("path_mappings", "[]")
    try:
        _path_mappings = _json_mod.loads(_raw_mappings)
        if not isinstance(_path_mappings, list):
            _path_mappings = []
    except Exception:
        _path_mappings = []
    app.config["PATH_MAPPINGS"] = _path_mappings

    # Jinja2-Filter: {{ pfad | map_path }}
    # Wendet konfigurierte Pfad-Mappings auf einen Anzeigewert an.
    # Idempotent: bereits gemappte Pfade (aus der DB) bleiben unverändert.
    from flask import current_app as _cur_app
    try:
        from scanner.path_utils import apply_path_mappings as _apply_pm
    except ImportError:
        try:
            sys.path.insert(0, os.path.join(_project_root, 'scanner'))
            from path_utils import apply_path_mappings as _apply_pm
        except ImportError:
            _apply_pm = None

    if _apply_pm is not None:
        @app.template_filter("map_path")
        def _map_path_filter(path):
            if not path:
                return path
            from flask import current_app
            mappings = current_app.config.get("PATH_MAPPINGS", [])
            return _apply_pm(str(path), mappings)
    else:
        @app.template_filter("map_path")
        def _map_path_filter(path):
            return path

    # Datenbank
    init_app_db(app)

    # Sidecar DB-Migration: updates/db_migrate.py wird einmalig beim Start
    # ausgeführt, wenn die Datei vorhanden ist. ZIP-Updates können damit
    # Schemaänderungen (ALTER TABLE, neue Tabellen) mitliefern, ohne dass
    # die EXE ausgetauscht werden muss. Konvention: die Datei muss eine
    # Funktion run(db_path: str) exportieren.
    _migrate_script = os.path.join(_project_root, 'updates', 'db_migrate.py')
    if os.path.isfile(_migrate_script):
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location('db_migrate', _migrate_script)
            _mmod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mmod)
            if hasattr(_mmod, 'run'):
                _mmod.run(app.config['DATABASE'])
        except Exception as _me:
            app.logger.warning("updates/db_migrate.py fehlgeschlagen: %s", _me)

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

    # Zeitplan-Scheduler starten (Daemon-Thread – nicht in Testläufen)
    if not app.testing:
        from .routes.admin import start_scheduler
        start_scheduler(app)

    # Sidecar-Blueprint-Autodiscovery: neue .py-Dateien in
    # updates/webapp/routes/ werden automatisch als Blueprint registriert,
    # wenn sie ein Attribut 'bp' exportieren. Ermöglicht das Einführen neuer
    # Feature-Module per ZIP-Update ohne EXE-Austausch.
    _upd_routes = os.path.join(_project_root, 'updates', 'webapp', 'routes')
    if os.path.isdir(_upd_routes):
        import importlib as _il
        for _fname in sorted(os.listdir(_upd_routes)):
            if not _fname.endswith('.py') or _fname.startswith('_'):
                continue
            _bpname = _fname[:-3]
            if _bpname in app.blueprints:
                continue
            try:
                _mod = _il.import_module(f"webapp.routes.{_bpname}")
                if hasattr(_mod, 'bp'):
                    app.register_blueprint(_mod.bp)
            except Exception as _be:
                app.logger.warning("Sidecar-Blueprint '%s' nicht geladen: %s", _bpname, _be)

    # VULN-008 / VULN-M: HTTP-Security-Header bei jeder Antwort setzen.
    # CSP-Nonce wird an dieser Stelle in inline <script>/<style>-Tags injiziert
    # und der Header passend dazu gesetzt.
    @app.after_request
    def _add_security_headers(response):
        for name, value in _SECURITY_HEADERS_STATIC.items():
            response.headers.setdefault(name, value)

        nonce = getattr(g, "csp_nonce", None)
        if nonce:
            response.headers.setdefault(
                "Content-Security-Policy", _build_csp(nonce)
            )
            # Nur HTML-Antworten (keine Downloads, kein JSON) bekommen
            # Nonces in inline Scripts/Styles injiziert.
            ctype = (response.content_type or "").lower()
            if (
                ctype.startswith("text/html")
                and not response.direct_passthrough
                and response.is_sequence
            ):
                try:
                    body = response.get_data()
                    response.set_data(_inject_nonces(body, nonce))
                except (RuntimeError, AttributeError):
                    pass

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

    # Template-Global: url_for-Wrapper der keinen BuildError wirft
    @app.template_global()
    def safe_url_for(endpoint: str, **values) -> str:
        """Gibt die URL zurück oder '#' wenn der Endpoint nicht existiert."""
        from flask import url_for
        from werkzeug.routing import BuildError
        try:
            return url_for(endpoint, **values)
        except BuildError:
            return "#"

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

    @app.template_filter("path_breadcrumbs")
    def path_breadcrumbs(path: str):
        """Zerlegt einen Dateisystempfad in Breadcrumb-Segmente.

        Gibt eine Liste von (label, prefix) Tupeln zurück, wobei prefix der
        kumulative Pfad bis einschließlich dieses Segments ist.
        Windows-UNC-Pfade (\\\\server\\share\\...) werden korrekt behandelt.
        """
        if not path:
            return []
        # Trennzeichen erkennen: Windows nutzt \\, Unix /
        if "\\" in path:
            sep = "\\"
        else:
            sep = "/"
        parts = path.split(sep)
        # Leere Segmente (z.B. führende \\ oder /) herausfiltern, aber Position merken
        segs = []
        prefix_parts = []
        is_unc = path.startswith("\\\\") or path.startswith("//")
        for i, p in enumerate(parts):
            if not p:
                prefix_parts.append(p)
                continue
            prefix_parts.append(p)
            prefix = sep.join(prefix_parts)
            # UNC-Pfade beginnen mit \\server → kein Link für den ersten echten Teil
            if is_unc and len([x for x in prefix_parts if x]) <= 1:
                # \\server allein – nicht klickbar (share_root)
                continue
            segs.append((p, prefix))
        return segs

    return app
