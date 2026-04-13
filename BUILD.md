# idvault – Standalone-Executable erstellen

Diese Anleitung beschreibt, wie aus dem idvault-Quellcode eine eigenständige
`.exe` erstellt wird, die ohne Python-Installation auf anderen Rechnern läuft.

---

## Voraussetzungen

- Python 3.10 oder neuer muss installiert sein
- Prüfen mit:
  ```cmd
  python --version
  ```
  → Gibt `Python 3.x.x` aus. Falls Fehler: Python von https://www.python.org/downloads/ herunterladen und **"Add Python to PATH"** beim Installieren anhaken.

---

## Schritt 1: Abhängigkeiten installieren

Im Projektverzeichnis ausführen:

```cmd
pip install -r requirements.txt
pip install -r requirements-build.txt
```

**Optionale Scanner-Pakete** (nur installieren wenn gewünscht – werden dann automatisch ins Bundle aufgenommen):

```cmd
pip install xxhash
pip install pywin32
```

> `xxhash` beschleunigt das Hashing großer Dateien im Scanner.
> `pywin32` wird benötigt um Datei-Eigentümer unter Windows auszulesen.

---

## Schritt 2: Executable bauen

```cmd
python -m PyInstaller idvault.spec --clean --noconfirm
```

Der Build dauert ca. 1–3 Minuten. Am Ende erscheint:

```
Building EXE from EXE-00.toc completed successfully.
```

---

## Schritt 3: Ergebnis

Die fertige Datei liegt unter:

```
dist\idvault.exe
```

Diese einzelne Datei kann auf andere Windows-Rechner kopiert und dort direkt
ausgeführt werden – ohne Python-Installation.

---

## Schritt 4: Starten

```cmd
dist\idvault.exe
```

Oder per Doppelklick im Explorer. Es öffnet sich ein Konsolenfenster mit:

```
=======================================================
  idvault – IDV-Register
  http://localhost:5000
  DB: C:\Users\...\AppData\Local\Temp\_MEIxxxxx\...
  Demo-Login: admin / idvault2025
=======================================================
```

Anschließend im Browser `http://localhost:5000` aufrufen.

> Die Datenbank (`idvault.db`) wird beim ersten Start automatisch im Ordner
> `instance\` neben der `.exe` angelegt und bleibt beim nächsten Start erhalten.

---

## Fehlerbehebung

### `python` nicht gefunden
Python ist nicht im PATH. Entweder Python neu installieren (Haken bei
"Add Python to PATH" setzen) oder den vollständigen Pfad verwenden:
```cmd
C:\Users\<Name>\AppData\Local\Programs\Python\Python3xx\python.exe -m pip install pyinstaller
```

### `pip` nicht gefunden
```cmd
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
```

### Antivirus blockiert die .exe
PyInstaller-Executables werden von manchen Virenscannern fälschlicherweise
als verdächtig eingestuft (False Positive). Die Datei als Ausnahme hinzufügen
oder den Build auf dem Zielrechner selbst durchführen.

### Fehler beim Start: `schema.sql nicht gefunden`
Die `.exe` wurde möglicherweise aus dem falschen Verzeichnis gebaut.
Sicherstellen dass `pyinstaller` im Projektverzeichnis (dort wo `schema.sql`
liegt) ausgeführt wird.

---

## Update-Pakete erstellen

Mit dem Sidecar-Update-Mechanismus können Fehlerkorrekturen und neue Funktionen
ohne Neuverteilung der EXE eingespielt werden. Die EXE bleibt byte-identisch —
AppLocker-Regeln bleiben gültig.

### Was kann per Update-Paket geändert werden?

| Änderungstyp | Möglich ohne neuen Build? |
|---|:---:|
| Python-Quellcode (`.py`-Dateien in `webapp/`) | ✓ |
| Jinja2-Templates (`.html`) | ✓ |
| Datenbankschema (`schema.sql`) | ✓ (wird nicht automatisch ausgeführt) |
| Neue Python-Pakete / Abhängigkeiten | — (erfordert neuen Build) |
| Änderungen an `run.py` | — (wird vor dem Override geladen) |
| Änderungen an `scanner/` | — (Scanner ist separat gebündelt) |

> Schemaänderungen im ZIP werden nicht automatisch auf die Datenbank angewendet.
> Falls nötig, muss eine Migration über `db.py` ergänzt und eingespielt werden.

### ZIP-Struktur

```
update-v0.2.0.zip
├── version.json                 ← Pflicht (Versionsinformation + Changelog)
├── webapp/                      ← Python-Module (spiegelt Projektstruktur)
│   ├── __init__.py
│   ├── routes/
│   │   ├── admin.py
│   │   └── idv.py
│   └── email_service.py
└── templates/                   ← Templates (NICHT webapp/templates/ !)
    ├── admin/
    │   └── update.html
    └── idv/
        └── list.html
```

### Update-ZIP unter Linux / macOS erstellen

```bash
# Verzeichnisstruktur aufbauen
mkdir -p update_pkg/webapp/routes
mkdir -p update_pkg/templates/admin

# Geänderte Dateien kopieren
cp webapp/routes/admin.py  update_pkg/webapp/routes/
cp webapp/templates/admin/update.html  update_pkg/templates/admin/

# version.json erstellen
cat > update_pkg/version.json << 'EOF'
{
  "version": "0.2.0",
  "released": "2026-04-14",
  "changelog": [
    {
      "version": "0.2.0",
      "date": "2026-04-14",
      "changes": [
        "Beschreibung der Änderung"
      ]
    }
  ]
}
EOF

# ZIP packen
cd update_pkg
zip -r ../update-v0.2.0.zip .
cd ..
```

### Update-ZIP unter Windows (PowerShell) erstellen

```powershell
# Verzeichnisstruktur aufbauen
New-Item -ItemType Directory -Force -Path update_pkg\webapp\routes
New-Item -ItemType Directory -Force -Path update_pkg\templates\admin

# Geänderte Dateien kopieren
Copy-Item webapp\routes\admin.py  update_pkg\webapp\routes\
Copy-Item webapp\templates\admin\update.html  update_pkg\templates\admin\

# version.json erstellen
@'
{
  "version": "0.2.0",
  "released": "2026-04-14",
  "changelog": [
    {
      "version": "0.2.0",
      "date": "2026-04-14",
      "changes": ["Beschreibung der Änderung"]
    }
  ]
}
'@ | Set-Content -Encoding UTF8 update_pkg\version.json

# ZIP packen
Compress-Archive -Path update_pkg\* -DestinationPath update-v0.2.0.zip -Force
```

### Update einspielen

Das ZIP kann über die Web-Oberfläche der laufenden Anwendung eingespielt werden:

```
System → Software-Update → ZIP-Datei auswählen → „ZIP hochladen & einspielen"
→ „App neu starten"
```

Alternativ: ZIP-Inhalt manuell in den `updates/`-Ordner neben der EXE entpacken
und die Anwendung neu starten.

### GitHub-Repository-ZIP direkt verwenden (empfohlen)

Statt eines manuell erstellten Pakets kann der direkte GitHub-Download-Link
verwendet werden:

```
https://github.com/hvorragend/idvault/archive/refs/heads/main.zip
```

Die Anwendung erkennt das `idvault-main/`-Präfix automatisch, überspringt
nicht-relevante Dateien (`.md`, `.gitignore`, `.spec` usw.) und mappt
`webapp/templates/` korrekt auf `templates/` um.

→ Weitere Details: [Software-Update in README.md](README.md#software-update)
