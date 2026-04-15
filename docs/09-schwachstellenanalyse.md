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
- **Abgleich mit OWASP Top 10 (2021)**
- **Abgleich mit CWE (Common Weakness Enumeration)**
- **Review der Abhängigkeitsliste** (`requirements.txt`)
- **Review der Konfigurationsdefaults**

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
| VULN-002 | Fehlender CSRF-Schutz | **Kritisch** | A01:2021 | CWE-352 | ⏳ Offen – Roadmap Sprint 1 |
| VULN-003 | Hardcodierte Demo-Zugangsdaten | **Kritisch** | A07:2021 | CWE-798 | 📋 Bewusst beibehalten (Betriebsauflage) |
| VULN-004 | Standard-`SECRET_KEY` im Quellcode | **Kritisch** | A02:2021 | CWE-798 | ✅ Behoben (Startup-Check in run.py) |
| VULN-005 | Aktivierbarer Debug-Modus | **Hoch** | A05:2021 | CWE-489 | ✅ Behoben (prominente Start-Warnung) |
| VULN-006 | Fehlendes Rate-Limiting am Login | **Hoch** | A07:2021 | CWE-307 | ⏳ Offen – Roadmap Sprint 2 |
| VULN-007 | SMTP-Passwort im Klartext in DB | **Mittel** | A02:2021 | CWE-312 | ✅ Behoben (Fernet mit "enc:"-Präfix) |
| VULN-008 | Fehlende HTTP-Security-Header | **Mittel** | A05:2021 | CWE-693 | 🛡 Teilweise behoben (after_request-Hook; CSP mit unsafe-inline) |
| VULN-009 | Upload-Größe 32 MB ohne Rate-Limiting | **Mittel** | A05:2021 | CWE-770 | ⏳ Offen – adressiert mit VULN-006 |
| VULN-010 | Unvalidierte Eingaben (Längen/Format) | **Mittel** | A03:2021 | CWE-20 | ⏳ Offen – Roadmap Sprint 3 |
| VULN-011 | Generisches Exception-Handling | **Mittel** | A09:2021 | CWE-391 | ⏳ Offen – Roadmap Sprint 3 |
| VULN-012 | LDAP-Zertifikatsprüfung deaktivierbar | **Mittel** | A02:2021 | CWE-295 | ✅ Behoben (Default=1, Warnungen im Log + UI) |
| VULN-013 | Keine Session-Idle-Timeouts | **Niedrig** | A07:2021 | CWE-613 | ✅ Behoben (4 h Lifetime + Cookie-Flags) |
| VULN-014 | Keine automatisierten Tests / Security-Tests | **Niedrig** | A08:2021 | CWE-1173 | ⏳ Offen – Roadmap Sprint 4 |

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

### 3.2 VULN-002 – Fehlender CSRF-Schutz

**Beschreibung**: POST-Formulare sind nicht mit CSRF-Tokens
abgesichert. Ein Angreifer kann einen angemeldeten Nutzer dazu
bringen, ungewollt zustandsändernde Anfragen (z. B. Status-Änderung,
Personen-Anlage) auszulösen.

**Wirkung**: Cross-Site-Request-Forgery-Angriffe sind bei ungeschützter
Browser-Session möglich.

**Remediation**:
1. `flask-wtf` aufnehmen (`pip install flask-wtf`)
2. In `webapp/__init__.py` `CSRFProtect(app)` aktivieren
3. In jedem POST-Formular `{{ csrf_token() }}` einfügen
4. AJAX-POST-Requests (z. B. Scanner-Start) mit `X-CSRFToken`-Header versehen
5. Regressionstest aller ~50 POST-Endpunkte

**Aufwand**: ca. 2–3 Personentage
**Priorität**: vor Go-Live

### 3.3 VULN-003 – Hardcodierte Demo-Zugangsdaten 📋 BEWUSST BEIBEHALTEN

**Beschreibung**: Die Anwendung liefert drei Demo-Zugänge
(`admin / idvault2026`, `koordinator / demo`, `fachverantwortlicher /
demo`) im Quellcode (`webapp/routes/auth.py`, `run.py:228`).

**Wirkung**: Bei einer Produktivinstallation ohne Deaktivierung bleibt
ein **bekannter Administrator-Zugang** bestehen.

**Entscheidung**: Die Demo-Zugänge bleiben auf ausdrücklichen Wunsch
des Auftraggebers **aktiv**, um den Erstzugang in abgeschotteten
Installations-umgebungen (keine SSO, keine Passwort-Resets per E-Mail)
jederzeit zu ermöglichen.

**Kompensierende Maßnahmen**:
- Die Demo-Zugänge greifen nur, wenn entweder kein passender
  DB-Eintrag vorhanden ist oder dessen Passwort nicht gesetzt ist
  (`_do_local_login` versucht zunächst die DB).
- Der erste Administrator muss zwingend ein neues Admin-Passwort
  setzen (Betriebsauflage).
- Die Nutzung des Demo-Zugangs wird im Login-Audit-Log als
  Methode "Demo" gekennzeichnet und ist damit jederzeit nachweisbar.

**Restrisiko**: Solange die Demo-Passwörter in der Auslieferung
enthalten sind, besteht das Risiko eines trivialen Passwort-Angriffs
auf neu installierte Systeme, in denen ein Administrator kein
eigenes Passwort gesetzt hat. Dieses Risiko ist dokumentiert und wird
vom Auftraggeber getragen.

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

### 4.2 VULN-006 – Fehlendes Rate-Limiting am Login

**Beschreibung**: Der Login-Endpunkt erlaubt unbegrenzte Anmeldeversuche.
Automatisierte Brute-Force-Angriffe werden nicht verhindert.

**Wirkung**: Passwort-Erraten bei schwachen lokalen Passwörtern; LDAP-Account-Lockout-Auslösung.

**Remediation**:
1. `flask-limiter` aufnehmen
2. Login-Endpunkt: `@limiter.limit("5 per minute")`
3. Weitere sicherheitskritische Endpunkte (Update-Upload, Notfall-Zugang) analog begrenzen
4. IP + User-ID als Kombination im Limit-Schlüssel

**Aufwand**: ca. 1 Personentag
**Priorität**: 30 Tage

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

### 5.2 VULN-008 – Fehlende HTTP-Security-Header 🛡 TEILWEISE BEHOBEN

**Beschreibung**: Keine Setzung von `Content-Security-Policy`,
`Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`,
`Referrer-Policy`, `Permissions-Policy`.

**Umgesetzte Remediation**:
- `webapp/__init__.py`: `after_request`-Hook setzt bei jeder Antwort
  folgende Header (mit `setdefault`, um explizit gesetzte
  Template-Header nicht zu überschreiben):
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: geolocation=(), microphone=(), camera=()`
  - `Content-Security-Policy: default-src 'self'; img-src 'self' data:;
    style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline';
    font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'`
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains`
    (nur wenn `IDV_HTTPS=1`, um HTTP-Betrieb nicht auszusperren)

**Restrisiko**: Die CSP enthält `unsafe-inline` für Scripts und
Styles, weil einige Templates inline-`onclick`- und `<script>`-Blöcke
sowie `style=`-Attribute nutzen. Der vollständige Schutz gegen DOM-XSS
erfordert eine Migration auf nonce- oder hash-basiertes CSP; dies ist
eine separate Arbeitspaket-Position (Roadmap Sprint 4).

**Verifikation**: `curl -I http://.../auth/login` zeigt die gesetzten
Header; Smoketest im Test-Client grün.

### 5.3 VULN-009 – Upload-Größe ohne Rate-Limiting

**Beschreibung**: 32 MB Upload-Größe pro Request × viele Requests
ermöglichen Resource-Exhaustion-Angriffe.

**Remediation**: Zusätzlich zu Größenlimit auch Rate-Limiting auf
Upload-Endpunkte (VULN-006).

### 5.4 VULN-010 – Unvalidierte Eingaben

**Beschreibung**: Eingabewerte werden teilweise ohne Längen-/Format-
Prüfung in die Datenbank übernommen.

**Remediation**: Einführung eines Validierungslayers (WTForms
zusammen mit Flask-WTF aus VULN-002; oder `pydantic`).

### 5.5 VULN-011 – Generisches Exception-Handling

**Beschreibung**: `except Exception: pass` führt zu stillen
Fehlschlägen, die im Log nicht sichtbar werden.

**Remediation**: Konkrete Exception-Typen; Logging statt `pass`.

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

## 7 OWASP-Top-10-Abdeckung

| OWASP 2021 | Status | Bemerkung |
|---|---|---|
| A01 Broken Access Control | ⚠️ Teilweise | Rollenmodell ok; CSRF (VULN-002) offen |
| A02 Cryptographic Failures | ⚠️ Teilweise | SHA-256 (VULN-001), SMTP-PW (VULN-007) |
| A03 Injection | ✅ Geschützt | Parametrisierte Queries; Auto-Escaping |
| A04 Insecure Design | ✅ Bedacht | Funktionstrennung, 4-Augen-Prinzip |
| A05 Security Misconfiguration | ⚠️ Teilweise | Default-`SECRET_KEY`, Demo-Accounts, Debug |
| A06 Vulnerable Components | ✅ Geprüft | Aktuelle Paketversionen |
| A07 Identification/Authentication | ⚠️ Teilweise | Rate-Limiting, Hashing, Session-Timeout |
| A08 Software/Data Integrity | ✅ Geschützt | Signierte Sessions; Sidecar-Whitelist |
| A09 Logging/Monitoring | ⚠️ Teilweise | Logs vorhanden; SIEM-Integration empfohlen |
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
- [ ] VULN-002 CSRF-Schutz aktivieren
- [ ] VULN-003 Demo-Zugangsdaten – *bewusst beibehalten* (siehe 3.3)
- [x] **VULN-004** `SECRET_KEY`-Check beim Start erzwingen
- [x] **VULN-005** Prominente Debug-Warnung beim Start
- [ ] Produktiv-Zertifikat aus interner CA einbinden (Betrieb)
- [ ] `SECRET_KEY` aus HSM/KeyVault (Betrieb)

### Sprint 2 (30 Tage)

- [ ] VULN-006 Rate-Limiting auf Login + Update-Upload
- [x] **VULN-012** LDAP `ssl_verify`-Default `1` + Warnhinweise
- [x] **VULN-013** Session-Timeouts konfiguriert
- [ ] Monatlicher pip-audit-Lauf etabliert (Betrieb)

### Sprint 3 (90 Tage)

- [x] **VULN-007** SMTP-Passwort Fernet-verschlüsselt
- [x] **VULN-008** HTTP-Security-Header (CSP mit `unsafe-inline` – Rest in Sprint 4)
- [ ] VULN-009 Upload-Rate-Limiting (zusammen mit VULN-006)
- [ ] VULN-010 Validierungslayer (WTForms/Pydantic)
- [ ] VULN-011 Konkretes Exception-Handling in Admin-Routen

### Sprint 4 (bis nächster Major-Release)

- [ ] VULN-014 Test-Suite pytest mit ≥ 70 % Abdeckung der Kernlogik
- [ ] Statische Analyse in CI (ruff, bandit, mypy)
- [ ] `admin.py`-Refaktorierung nach Fachdomänen
- [ ] CSP auf nonce-/hash-basiertes Verfahren umstellen (ohne `unsafe-inline`)
- [ ] Argon2id anstelle pbkdf2:sha256 (Zukunftsfähigkeit)
- [ ] Externer Penetrationstest beauftragt

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
