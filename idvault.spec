# idvault.spec
# PyInstaller-Spec für einen eigenständigen Einzel-Executable.
#
# Build-Befehl:
#   pip install -r requirements-build.txt
#   pyinstaller idvault.spec --clean --noconfirm
#
# Optionale Scanner-Pakete (falls gewünscht, vor dem Build installieren):
#   pip install xxhash pywin32
#
# Ergebnis: dist/idvault  (Linux/macOS)  oder  dist/idvault.exe  (Windows)

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# ---------------------------------------------------------------------------
# Abhängigkeiten vollständig einsammeln (Binaries, Daten, Hidden-Imports)
# ---------------------------------------------------------------------------
flask_d,        flask_b,        flask_h        = collect_all('flask')
jinja2_d,       jinja2_b,       jinja2_h       = collect_all('jinja2')
werkzeug_d,     werkzeug_b,     werkzeug_h     = collect_all('werkzeug')
openpyxl_d,     openpyxl_b,     openpyxl_h     = collect_all('openpyxl')
cryptography_d, cryptography_b, cryptography_h = collect_all('cryptography')
ldap3_d,        ldap3_b,        ldap3_h        = collect_all('ldap3')

# ---------------------------------------------------------------------------
# Datei-Ressourcen
# ---------------------------------------------------------------------------
datas = [
    # Pflicht: Datenbankschema (wird von db.py beim ersten Start geladen)
    ('schema.sql', '.'),
    # Versionsinformationen
    ('version.json', '.'),
    # Pflicht: alle Jinja2-Templates
    ('webapp/templates', 'webapp/templates'),
    # Framework-Daten
    *flask_d,
    *jinja2_d,
    *werkzeug_d,
    *openpyxl_d,
    *cryptography_d,
    *ldap3_d,
]

# ---------------------------------------------------------------------------
# Hidden Imports
# Blueprints werden dynamisch registriert und von PyInstaller nicht
# automatisch erkannt. Deshalb hier explizit auflisten.
# ---------------------------------------------------------------------------
hiddenimports = [
    # Scanner-Module (liegen in scanner/, werden über pathex gefunden)
    'idv_scanner',
    'idv_export',
    # Eigene Module
    'webapp',
    'webapp.db_flask',
    'webapp.email_service',
    'webapp.reports',
    'webapp.routes',
    'webapp.routes.auth',
    'webapp.routes.dashboard',
    'webapp.routes.idv',
    'webapp.routes.reviews',
    'webapp.routes.measures',
    'webapp.routes.admin',
    'webapp.routes.scanner',
    'webapp.routes.reports',
    'webapp.routes.freigaben',
    'webapp.ldap_auth',
    'db',
    # LDAP + Verschlüsselung
    'ldap3',
    *ldap3_h,
    'cryptography',
    'cryptography.fernet',
    'cryptography.hazmat.primitives.ciphers.algorithms',
    *cryptography_h,
    # Scanner – optional (werden ignoriert wenn nicht installiert)
    'xxhash',
    'win32security',
    'win32api',
    'ntsecuritycon',
    # Framework-Internals
    *flask_h,
    *jinja2_h,
    *werkzeug_h,
    *openpyxl_h,
]

# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------
a = Analysis(
    ['run.py'],
    pathex=['.', 'scanner'],  # scanner/ damit idv_scanner.py gefunden wird
    binaries=[*flask_b, *jinja2_b, *werkzeug_b, *openpyxl_b, *cryptography_b, *ldap3_b],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pakete die nicht benötigt werden (verkleinert den Bundle)
    excludes=['gunicorn', 'tkinter', 'unittest', 'test', '_pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# Executable (--onefile)
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='idvault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True: Konsolenfenster bleibt offen (zeigt URL + DB-Pfad)
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    cipher=block_cipher,
)
