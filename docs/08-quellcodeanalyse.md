# 08 – Quellcodeanalyse

---

## 1 Zielsetzung

Diese Analyse bewertet die Qualität, Wartbarkeit und Prüffähigkeit des
IDVScope-Quellcodes nach den Kriterien ISO/IEC 25010 (Product Quality
Model). Sie dient als Grundlage für die aufsichtsrechtliche
Nachweisführung über die **fachliche und handwerkliche Eignung** der
eingesetzten Software.

## 2 Methodik

### 2.1 Analysegegenstand

| Komponente | Umfang |
|---|---|
| Python-Produktivcode | ca. 8.700 Zeilen |
| Datenbankschema | ca. 900 Zeilen SQL |
| Jinja2-Templates | 43 Dateien |
| Scanner | ca. 2.000 Zeilen Python |

### 2.2 Analyseansätze

- **Strukturelle Analyse** – Modulgrenzen, Abhängigkeiten, Schichtung
- **Statische Analyse** – Code-Muster, f-Strings in SQL, Exception-Handling
- **Sicherheitsanalyse** – separat dokumentiert in [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md)
- **Konformitätsanalyse** – Abgleich Dokumentation ↔ Quellcode

## 3 Quantitative Kennzahlen

### 3.1 Umfang je Modul

| Modul | Zeilen (ca.) | Bewertung |
|---|---:|---|
| `webapp/routes/admin.py` | 2.163 | **Hoch** – Aufspaltung empfohlen |
| `scanner/network_scanner.py` | 1.200 | Akzeptabel für Scanner-Kernlogik |
| `db.py` | 846 | Angemessen |
| `webapp/routes/eigenentwicklung.py` | 869 | Angemessen |
| `webapp/routes/funde.py` | 923 | Angemessen |
| `webapp/email_service.py` | 550 | Angemessen |
| `webapp/routes/freigaben.py` | 500 | Angemessen |
| `webapp/ldap_auth.py` | 416 | Angemessen |
| `run.py` | 234 | Schlank |
| `webapp/__init__.py` | 178 | Schlank |
| `ssl_utils.py` | 150 | Schlank |
| `webapp/routes/__init__.py` | 100 | Schlank |
| `webapp/login_logger.py` | 73 | Schlank |
| `webapp/db_flask.py` | 31 | Schlank |

### 3.2 Modul-Dichte

- **Blueprints** mit je einem klaren Zuständigkeitsbereich
- **Durchschnittliche Funktionslänge**: ca. 30–50 Zeilen (akzeptabel)
- **Zyklomatische Komplexität**: nicht automatisiert gemessen – empfohlen via `radon cc`

## 4 Strukturbewertung nach ISO/IEC 25010

### 4.1 Funktionale Angemessenheit

| Kriterium | Bewertung | Bemerkung |
|---|---|---|
| Funktionale Vollständigkeit | ✅ Hoch | Alle Muss-Anforderungen des Pflichtenhefts abgedeckt |
| Funktionale Korrektheit | ✅ Hoch | Datenbank-Constraints, History-Trigger-Logik solide |
| Funktionale Angemessenheit | ✅ Hoch | Keine unnötige Komplexität |

### 4.2 Zuverlässigkeit (Reliability)

| Kriterium | Bewertung | Bemerkung |
|---|---|---|
| Ausgereiftheit | ✅ Gut | SQLite WAL-Modus, Foreign Keys, CHECK-Constraints |
| Verfügbarkeit | ✅ Gut | LDAP-Fallback, ziellaufbeständige Scanner-Checkpoints |
| Fehlertoleranz | ⚠️ Mittel | Teilweise generische `except Exception`-Klauseln |
| Wiederherstellbarkeit | ✅ Gut | Idempotente Migrationen, backup-freundliches Schema |

### 4.3 Benutzbarkeit (Usability)

| Kriterium | Bewertung |
|---|---|
| Erlernbarkeit | ✅ Gut (durchgängig deutsche Oberfläche, Kontextbeschreibungen) |
| Bedienbarkeit | ✅ Gut (Bootstrap-UI, Tastatursteuerung) |
| Barrierefreiheit | ⚠️ Mittel – Kontrastverhältnisse in UI-Paletten nicht geprüft |

### 4.4 Leistung (Performance Efficiency)

| Kriterium | Bewertung |
|---|---|
| Zeitverhalten | ✅ Gut (indizierte Queries, paginierte Listen) |
| Ressourcenverbrauch | ✅ Gut (SQLite, kein Application-Server nötig) |
| Kapazität | ✅ Gut (Checkpoint-fähiger Scanner) |

### 4.5 Wartbarkeit (Maintainability)

| Kriterium | Bewertung | Bemerkung |
|---|---|---|
| Modularität | ⚠️ Mittel | `admin.py` mit 2.163 Zeilen zu groß; Zerlegung empfohlen |
| Wiederverwendbarkeit | ✅ Gut | `db.py` sauber gekapselt |
| Analysierbarkeit | ✅ Gut | Logs, Audit-Trail, einheitliche Konventionen |
| Modifizierbarkeit | ✅ Gut | Sidecar-Updates ermöglichen Hot-Patches |
| Prüfbarkeit | ⚠️ Mittel | Automatisierte Tests fehlen; Integration von pytest empfohlen |

### 4.6 Portabilität (Portability)

| Kriterium | Bewertung |
|---|---|
| Anpassbarkeit | ✅ Gut (Windows/Linux/macOS) |
| Installationsaufwand | ✅ Sehr gut (Single-File-EXE) |
| Ersetzbarkeit | ✅ Gut (SQLite → PostgreSQL denkbar) |

### 4.7 Sicherheit (Security)

Detaillierte Analyse in [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md).

## 5 Architektur- und Codequalität

### 5.1 Positive Aspekte

- **Klare Schichtung**: Blueprints kapseln UI-Anliegen, `db.py` kapselt Datenbankzugriffe
- **Einheitliche Templates**: Alle Formulare nach identischem Muster
- **Durchgängiges Audit-Trail**: Jeder Statuswechsel / Datenänderung erzeugt `idv_history`-Eintrag
- **Parametrisierte Queries**: Kein dynamisches SQL mit Nutzereingaben
- **Auto-Escaping in Jinja2**: Systemweit aktiv, keine `|safe`-Umgehungen
- **Path-Traversal-Schutz im Update-Mechanismus**: mehrschichtig (Whitelist, `..`-Detection)
- **Idempotente Migrationen**: Neue Spalten werden rückwärtskompatibel ergänzt
- **Funktionstrennung**: Logik im Code durchgesetzt (nicht nur organisatorisch)

### 5.2 Verbesserungspotenzial

| Punkt | Aktueller Zustand | Empfehlung |
|---|---|---|
| Aufteilung `admin.py` (2.163 Zeilen) | Monolithischer Blueprint | Aufsplittung nach Fachdomänen (`admin_stammdaten.py`, `admin_ldap.py`, `admin_scanner.py`, `admin_update.py`) |
| Fehlende automatisierte Tests | Keine Test-Suite im Repo | Einführung `pytest` + Flask-Test-Client; Abdeckung mindestens für auth, idv, freigaben |
| Generisches Exception-Handling | Teilweise `except Exception: pass` | Konkrete Exception-Typen; Logging |
| Keine Type Hints | Python-Code ohne `typing` | Schrittweise Einführung, mindestens für öffentliche API |
| Keine Linter-Konfiguration | Keine `pyproject.toml`/`ruff.toml` | `ruff` + `mypy` + `bandit` in CI |
| Module dokumentieren | Uneinheitliche Docstrings | NumPy- oder Google-Style-Docstrings |
| Konstanten zentralisieren | Rollennamen sowohl Konstanten als auch Literal-Strings | Einheitliche Nutzung der Konstanten aus `webapp/routes/__init__.py` |
| Datenbank-Locking | Timeout-Verhalten nicht explizit gesetzt | `connect(..., timeout=30)` + retry-Strategie |

## 6 Abhängigkeitsanalyse

### 6.1 Direkt genutzte Pakete

| Paket | Version | Aktiv gepflegt | Risiko |
|---|---|:---:|---|
| flask | ≥ 3.0.0 | ✓ | niedrig |
| openpyxl | ≥ 3.1.0 | ✓ | niedrig |
| ldap3 | ≥ 2.9.1 | ✓ | niedrig |
| cryptography | ≥ 42.0.0 | ✓ | niedrig |
| gunicorn | ≥ 21.0.0 | ✓ | niedrig (nur Linux-Prod) |
| msal | ≥ 1.28.0 | ✓ | niedrig (optional, Teams-Scanner) |
| requests | ≥ 2.31.0 | ✓ | niedrig |
| pywin32 | ≥ 306 | ✓ | niedrig (optional, Windows) |
| xxhash | ≥ 3.0.0 | ✓ | niedrig (optional) |

**Empfehlung**: Monatlicher Scan mit `pip-audit` bzw. `safety` und
Aufnahme der Ergebnisse in das IT-Sicherheits-Reporting.

### 6.2 Fehlende Sicherheits-Pakete

| Paket | Einsatzgebiet |
|---|---|
| `argon2-cffi` oder `bcrypt` | Sicheres Passwort-Hashing (ersetzt SHA-256) |
| `flask-wtf` | CSRF-Schutz für alle Formulare |
| `flask-limiter` | Rate-Limiting (Brute-Force-Schutz Login) |
| `bleach` (optional) | HTML-Sanitizing für Freitextfelder |

## 7 Code-Konventionen

Beobachtete Konventionen:

- **Sprache im Code**: Deutsch (Variablennamen, Kommentare, DB-Spalten); konsistent beibehalten
- **Dateiname = Blueprint-Name**: `webapp/routes/eigenentwicklung.py` ⇒ `eigenentwicklung`-Blueprint
- **Templates spiegeln Blueprints**: `webapp/templates/eigenentwicklung/*.html`
- **Datum/Zeit**: ISO 8601 UTC in der DB; Darstellung via Jinja-Filter
- **Boolean-Kodierung**: INTEGER 0/1 in SQLite (SQLite hat keinen nativen BOOL-Typ)

## 8 Testabdeckung

**Befund**: Das Repository enthält keine automatisierten Tests.

**Empfehlung**: Einführung einer Test-Suite nach folgendem Plan:

| Teststufe | Framework | Zielabdeckung |
|---|---|---|
| Unit-Tests | pytest | `db.py`, `ssl_utils.py`, `webapp/ldap_auth.py` — Kernlogik |
| Integration | pytest + Flask-Test-Client | Authentifizierung, IDV-CRUD, Freigabeverfahren |
| End-to-End | playwright oder Selenium | Wichtigste Workflows |
| Security-Tests | bandit + safety | bei jedem Commit |

## 9 Dokumentations-Aktualität

| Dokument | Abgleich mit Code | Status |
|---|---|---|
| Diese Dokumentationsreihe (`docs/`) | Stand: Version 0.1.0 | ✅ aktuell |
| Inline-Dokumentation (Docstrings) | Unvollständig | ⚠️ verbesserungsfähig |
| Changelog (`version.json`) | Gepflegt | ✅ aktuell |

## 10 Release- und Änderungsmanagement

- Versionierung über `version.json` (semver-ähnlich: MAJOR.MINOR.PATCH)
- Sidecar-Update-Mechanismus trennt Auslieferung von Deployment
- Empfehlung: Führen eines CHANGELOG.md im Repo (derzeit: im `version.json`-Feld `changelog`)
- Empfehlung: Git-Tags pro Release (signiert)

## 11 Zusammenfassende Bewertung

| Qualitätsmerkmal | Bewertung |
|---|---|
| Funktionalität | ✅ Hoch |
| Zuverlässigkeit | ✅ Gut |
| Benutzbarkeit | ✅ Gut |
| Performance | ✅ Gut |
| Wartbarkeit | ⚠️ Mittel – `admin.py` und Tests adressieren |
| Portabilität | ✅ Sehr gut |
| Sicherheit | ⚠️ Mittel – fünf kritische Punkte, Remediation in [09](09-schwachstellenanalyse.md) |
| Dokumentation | ✅ Gut – durch diese Dokumentationsreihe |

**Gesamtbewertung**: Der Quellcode ist insgesamt solide strukturiert und
für die fachliche Zielsetzung angemessen. Die identifizierten
Verbesserungspotenziale (Modul-Aufspaltung, automatisierte Tests,
moderneres Passwort-Hashing, CSRF-Schutz) sollten in der nächsten
Ausbaustufe adressiert werden. Keine der gefundenen Schwächen steht
einem produktiven Einsatz grundsätzlich entgegen, sofern die unter
Punkt 11 im [Pflichtenheft](02-pflichtenheft.md) und in der
[Schwachstellenanalyse](09-schwachstellenanalyse.md) geforderten
Maßnahmen vor Go-Live umgesetzt werden.

## 12 Anhänge: Datei-Index

| Datei | Zeilen | Funktion |
|---|---:|---|
| `run.py` | 234 | Startpunkt, Sidecar-Loader, SSL-Kontext |
| `db.py` | 846 | Datenbank-Schicht, Migrationen, CRUD |
| `ssl_utils.py` | 150 | TLS-Zertifikatsverwaltung |
| `schema.sql` | ~900 | DDL: Tabellen, Views, Indizes, Trigger |
| `webapp/__init__.py` | 178 | Applikations-Fabrik |
| `webapp/db_flask.py` | 31 | Flask-Request-Wrapper für `db.py` |
| `webapp/ldap_auth.py` | 416 | LDAP-Authentifizierung, JIT-Provisioning |
| `webapp/login_logger.py` | 73 | Login-Audit-Log |
| `webapp/email_service.py` | 550 | SMTP-Versand |
| `webapp/routes/__init__.py` | 100 | Autorisierungs-Decorators |
| `webapp/routes/auth.py` | 161 | Login, Logout |
| `webapp/routes/dashboard.py` | 90 | Kennzahlen-Dashboard |
| `webapp/routes/eigenentwicklung.py` | 869 | Eigenentwicklung-CRUD, Export |
| `webapp/routes/admin.py` | 2.163 | Administration (Refaktorierung empfohlen) |
| `webapp/routes/funde.py` | 923 | Scanner-Eingang |
| `webapp/routes/freigaben.py` | 500 | Test- und Abnahmeverfahren |
| `webapp/routes/measures.py` | 90 | Maßnahmen |
| `webapp/routes/reports.py` | 110 | Reports |
| `webapp/routes/reviews.py` | 90 | Prüfungen |
| `webapp/routes/tests.py` | 230 | Test-Fälle |
| `scanner/network_scanner.py` | ~1.200 | Dateisystem-Scanner |
| `scanner/teams_scanner.py` | ~800 | Teams/Graph-Scanner |
| `scanner/excel_export.py` | 280 | Standalone-Excel-Export |
