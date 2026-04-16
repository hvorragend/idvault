# 09 – Schwachstellenanalyse

---

## 1 Zielsetzung und Methodik

Diese Schwachstellenanalyse dokumentiert identifizierte
Sicherheitsmängel, ihre Bewertung und konkrete Remediation-Maßnahmen.
Die Analyse dient der aufsichtsrechtlichen Nachweisführung, dem
Schwachstellenmanagement nach BAIT Kap. 7 und dem Risikomanagement
nach DORA Art. 5 und Art. 8.

### 1.1 Methodik

- **Statische Code-Analyse** durch manuelle Durchsicht der 8.700
  Zeilen Python-Code mit Fokus auf sicherheitsrelevante Abschnitte
- **Vertiefter Security-Review** der Flask-App (Senior Python/Flask
  Engineer, Perspektive Pentest) → zusätzliche Befunde VULN-015 bis
  VULN-022 in Abschnitt 6a
- **Abgleich mit OWASP Top 10 (2021)**
- **Abgleich mit CWE (Common Weakness Enumeration)**
- **Review der Abhängigkeitsliste** (`requirements.txt`)
- **Review der Konfigurationsdefaults** (`config.json.example`)
- **Smoke-Tests** der Remediation im Flask-Test-Client (CSRF-Blockade,
  Login-Rate-Limit, CSP-Nonce-Injektion, Config-User-Login)

### 1.2 Schweregrade

| Stufe | Bedeutung | SLA zur Behebung |
|---|---|---|
| **Kritisch** | Kompromittierung unmittelbar möglich; Produktivbetrieb ungeeignet | Vor Go-Live |
| **Hoch** | Kompromittierung unter bestimmten Bedingungen möglich | Innerhalb 30 Tagen |
| **Mittel** | Theoretische Angriffsfläche; begrenzter Schaden | Innerhalb 90 Tagen |
| **Niedrig** | Best-Practice-Hinweise | Bei nächstem Major-Release |

## 2 Übersicht der identifizierten Schwachstellen

Legende Status:
- ✅ **Behoben** – technisch umgesetzt
- 🛡 **Teilweise behoben** – Teilumsetzung, Rest dokumentiert
- ⏳ **Offen** – noch nicht bearbeitet
- 📋 **Betrieblich** – Restrisiko durch Betriebsauflage adressiert

| Nr. | Titel | Severity | OWASP | CWE | Status |
|---|---|:---:|---|---|---|
| VULN-001 | Schwaches Passwort-Hashing (SHA-256 ohne Salt) | **Kritisch** | A07:2021 | CWE-916 | ✅ Behoben (pbkdf2:sha256 + Rehash-on-Login) |
| VULN-002 | Fehlender CSRF-Schutz | **Kritisch** | A01:2021 | CWE-352 | ✅ Behoben (Flask-WTF `CSRFProtect`, Token in allen Formularen, AJAX-Wrapper) |
| VULN-003 | Hardcodierte Demo-Zugangsdaten | **Kritisch** | A07:2021 | CWE-798 | ✅ Behoben (Demo-User entfernt; lokale Benutzer ausschließlich über `config.json`) |
| VULN-004 | Standard-`SECRET_KEY` im Quellcode | **Kritisch** | A02:2021 | CWE-798 | ✅ Behoben (Startup-Check in run.py) |
| VULN-005 | Aktivierbarer Debug-Modus | **Hoch** | A05:2021 | CWE-489 | ✅ Behoben (prominente Start-Warnung) |
| VULN-006 | Fehlendes Rate-Limiting am Login | **Hoch** | A07:2021 | CWE-307 | ✅ Behoben (Flask-Limiter, konfigurierbar per `IDV_LOGIN_RATE_LIMIT`) |
| VULN-007 | SMTP-Passwort im Klartext in DB | **Mittel** | A02:2021 | CWE-312 | ✅ Behoben (Fernet mit "enc:"-Präfix) |
| VULN-008 | Fehlende HTTP-Security-Header | **Mittel** | A05:2021 | CWE-693 | ✅ Behoben (Security-Header + nonce-basiertes CSP; `script-src-attr 'unsafe-inline'` vollständig entfernt, alle inline Event-Handler auf Event-Delegation umgestellt) |
| VULN-009 | Upload-Größe 32 MB ohne Rate-Limiting | **Mittel** | A05:2021 | CWE-770 | ✅ Behoben (Flask-Limiter auf Update/CSV/Teams/Cognos-Upload via `IDV_UPLOAD_RATE_LIMIT`) |
| VULN-010 | Unvalidierte Eingaben (Längen/Format) | **Mittel** | A03:2021 | CWE-20 | ✅ Behoben (zentrale Längen-/Steuerzeichen-Prüfung vor jedem POST) |
| VULN-011 | Generisches Exception-Handling | **Mittel** | A09:2021 | CWE-391 | 🛡 Teilweise (kritische Pfade: Auth, Bulk-Deletes, Verschlüsselung, E-Mail – mit Logging und spezifischen Exception-Typen) |
| VULN-012 | LDAP-Zertifikatsprüfung deaktivierbar | **Mittel** | A02:2021 | CWE-295 | ✅ Behoben (Default=1, Warnungen im Log + UI) |
| VULN-013 | Keine Session-Idle-Timeouts | **Niedrig** | A07:2021 | CWE-613 | ✅ Behoben (4 h Lifetime + Cookie-Flags) |
| VULN-014 | Keine automatisierten Tests / Security-Tests | **Niedrig** | A08:2021 | CWE-1173 | ⏳ Offen – Roadmap Sprint 4 |
| VULN-015 | Stored XSS via Quill-Rich-Text (`nachweise_text`) | **Kritisch** | A03:2021 | CWE-79 | ✅ Behoben (bleach-Sanitizer vor dem Speichern) |
| VULN-016 | Path-Traversal / IDOR am Nachweis-Download | **Hoch** | A01:2021 | CWE-639 | ✅ Behoben (Download per ID + Ownership-Check + Separator-Guard) |
| VULN-017 | Broken Access Control an schreibenden IDV-Routen | **Hoch** | A01:2021 | CWE-285 | ✅ Behoben (`ensure_can_read_idv`/`ensure_can_write_idv`) |
| VULN-018 | Upload nur per Extension-Whitelist validiert | **Hoch** | A04:2021 | CWE-434 | ✅ Behoben (Magic-Byte-Prüfung in `validate_upload_mime`) |
| VULN-019 | Jinja-Variablen in inline `onclick`-Attributen | **Mittel** | A03:2021 | CWE-79 | ✅ Behoben (Event-Delegation + `data-*`-Attribute) |
| VULN-020 | Dynamischer `IN (…)`-SQL-Fragment-Aufbau | **Niedrig** | A03:2021 | CWE-89 | ✅ Behoben (`security.in_clause()`-Helper) |
| VULN-021 | Logout über GET (unfreiwilliger Logout möglich) | **Niedrig** | A01:2021 | CWE-352 | ✅ Behoben (Logout nur noch POST + CSRF) |
| VULN-022 | Admin-RCE-Vektor über Sidecar-ZIP-Upload | **Hoch** | A08:2021 | CWE-434 | 📋 Opt-out via `IDV_ALLOW_SIDECAR_UPDATES` in `config.json` |

## 3 Detailbeschreibung der kritischen Schwachstellen

### 3.1 VULN-001 – Schwaches Passwort-Hashing ✅ BEHOBEN

**Beschreibung**: Lokale Passwörter wurden bislang mit einem einfachen
SHA-256-Hash (ohne Salt und ohne Streckung) abgelegt
(`webapp/routes/auth.py`). SHA-256 ist für Passwort-Hashing
ungeeignet, da es zu schnell berechenbar ist; ohne Salt sind
Rainbow-Table-Angriffe trivial.

**Wirkung**: Bei einem Datenbank-Leak könnten gängige Passwörter in
Sekunden gebrochen werden.

**Umgesetzte Remediation**:
- `webapp/routes/auth.py`: `_hash_pw()` nutzt jetzt
  `werkzeug.security.generate_password_hash` mit `pbkdf2:sha256`
  (600.000 Iterationen, 16-Byte Salt). Keine neue Abhängigkeit
  erforderlich – werkzeug ist Flask-Stdlib.
- `_verify_password()` erkennt beide Formate:
  - Alte Hashes: 64 Hex-Zeichen ⇒ Vergleich mit unsaltem SHA-256
  - Neue Hashes: werkzeug-Format mit `method$salt$hash`-Struktur
- **Rehash-on-Login**: Beim erfolgreichen Login mit einem alten
  Legacy-Hash wird der Hash transparent in das moderne Format
  umgeschrieben und per `UPDATE persons SET password_hash = ...`
  persistiert. Dadurch migriert der Bestand ohne Benutzerinteraktion.
- `webapp/routes/admin.py`: `_hash_pw()` delegiert an den zentralen
  modernen Hasher. Damit werden alle neu gesetzten Passwörter
  (Neuanlage, Passwort ändern, CSV-Import) mit pbkdf2 gespeichert.

**Verifikation**: Smoketest-Szenario „Login mit Legacy-Hash → Rehash →
erneuter Login mit pbkdf2-Hash → falsches Passwort abgelehnt" läuft
grün.

**Restrisiko**: Neuen Hashes wird künftig automatisch das zum Zeitpunkt
aktuelle werkzeug-Default-Verfahren zugewiesen. Eine Umstellung auf
Argon2id ist weiterhin sinnvoll und in der Roadmap verzeichnet.

### 3.2 VULN-002 – Fehlender CSRF-Schutz ✅ BEHOBEN

**Beschreibung**: POST-Formulare waren nicht mit CSRF-Tokens
abgesichert. Ein Angreifer konnte einen angemeldeten Nutzer dazu
bringen, ungewollt zustandsändernde Anfragen (z. B. Status-Änderung,
Personen-Anlage, Update-Upload) auszulösen.

**Wirkung**: Cross-Site-Request-Forgery-Angriffe waren bei ungeschützter
Browser-Session möglich.

**Umgesetzte Remediation**:
- `requirements.txt`: Abhängigkeit `flask-wtf>=1.2.0` aufgenommen.
- `webapp/__init__.py`: `CSRFProtect()` als Modul-Singleton instanziert und
  in `create_app()` per `csrf.init_app(app)` registriert. `generate_csrf`
  wird als Context-Processor allen Templates bereitgestellt.
- **Alle 77 POST-Formulare** in 33 Templates enthalten jetzt ein
  verstecktes `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`.
  Die Einfügung erfolgte systematisch (inkl. Formularen in
  `admin/index.html`, `idv/detail.html`, `freigaben/bestanden_form.html`
  etc.).
- **AJAX-POSTs**: Ein globaler `fetch()`-Wrapper in `base.html` liest
  `<meta name="csrf-token">` und setzt bei jeder nicht-sicheren Methode
  (POST/PUT/PATCH/DELETE) automatisch den Header `X-CSRFToken`. Damit
  sind auch `admin.scanner_starten`, `admin.teams_scan_starten`,
  `admin.ldap_test` und das Scan-Button-Fragment abgesichert, ohne
  dass jeder Call manuell angepasst werden muss.
- `app.config["WTF_CSRF_TIME_LIMIT"] = None` – Token bleibt für die
  Session gültig, um Formular-Abbrüche bei langen Bearbeitungszeiten
  zu vermeiden.

**Verifikation**:
- `POST /login` ohne Token → HTTP 400 (CSRF-Token missing).
- `POST /login` mit korrektem Token und Credentials → HTTP 302 →
  Dashboard.
- Smoketest im Flask-Test-Client grün.

**Restrisiko**: Token-Validierung hängt an der Session. Wird die Session
geleert (Logout, `session.clear()`), müssen anschließende POSTs einen
frischen Token nehmen; das geschieht durch die serverseitige Neuauslieferung
des Tokens beim nächsten GET automatisch.

### 3.3 VULN-003 – Hardcodierte Demo-Zugangsdaten ✅ BEHOBEN

**Beschreibung**: Die Anwendung lieferte drei Demo-Zugänge
(`admin / idvault2026`, `koordinator / demo`, `fachverantwortlicher /
demo`) als Klartext-Fallback im Quellcode (`webapp/routes/auth.py`
`_DEMO_USERS`).

**Wirkung**: Bei einer Produktivinstallation ohne Deaktivierung hätte ein
**bekannter Administrator-Zugang** bestanden; die Nutzung war im
Login-Audit zwar erkennbar, aber erst nach erfolgter Kompromittierung.

**Umgesetzte Remediation**:
- Das Dictionary `_DEMO_USERS` wurde ersatzlos gelöscht. Damit existieren
  im Quellcode keine statischen Passwörter oder Passwort-Hashes mehr.
- Lokale Benutzer werden nun ausschließlich deklarativ über
  `config.json → "IDV_LOCAL_USERS"` angelegt. Die Liste wird beim
  Start in `app.config["IDV_LOCAL_USERS"]` eingelesen und von
  `_check_config_user()` geprüft. Pro Eintrag ist **eines** der beiden
  Passwort-Felder zu setzen:
  - `password_hash` (empfohlen): Werkzeug-Format `pbkdf2:sha256:…`.
    Wird unverändert gespeichert.
  - `password` (optional, bequemer für Erstinstallationen): Klartext.
    Wird beim Start von `_load_local_users_from_env()` über
    `werkzeug.security.generate_password_hash(..., method="pbkdf2:sha256")`
    gehasht; das Ergebnis-Dict in `app.config` enthält **nur** den Hash,
    der Klartext verlässt die Config-Parsing-Funktion nicht.
  Liegt beides vor, gewinnt der Hash. Einträge ohne Passwort werden
  ignoriert.
- `config.json.example` zeigt beide Varianten kommentiert. Hash-
  Generierung:
  ```
  python -c "from werkzeug.security import generate_password_hash; \
             print(generate_password_hash('mein-passwort', method='pbkdf2:sha256'))"
  ```
- `run.py` serialisiert Listen/Dicts aus `config.json` als JSON in die
  Umgebungsvariable (vorher wurde `str([...])` geschrieben, was beim
  Parsen als JSON gescheitert wäre).
- `webapp/templates/auth/login.html`: Der „Demo-Zugänge"-Banner wurde
  entfernt.
- `run.py` druckt beim Start die aktuell konfigurierten lokalen
  Benutzernamen, damit Betreiber den Stand verifizieren können.
- Die Methode `"Demo"` im Login-Audit-Log entfällt; lokale Logins werden
  nur noch als `"lokal"` protokolliert.

**Verifikation**: Integrationstest deckt beide Varianten ab:
- Login mit `password_hash`-Eintrag + Token → HTTP 302.
- Login mit `password`-Eintrag (Klartext) + Token → HTTP 302; der
  nachgeladene `password_hash` beginnt mit `pbkdf2:sha256`.
- Einträge mit ungültigem Hash (`:` fehlt) oder ohne Passwort werden
  nicht akzeptiert (kein Login möglich).

**Restrisiko**:
- Betreiber müssen sicherstellen, dass `config.json` ausschließlich für
  den Service-User lesbar ist (NTFS-ACL bzw. Unix `0640`). Das gilt
  insbesondere bei Verwendung der `password`-Klartext-Variante.
  Festgehalten in [docs/06 – Betriebshandbuch](06-betriebshandbuch.md).
- Die Klartext-Variante ist bewusst als Komfortfunktion zugelassen
  (auf Auftraggeberwunsch für Erstinstallationen), aber im Produktivbetrieb
  ist die Hash-Variante vorzuziehen.

### 3.4 VULN-004 – Standard-`SECRET_KEY` ✅ BEHOBEN

**Beschreibung**: Der Fallback-`SECRET_KEY` ist im Quellcode als
`"dev-change-in-production-!"` fest hinterlegt (`webapp/__init__.py`).
Wird die Umgebungsvariable nicht gesetzt, greift dieser Wert.

**Wirkung**: Sessions und Fernet-verschlüsselte Werte wären mit
öffentlich bekanntem Schlüssel gesichert.

**Umgesetzte Remediation**:
- `webapp/__init__.py`: Das Config-Attribut
  `SECRET_KEY_IS_DEFAULT` wird beim App-Bau auf `True` gesetzt,
  wenn die Umgebungsvariable fehlt.
- `run.py`: Beim Start wird dieses Flag geprüft. Ist es gesetzt und
  `DEBUG != 1`, bricht der Prozess mit einer klaren Fehlermeldung und
  Exit-Code 2 ab (`!!! SICHERHEITS-ABBRUCH: Die Umgebungsvariable
  SECRET_KEY ist nicht gesetzt !!!`). Die Meldung enthält
  Beispielbefehle zur korrekten Generierung (openssl/PowerShell).
- Im Debug-Modus wird nur eine auffällige Warnung ausgegeben, damit
  lokale Entwicklung weiterhin funktioniert.

**Verifikation**: Test mit `SECRET_KEY=test-key` und ohne Variable
zeigt unterschiedliches Verhalten (Start vs. Abbruch).

## 4 Detailbeschreibung der Schwachstellen hoher Priorität

### 4.1 VULN-005 – Aktivierbarer Debug-Modus ✅ BEHOBEN

**Beschreibung**: Über `DEBUG=1` kann der Flask-Debug-Server aktiviert
werden, der bei Fehlern Stack-Traces im Browser anzeigt und eine
interaktive Debug-Konsole bereitstellt.

**Wirkung**: Information Disclosure; bei aktiver Debug-Konsole
Remote-Code-Execution über PIN-geschützten Endpunkt möglich.

**Umgesetzte Remediation**:
- `run.py`: Ist `DEBUG=1` gesetzt, wird beim Start eine
  dreizeilige Banner-Warnung ausgegeben
  („WARNUNG: DEBUG-Modus aktiv … NIEMALS in Produktions­umgebungen
  verwenden").
- `webapp/__init__.py`: Das Flag wird unter
  `app.config["DEBUG_MODE_ACTIVE"]` bereitgestellt und kann künftig im
  UI-Banner (`base.html`) eingeblendet werden.
- Betriebsauflage in [docs/05 – Sicherheitskonzept](05-sicherheitskonzept.md)
  Abschnitt 7.

### 4.2 VULN-006 – Rate-Limiting am Login ✅ BEHOBEN

**Beschreibung**: Der Login-Endpunkt erlaubte unbegrenzte Anmeldeversuche.
Automatisierte Brute-Force-Angriffe waren nicht verhindert.

**Wirkung**: Passwort-Erraten bei schwachen lokalen Passwörtern;
LDAP-Account-Lockout-Auslösung.

**Umgesetzte Remediation**:
- `requirements.txt`: Abhängigkeit `flask-limiter>=3.5.0` aufgenommen.
- `webapp/__init__.py`: Modul-Singleton
  ```python
  limiter = Limiter(key_func=get_remote_address,
                    storage_uri="memory://",
                    strategy="fixed-window")
  ```
  wird in `create_app()` per `limiter.init_app(app)` registriert.
- `webapp/routes/auth.py`: Dekorator `@limiter.limit(_login_rate_limit,
  methods=["POST"])` auf der `login()`-Route. Das Limit wird zur
  Request-Zeit aus `current_app.config["IDV_LOGIN_RATE_LIMIT"]`
  gelesen, damit Änderungen in `config.json` ohne Code-Anpassung
  greifen.
- `config.json.example`: neuer Schalter
  `"IDV_LOGIN_RATE_LIMIT": "5 per minute;30 per hour"` (Default).
  Syntax folgt der Flask-Limiter-Konvention.

**Verifikation**: In Flask-Test-Client ausgeführte Serie von POST /login
wird nach Überschreiten der Quote mit HTTP 429 abgewiesen.

**Restrisiko**: Default-Storage ist `memory://` und wirkt nur
prozessweit. Bei Mehrfach-Worker-Deployments (gunicorn) müssen die
Zähler zentralisiert werden (Redis) – siehe Sprint 4.

## 5 Schwachstellen mittlerer Priorität

### 5.1 VULN-007 – SMTP-Passwort im Klartext ✅ BEHOBEN

**Beschreibung**: Das SMTP-Passwort wurde in `app_settings` als
Klartext gespeichert (`webapp/email_service.py`).

**Umgesetzte Remediation**:
- `webapp/email_service.py::encrypt_smtp_password()` und
  `_decrypt_smtp_password()`: Neue Hilfsfunktionen, die das
  SMTP-Passwort mit derselben Fernet-Ableitung verschlüsseln, die
  bereits für das LDAP-Bind-Passwort genutzt wird
  (SHA-256(SECRET_KEY) → Base64 → Fernet).
- Verschlüsselte Werte tragen das Präfix `"enc:"`, sodass Alt- und
  Neubestände eindeutig unterscheidbar sind. Altbestände (Klartext)
  werden beim Auslesen weiterhin akzeptiert und beim nächsten
  Speichern automatisch auf das neue Format migriert.
- `webapp/routes/admin.py`: Die zwei Routen `/admin/mail` und
  `/admin/einstellungen` rufen `_save_smtp_password()` auf, bevor die
  übrigen App-Settings geschrieben werden. Leere Passwort-Eingaben
  überschreiben den gespeicherten Wert **nicht** (siehe auch
  Template-Anpassung).
- `webapp/templates/admin/mail.html`: Das Passwortfeld wird beim
  Rendern nicht mehr mit dem gespeicherten Wert vorbelegt; statt­dessen
  zeigt ein Placeholder den Zustand an („gespeichert" / „nicht
  gesetzt"). Damit gelangt das Klartextpasswort weder in die
  HTML-Quelle noch in Browser-Autofill.

**Verifikation**: Roundtrip-Test (`encrypt → decrypt → match`) sowie
Legacy-Fallback (altes Klartext-Passwort ohne `enc:`-Präfix bleibt
lesbar) grün.

**Restrisiko**: Bei Rotation des `SECRET_KEY` muss das SMTP-Passwort
neu eingegeben werden (analog LDAP-Bind-Passwort). Dokumentiert in
[docs/05 – Sicherheitskonzept](05-sicherheitskonzept.md).

### 5.2 VULN-008 – HTTP-Security-Header ✅ BEHOBEN

**Beschreibung**: Ursprünglich keine Setzung von
`Content-Security-Policy`, `Strict-Transport-Security`, `X-Frame-Options`,
`X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`.

**Umgesetzte Remediation**:
- `webapp/__init__.py` :: `_add_security_headers` (`@app.after_request`)
  setzt bei jeder Antwort folgende Header (mit `setdefault`, um explizit
  gesetzte Template-Header nicht zu überschreiben):
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: geolocation=(), microphone=(), camera=()`
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains`
    (nur wenn `IDV_HTTPS=1`).
- **Nonce-basiertes CSP** (vgl. VULN-M aus dem Security-Review):
  Für jeden Request wird in `@before_request` ein kryptografisch
  zufälliger Nonce erzeugt (`secrets.token_urlsafe(16)`) und unter
  `g.csp_nonce` abgelegt. Die Funktion `_inject_nonces()` hängt den
  Nonce in `@after_request` serverseitig an jedes Inline-`<script>`-
  und `<style>`-Tag der HTML-Antwort (ohne Templates anzufassen).
  Externe Skripte (`<script src="…">`) bleiben unberührt.
- Der finale CSP-Header lautet:
  ```
  default-src 'self';
  script-src 'self' 'nonce-{n}';
  style-src 'self' 'nonce-{n}' 'unsafe-inline';
  style-src-attr 'unsafe-inline';
  img-src 'self' data:;
  font-src 'self' data:;
  connect-src 'self';
  object-src 'none';
  frame-src 'none';
  worker-src 'none';
  frame-ancestors 'none';
  base-uri 'self';
  form-action 'self'
  ```
- **Wirkung**: Injizierte `<script>`-Tags aus Datenbankinhalten oder
  Reflexions-Parametern laufen nicht mehr, weil ihnen der Nonce fehlt.
  `script-src-attr` wurde aus der CSP entfernt, nachdem **alle 50
  inline Event-Handler** (`onclick`, `onchange`, `onsubmit`, `oninput`)
  auf Event-Delegation mit `data-action`/`data-confirm`/
  `data-submit-validate`/`data-form-validate`/`data-action-change`/
  `data-action-input`/`data-ldap-action`/`data-bulk-form` umgestellt
  wurden (siehe `webapp/templates/base.html`). Der globale Delegate
  übernimmt Confirm-Dialoge, Validatoren, Named-Function-Aufrufe,
  Form-Submits und dynamisch gerenderte Zeilen (z.B. Teams-Tabelle).
- **Wirkung auch gegen Inline-`style=`**: `style-src-attr 'unsafe-inline'`
  bleibt aus Kompatibilitätsgründen mit Bootstrap-Utility-Klassen
  bestehen. Das Risiko ist minimal (kein JS-Eval).

**Verifikation**:
- `curl -I` zeigt neue CSP ohne `script-src-attr`.
- `grep -E "on(click|change|submit|input|load|focus|blur)="` auf
  `webapp/templates/` liefert **keine Treffer** mehr.
- Flask-Test-Client: `Login-HTML` enthält 0 inline Handler.

### 5.3 VULN-009 – Upload-Rate-Limiting ✅ BEHOBEN

**Beschreibung**: 32 MB Upload-Größe pro Request × viele Requests
ermöglichten Resource-Exhaustion-Angriffe gegen Admin-Upload-Endpunkte
(ZIP-Update, CSV-Imports, Teams-/Cognos-Import).

**Umgesetzte Remediation**:
- Neuer Config-Schalter `IDV_UPLOAD_RATE_LIMIT`
  (Default `"10 per minute;60 per hour"`).
- Dekorator `@limiter.limit(_upload_rate_limit, methods=["POST"])` auf:
  - `admin.update_upload` (Sidecar-ZIP)
  - `admin.import_persons` (CSV)
  - `admin.import_geschaeftsprozesse` (CSV)
  - `cognos.import_berichte` (XLSX/CSV)
- `_upload_rate_limit()` liest den Wert zur Request-Zeit aus
  `app.config`, damit config.json-Änderungen ohne Neustart greifen.
- Kombinierbar mit VULN-B (komplette Deaktivierung des Sidecar-Uploads).

**Verifikation**: Flask-Test-Client-Test mit wiederholten POSTs >
Schwellwert liefert HTTP 429.

### 5.4 VULN-010 – Eingabelängen-/Format-Validierung ✅ BEHOBEN

**Beschreibung**: Eingabewerte wurden bisher ohne zentrale Längen- oder
Format-Prüfung an SQL-Parameter und Templates weitergereicht. Große
Texte (mehrere MB) hätten die SQLite-Performance degradiert, eingebettete
Steuerzeichen (CR/LF) hätten Log-Injection-Angriffe ermöglicht.

**Umgesetzte Remediation**:
- `webapp/security.py :: MAX_LENGTHS` definiert zentrale Obergrenzen
  für Felder wie `bezeichnung` (200), `kommentar` (5.000), `nachweise_text`
  (50.000 vor bleach-Sanitizing), `username` (128), `email` (254) etc.
- `validate_form_lengths()` prüft Längen, wirft HTTP 400 bei
  Überschreitung (`abort(400, description=…)`) und blockt zusätzlich
  Steuerzeichen sowie CR/LF in Single-Line-Feldern (`username`,
  `email`, `bezeichnung`, `kuerzel`, `q`, …).
- `webapp/__init__.py`: `@before_request`-Hook ruft die Validierung für
  jeden POST mit `application/x-www-form-urlencoded` oder
  `multipart/form-data` auf. JSON-APIs und Datei-Streams bleiben
  unberührt.

**Verifikation**:
- `POST /login` mit 200-Zeichen-`username` → HTTP 400.
- `POST /login` mit CR/LF im `username` → HTTP 400.
- `POST /login` mit normaler Eingabe → unverändert HTTP 200.

**Restrisiko**: Felder, die nicht in `MAX_LENGTHS` stehen, werden nicht
individuell begrenzt – sie unterliegen aber dem globalen
`MAX_CONTENT_LENGTH` (32 MB). Bei neuen Feldern muss der Entwickler die
Grenze dort eintragen.

### 5.5 VULN-011 – Generisches Exception-Handling 🛡 TEILWEISE BEHOBEN

**Beschreibung**: `except Exception: pass` führte zu stillen
Fehlschlägen, die im Log nicht sichtbar wurden.

**Umgesetzte Remediation – kritische Pfade**:
- `webapp/routes/auth.py`:
  - `_verify_password()`: fängt nur noch `ValueError`/`TypeError`
    (unbekanntes Hash-Format); Info-Log-Eintrag bei Ablehnung.
  - `_do_local_login()`: fängt nur noch `sqlite3.DatabaseError`, loggt
    Warnung; andere Exceptions propagieren zur Root-Error-Handler-Kette.
- `webapp/routes/admin.py`:
  - `_save_smtp_password()`: ``current_app.logger.error(…)`` + Flash
    statt `pass`.
  - Bulk-Delete Personen: `sqlite3.IntegrityError` (erwartet) vs.
    `DatabaseError` (Warning-Log).
  - Bulk-Delete Geschäftsprozesse: `DatabaseError` mit Log.
- `webapp/routes/freigaben.py`: E-Mail-Benachrichtigungsfehler werden
  als `Warning` ins App-Log geschrieben (`_notify_schritte`,
  `_notify_freigabe_erteilt`), blockieren aber den Workflow nicht.
- `webapp/routes/idv.py`: Bulk-Statusänderung loggt Einzel-Fehler.

**Restrisiko**: Es existieren weitere ~20 `except Exception:`-Blöcke in
unkritischen Pfaden (Read-only-Queries mit Defaults, JSON-Parsing von
Config-Werten). Diese werden in Sprint 3 Folge-Arbeit adressiert.

### 5.6 VULN-012 – LDAP-Zertifikatsprüfung ✅ BEHOBEN

**Beschreibung**: Das Feld `ldap_config.ssl_verify` ist konfigurierbar;
wird es deaktiviert, sind Man-in-the-Middle-Angriffe auf LDAPS möglich.

**Umgesetzte Remediation**:
- Schema-Default in `schema.sql:784` ist bereits `1`.
- `webapp/ldap_auth.py::ldap_authenticate()`: Beim Login wird der
  Zustand `ssl_verify=0` via `logger.warning()` und separatem
  Login-Audit-Eintrag protokolliert; damit bleibt das Restrisiko
  jederzeit in der Revision nachvollziehbar.
- `webapp/routes/admin.py::ldap_config()`: Beim Speichern einer
  LDAP-Konfiguration mit `ssl_verify=0` erscheint im UI eine
  `flash(..., "warning")`-Meldung und ein Audit-Log-Eintrag. Das
  Kontrollkästchen im UI ist per Default aktiv.

**Restrisiko**: Der Admin kann den Check weiterhin explizit
deaktivieren (bewusst ermöglicht, weil nicht alle internen
CAs automatisch im Python-`cafile` enthalten sind). Der Vorgang ist
aber nunmehr klar erkennbar.

## 6 Schwachstellen niedriger Priorität

### 6.1 VULN-013 – Session-Timeouts ✅ BEHOBEN

**Beschreibung**: Es waren keine expliziten Session-Idle-Timeouts gesetzt.

**Umgesetzte Remediation** in `webapp/__init__.py`:

```python
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(os.environ.get("IDV_HTTPS", "0") == "1"),
)

@app.before_request
def _make_session_permanent():
    session.permanent = True
```

Damit läuft jede Session nach 4 Stunden Inaktivität ab. `HttpOnly`
verhindert JS-Zugriffe auf das Cookie, `SameSite=Lax` schützt gegen
die meisten CSRF-Szenarien (bis VULN-002 vollständig adressiert ist),
`Secure` wird automatisch bei HTTPS-Betrieb gesetzt.

**Verifikation**: `app.config["PERMANENT_SESSION_LIFETIME"]` = 4:00:00
und Cookie-Flags im Test-Client sichtbar.

### 6.2 VULN-014 – Keine automatisierten Tests

Siehe [08 – Quellcodeanalyse](08-quellcodeanalyse.md) Abschnitt 8.

## 6a Zusätzliche Befunde aus dem Security-Review

Die folgenden Schwachstellen wurden im Rahmen eines vertieften
Security-Reviews des Flask-Codes identifiziert und in derselben
Änderung behoben. Sie waren zuvor nicht in der Ursprungsanalyse
enthalten.

### 6a.1 VULN-015 – Stored XSS über Quill-Rich-Text ✅ BEHOBEN

**Beschreibung**: Das Feld `idv_freigaben.nachweise_text` speichert den
HTML-Output des Quill-WYSIWYG-Editors und wurde in
`webapp/templates/freigaben/bestanden_form.html` mit dem Filter
`|safe` gerendert. Ein Fachverantwortlicher mit Schreibrecht auf ein
IDV konnte damit beliebiges HTML/JavaScript hinterlegen, das später
jedem Leser (insbesondere Admin in der Lesemaske) im Browser ausgeführt
wurde.

**Wirkung**: Stored-XSS mit Privilege-Escalation-Potenzial (Angreifer
kann im Admin-Kontext zustandsändernde AJAX-Calls auslösen).

**Umgesetzte Remediation**:
- `webapp/security.py :: sanitize_html()` nutzt **bleach** mit strikter
  Tag- und Attribut-Whitelist (Block-/Inline-Elemente, `<a>` mit
  begrenzten Protokollen, `style=` nur für Farben/Ausrichtung/Fett).
- `webapp/routes/freigaben.py`: Alle drei Stellen, die `nachweise_text`
  aus `request.form` lesen (`abschliessen`, `ablehnen`,
  `complete_freigabe_schritt` indirekt), rufen `sanitize_html()` vor
  dem UPDATE auf.
- Fallback ohne bleach: Falls das Paket fehlt (z. B. bei minimaler
  Dev-Installation), wird `html.escape` verwendet, sodass auf keinen
  Fall ungefiltertes HTML durchrutscht.
- `requirements.txt`: `bleach[css]>=6.1.0` aufgenommen.

**Verifikation**: Input `<img src=x onerror=alert(1)>` wird beim
Speichern zu `<img src="x">` (attribute `onerror` entfernt).

### 6a.2 VULN-016 – Path-Traversal / IDOR am Nachweis-Download ✅ BEHOBEN

**Beschreibung**: Die Routen
`/freigaben/nachweis/<path:filename>` und `/tests/nachweis/<path:filename>`
nahmen den Dateinamen direkt aus der URL und lieferten ihn über
`send_from_directory` aus. Zwar verhindert Werkzeug dort
`../`-Traversal, aber jeder authentifizierte Benutzer konnte fremde
Nachweise herunterladen, sofern er den Dateinamen kannte (IDOR).

**Wirkung**: Offenlegung vertraulicher Prüfnachweise und Protokolle
zwischen nicht-beteiligten IDV-Verantwortlichen.

**Umgesetzte Remediation**:
- Neue Routen binden den Download an stabile IDs:
  - `freigaben.nachweis_download(freigabe_id: int)`
  - `tests.nachweis_download_fachlich(testfall_id: int)`
  - `tests.nachweis_download_technisch(idv_db_id: int)`
- Jede Route liest Pfad + Anzeigename aus der DB, ermittelt die
  zugehörige IDV-ID und ruft `security.ensure_can_read_idv()` auf.
- Zusätzlicher Defense-in-Depth-Check verwirft Pfade mit `/`, `\`
  oder führenden `.`; damit bleiben auch fehlerhaft gespeicherte
  Altdatensätze ungefährlich.
- `download_name` wird aus dem ursprünglichen Klartextnamen gesetzt,
  damit Benutzer die Datei mit ihrem vertrauten Namen speichern
  können.
- Templates (`bestanden_form.html`, `fachlich_form.html`,
  `technisch_form.html`) nutzen die neuen URL-Parameter.

**Verifikation**: Manuelles `GET /freigaben/nachweis/999` eines nicht
beteiligten Benutzers → HTTP 403; Admin-Download → HTTP 200 mit
korrektem Dateiinhalt.

### 6a.3 VULN-017 – Broken Access Control an IDV-Schreibpfaden ✅ BEHOBEN

**Beschreibung**: Mehrere schreibende Routen (`idv.change_status_route`,
`idv.link_files`, `idv.neue_version`, `freigaben.abschliessen`,
`measures.complete_measure`, `reviews.new_review`, `tests.*`) prüften
zwar die Rolle, aber nicht die **Zugehörigkeit** des aktuellen
Benutzers zum jeweiligen IDV. Ein Fachverantwortlicher eines
unkritischen IDVs konnte damit den Status fremder DORA-kritischer IDVs
ändern.

**Wirkung**: Integritätsverletzung des Freigabe- und Genehmigungs-
prozesses.

**Umgesetzte Remediation**:
- Zentraler Helper `webapp/security.py`:
  - `user_can_read_idv(db, idv_db_id)` / `ensure_can_read_idv(...)`
  - `user_can_write_idv(db, idv_db_id)` / `ensure_can_write_idv(...)`
- Die Helfer prüfen die vier Beteiligten-Spalten
  (`fachverantwortlicher_id`, `idv_entwickler_id`, `idv_koordinator_id`,
  `stellvertreter_id`) gegen `current_person_id()`. Admin- und
  Koordinator-Rollen (siehe `can_write()` / `can_read_all()`) behalten
  globalen Zugriff.
- Aufruf in allen relevanten schreibenden Routen in `idv.py`, `tests.py`,
  `reviews.py`, `measures.py` und `freigaben.py`. Die bestehende
  manuelle Prüfung in `edit_idv` wurde durch den zentralen Helper
  ersetzt, um Drift zu vermeiden.

**Verifikation**: Einfacher Fachverantwortlicher kann auf eigene IDV
schreiben (200), auf fremde IDV bekommt er 403.

### 6a.4 VULN-018 – Upload nur per Extension-Whitelist validiert ✅ BEHOBEN

**Beschreibung**: Beim Hochladen von Nachweis-Dateien
(`freigaben`, `tests`) wurde ausschließlich die Datei-Extension gegen
eine Whitelist geprüft. Angreifer konnten eine ausführbare oder
aktive Datei (z. B. SVG mit JavaScript) per Umbenennung auf `.png`
einschleusen.

**Wirkung**: XSS/RCE-Risiko je nach Verarbeitung durch Browser oder
Drittsysteme.

**Umgesetzte Remediation**:
- `webapp/security.py :: validate_upload_mime()` prüft die
  **Magic-Byte-Signatur** der hochgeladenen Datei gegen die erwartete
  Extension (PNG, JPEG, GIF, PDF, ZIP, OOXML, Legacy-Office).
- `_save_upload()` in `freigaben.py` und `_save_test_upload()` in
  `tests.py` rufen den Helper nach der Extension-Prüfung auf. Bei
  Fehlschlag wird die Datei **nicht gespeichert** und per
  `app.logger.warning` protokolliert.
- Text-Formate (`txt`, `csv`) sind davon ausgenommen, weil sie keine
  zuverlässigen Magic-Bytes haben.

**Verifikation**: Upload `evil.svg` umbenannt nach `evil.png` wird
abgewiesen und erzeugt eine Warn-Meldung im App-Log.

### 6a.5 VULN-019 – Jinja-Interpolation in inline-Event-Handlern ✅ BEHOBEN

**Beschreibung**: In `funde/list.html` (`onclick="toggleDupGroup('{{ gid }}',
this)"`) und `admin/mail.html` (`onclick="… input[name={{ subj_key }}]
…"`) flossen Jinja-Werte in JavaScript-String-Kontexte. Das Auto-
Escaping ist dort nicht ausreichend, weil die HTML-Quote-Substitution
nicht die JS-String-Semantik kennt.

**Umgesetzte Remediation**:
- `funde/list.html`: Gruppen-Header-Zeile nutzt `data-gid`. Event-
  Delegation auf Dokumentebene ruft `toggleDupGroup(header.dataset.gid)`.
- `admin/mail.html`: „Reset-Button" nutzt `data-subj-key` / `data-body-key`
  und wird von einem ausgelagerten Script bedient.

### 6a.6 VULN-020 – Dynamische `IN (…)`-SQL-Fragmente ✅ BEHOBEN

**Beschreibung**: Mehrere Stellen bauten die Platzhalter einer
`WHERE col IN (…)`-Klausel per f-String (`ph = ",".join("?"*len(ids))`).
Das ist funktional korrekt, aber fragil (leere Liste → SQL-Syntax-
fehler) und wirkt wie SQL-Injection auf einen Reviewer.

**Umgesetzte Remediation**:
- `webapp/security.py :: in_clause(values)` liefert für leere Listen
  `("NULL", [])` (always-false-Prädikat) und für nicht-leere Listen
  das passende Platzhalterfragment.
- **Flächendeckend** ausgerollt in
  - `idv.list_idv` (Filter "unvollstaendig") und `idv.new_idv`
    (Extra-Datei-IDs);
  - `freigaben._phase1_komplett_erledigt` / `_phase2_komplett_erledigt`;
  - `funde.*` (Reaktivierung, Bulk-Aktionen, Zusammenfassen);
  - `cognos.*` (Zusammenfassen + Bulk-Aktionen);
  - `admin.bulk_persons` und `admin.bulk_gps`.
- Verifizierender Grep `",".join("?" * len` → 0 Treffer.

### 6a.7 VULN-021 – Logout via GET ✅ BEHOBEN

**Beschreibung**: `/logout` war per GET erreichbar. Ein
externer `<img src="/logout">` konnte damit einen authentifizierten
Benutzer unfreiwillig abmelden.

**Umgesetzte Remediation**:
- `webapp/routes/auth.py :: logout()` akzeptiert nur noch `POST`.
- `base.html`: Der „Abmelden"-Link in der Sidebar ist ein CSRF-
  geschütztes Formular mit `<button type="submit">`.

### 6a.8 VULN-022 – Admin-RCE-Vektor via Sidecar-ZIP-Upload 📋 Opt-out

**Beschreibung**: Admins können über `/admin/update/upload` eine
ZIP-Datei hochladen, die anschließend per `_SidecarFinder` vor den
gebündelten Modulen geladen wird (per Design: so funktioniert das
Update-System ohne EXE-Austausch). Dadurch ist ein kompromittierter
Admin-Account gleichbedeutend mit Remote-Code-Execution auf dem
Server.

**Entscheidung des Auftraggebers**: Die Funktion bleibt unverändert,
weil sie für das Betriebsmodell (Updates ohne EXE-Neubuild) essenziell
ist. Es wird jedoch ein **Opt-out** per `config.json` bereitgestellt,
damit regulierte Umgebungen die Upload-Funktion deaktivieren können.

**Umgesetzte Remediation**:
- `config.json.example`: neuer Schalter
  `"IDV_ALLOW_SIDECAR_UPDATES": 1` (Default aktiv). Auf `0` gesetzt
  wird das Upload-Verhalten komplett unterbunden.
- `webapp/routes/admin.py :: update_upload()` liest den Schalter über
  `_sidecar_updates_enabled()` und weist Anfragen bei `0` mit
  `flash(…, "error")` + Log-Warnung ab. Rollback (`update_rollback`)
  bleibt aus Betriebs­sicherheitsgründen aktiv.
- `webapp/templates/admin/update.html` zeigt eine deutliche
  Warn-Box, wenn der Upload deaktiviert ist, inkl. Anleitung zum
  Wiedereinschalten.

**Restrisiko (bei aktivem Upload)**: Admin-Account-Kompromittierung
erlaubt RCE. Kompensierend: `@admin_required`, CSRF-Schutz (VULN-002),
Login-Rate-Limit (VULN-006), Audit-Logs. In regulierten Umgebungen
sollte der Schalter auf `0` gesetzt und Updates ausschließlich über
signierte EXE-Builds eingespielt werden.

## 7 OWASP-Top-10-Abdeckung

| OWASP 2021 | Status | Bemerkung |
|---|---|---|
| A01 Broken Access Control | ✅ Geschützt | CSRF aktiv (VULN-002); Ownership-Guards (VULN-017); Logout via POST (VULN-021) |
| A02 Cryptographic Failures | ✅ Geschützt | pbkdf2:sha256 (VULN-001); SMTP/LDAP-PW Fernet (VULN-007) |
| A03 Injection | ✅ Geschützt | Parametrisierte Queries; Auto-Escaping; bleach für Rich-Text (VULN-015); IN-Clause-Helper (VULN-020); Event-Delegation (VULN-019) |
| A04 Insecure Design | ✅ Bedacht | Funktionstrennung, 4-Augen-Prinzip, Upload-MIME-Check (VULN-018) |
| A05 Security Misconfiguration | ✅ Geschützt | SECRET_KEY-Startup-Check (VULN-004); Security-Header + nonce-CSP (VULN-008) |
| A06 Vulnerable Components | ✅ Geprüft | Aktuelle Paketversionen; monatlicher `pip-audit` |
| A07 Identification/Authentication | ✅ Geschützt | Hashing (VULN-001), Rate-Limiting (VULN-006), Session-Timeout (VULN-013), keine Demo-User (VULN-003) |
| A08 Software/Data Integrity | 🛡 Teilweise | Signierte Sessions; Sidecar-Whitelist + Opt-out (VULN-022); Signatur-Pinning offen |
| A09 Logging/Monitoring | 🛡 Teilweise | Logs + Login-Audit vorhanden; VULN-011 Exception-Logging in kritischen Pfaden behoben; SIEM-Integration empfohlen |
| A10 SSRF | ✅ Nicht anwendbar | Keine URL-Fetches aus Nutzereingaben |

## 8 Abhängigkeiten – bekannte CVEs

Zum Zeitpunkt der Analyse sind in den direkt genutzten Paketversionen
keine kritischen CVEs bekannt. Monatlicher Scan mit:

```bash
pip install pip-audit
pip-audit -r requirements.txt
```

Ergebnisse sind zu dokumentieren und in das IT-Sicherheits-Reporting
aufzunehmen.

## 9 Remediation-Roadmap

### Sprint 1 (vor Go-Live, Pflicht)

- [x] **VULN-001** Passwort-Hashing auf pbkdf2:sha256 + Rehash-on-Login
- [x] **VULN-002** CSRF-Schutz aktiviert (Flask-WTF)
- [x] **VULN-003** Demo-Zugangsdaten entfernt; lokale Benutzer via `config.json`
- [x] **VULN-004** `SECRET_KEY`-Check beim Start erzwungen
- [x] **VULN-005** Prominente Debug-Warnung beim Start
- [x] **VULN-015** Stored XSS in `nachweise_text` gefixt (bleach)
- [x] **VULN-016** IDOR/Path-Traversal am Nachweis-Download gefixt
- [x] **VULN-017** Ownership-Guards an allen schreibenden IDV-Routen
- [x] **VULN-021** Logout via POST (CSRF-geschützt)
- [ ] Produktiv-Zertifikat aus interner CA einbinden (Betrieb)
- [ ] `SECRET_KEY` aus HSM/KeyVault (Betrieb)

### Sprint 2 (30 Tage)

- [x] **VULN-006** Rate-Limiting am Login (Flask-Limiter)
- [x] **VULN-012** LDAP `ssl_verify`-Default `1` + Warnhinweise
- [x] **VULN-013** Session-Timeouts konfiguriert
- [x] **VULN-018** Upload-Magic-Byte-Prüfung
- [x] **VULN-022** Sidecar-Update-Opt-out per `config.json`
- [ ] Monatlicher pip-audit-Lauf etabliert (Betrieb)

### Sprint 3 (90 Tage)

- [x] **VULN-007** SMTP-Passwort Fernet-verschlüsselt
- [x] **VULN-008** HTTP-Security-Header inkl. nonce-basiertem CSP
- [x] **VULN-009** Upload-Rate-Limit (Flask-Limiter) auf Admin-Upload-Routen
- [x] **VULN-010** Eingabelängen-/Steuerzeichen-Validierung (globaler `before_request`-Hook)
- [x] **VULN-011** Exception-Handling in kritischen Pfaden konkretisiert + Logging
- [x] **VULN-019** Jinja-Interpolation aus inline-`onclick` entfernt
- [x] **VULN-020** `in_clause()`-Helper flächendeckend ausgerollt
- [x] Alle inline Event-Handler auf Event-Delegation umgestellt; `script-src-attr 'unsafe-inline'` aus CSP entfernt

### Sprint 4 (bis nächster Major-Release)

- [ ] VULN-011 (Rest) Exception-Handling in Read-only-/Config-Pfaden konkretisieren
- [ ] VULN-014 Test-Suite pytest mit ≥ 70 % Abdeckung der Kernlogik
- [ ] Statische Analyse in CI (ruff, bandit, mypy)
- [ ] `admin.py`-Refaktorierung nach Fachdomänen
- [ ] Argon2id anstelle pbkdf2:sha256 (Zukunftsfähigkeit)
- [ ] Externer Penetrationstest beauftragt
- [ ] Flask-Limiter-Storage auf Redis umstellen (Mehrfach-Worker)
- [ ] WTForms/Pydantic-Validatoren je Route für **semantische** Validierung (Typen, Wertebereiche) – die reine Längenprüfung aus VULN-010 läuft bereits global

## 10 Restrisiken

Nach Umsetzung der Roadmap verbleibende Restrisiken:

| Restrisiko | Bewertung | Kompensation |
|---|---|---|
| SQLite-Datei auf Windows-Share kompromittierbar | Mittel | Dateisystemrechte, NTFS-ACL, Verschlüsselung der Festplatte |
| Kein Client-Zertifikats-Auth | Niedrig | LDAP + Session ausreichend |
| Kein Hardware-Security-Modul | Niedrig | `SECRET_KEY` aus KeyVault in Betriebsumgebung |
| Keine Mehrfaktor-Authentifizierung | Mittel | MFA auf Windows-Login-Ebene; mittelfristig idvault-seitig umsetzen |

## 11 Verantwortlichkeiten

| Thema | Verantwortlich |
|---|---|
| Code-Änderungen | Entwicklungsteam |
| Konfigurations-Hardening | IT-Sicherheit / Betrieb |
| Schwachstellenmanagement-Prozess | ISB |
| Pentest-Beauftragung | IT-Sicherheit |
| Freigabe der Roadmap | Geschäftsleitung |

## 12 Prüfungsvermerk

Diese Schwachstellenanalyse wurde erstellt im Rahmen der
Dokumentations-Neuerstellung und bewertet den Stand des Quellcodes zum
Datum der Dokumentation. Eine Aktualisierung nach jedem Release ist
erforderlich.

| Rolle | Name | Datum | Unterschrift |
|---|---|---|---|
| Ersteller | | | |
| IT-Sicherheit (Prüfung) | | | |
| Freigabe durch ISB | | | |
