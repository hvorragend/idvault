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
