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

| Nr. | Titel | Severity | OWASP | CWE | Status |
|---|---|:---:|---|---|---|
| VULN-001 | Schwaches Passwort-Hashing (SHA-256 ohne Salt) | **Kritisch** | A07:2021 | CWE-916 | Offen |
| VULN-002 | Fehlender CSRF-Schutz | **Kritisch** | A01:2021 | CWE-352 | Offen |
| VULN-003 | Hardcodierte Demo-Zugangsdaten | **Kritisch** | A07:2021 | CWE-798 | Offen (Betrieb) |
| VULN-004 | Standard-`SECRET_KEY` im Quellcode | **Kritisch** | A02:2021 | CWE-798 | Offen (Betrieb) |
| VULN-005 | Aktivierbarer Debug-Modus | **Hoch** | A05:2021 | CWE-489 | Betriebsauflage |
| VULN-006 | Fehlendes Rate-Limiting am Login | **Hoch** | A07:2021 | CWE-307 | Offen |
| VULN-007 | SMTP-Passwort im Klartext in DB | **Mittel** | A02:2021 | CWE-312 | Offen |
| VULN-008 | Fehlende HTTP-Security-Header | **Mittel** | A05:2021 | CWE-693 | Offen |
| VULN-009 | Upload-Größe 32 MB ohne Rate-Limiting | **Mittel** | A05:2021 | CWE-770 | Offen |
| VULN-010 | Unvalidierte Eingaben (Längen/Format) | **Mittel** | A03:2021 | CWE-20 | Offen |
| VULN-011 | Generisches Exception-Handling | **Mittel** | A09:2021 | CWE-391 | Offen |
| VULN-012 | LDAP-Zertifikatsprüfung per Default nicht erzwungen | **Mittel** | A02:2021 | CWE-295 | Offen |
| VULN-013 | Keine Session-Idle-Timeouts | **Niedrig** | A07:2021 | CWE-613 | Offen |
| VULN-014 | Keine automatisierten Tests / Security-Tests | **Niedrig** | A08:2021 | CWE-1173 | Offen |

## 3 Detailbeschreibung der kritischen Schwachstellen

### 3.1 VULN-001 – Schwaches Passwort-Hashing

**Beschreibung**: Lokale Passwörter werden mit einem einfachen
SHA-256-Hash (ohne Salt und ohne Streckung) abgelegt
(`webapp/routes/auth.py`). SHA-256 ist für Passwort-Hashing
ungeeignet, da es zu schnell berechenbar ist; ohne Salt sind
Rainbow-Table-Angriffe trivial.

**Wirkung**: Bei einem Datenbank-Leak könnten gängige Passwörter in
Sekunden gebrochen werden.

**Remediation**:
1. `argon2-cffi` oder `bcrypt` in `requirements.txt` aufnehmen
2. `webapp/routes/auth.py` auf `argon2.PasswordHasher` umstellen
3. Rehash-on-Login-Migration für Bestandspasswörter
4. Spalte `password_hash_algorithm` ergänzen, um Hybrid-Betrieb zu
   ermöglichen

**Aufwand**: ca. 1–2 Personentage
**Priorität**: vor Go-Live

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

### 3.3 VULN-003 – Hardcodierte Demo-Zugangsdaten

**Beschreibung**: Die Anwendung liefert drei Demo-Zugänge
(`admin / idvault2025`, `koordinator / demo`, `fachverantwortlicher /
demo`) im Quellcode (`webapp/routes/auth.py`, `run.py:228`).

**Wirkung**: Bei einer Produktivinstallation ohne Deaktivierung bleibt
ein **bekannter Administrator-Zugang** bestehen.

**Remediation**:
1. Feature-Flag `IDV_ENABLE_DEMO_ACCOUNTS` einführen, Default `0`
2. Demo-Accounts nur bei leerer Personen-Tabelle aktiv
3. Automatische Deaktivierung nach erster erfolgreicher Personen-Anlage
4. Prominente Warnung im Admin-Dashboard, solange Demo-Accounts aktiv

**Aufwand**: ca. 0,5 Personentage
**Priorität**: vor Go-Live – Betriebsauflage

### 3.4 VULN-004 – Standard-`SECRET_KEY`

**Beschreibung**: Der Fallback-`SECRET_KEY` ist im Quellcode als
`"dev-change-in-production-!"` fest hinterlegt (`webapp/__init__.py`).
Wird die Umgebungsvariable nicht gesetzt, greift dieser Wert.

**Wirkung**: Sessions und Fernet-verschlüsselte Werte wären mit
öffentlich bekanntem Schlüssel gesichert.

**Remediation**:
1. Falls `SECRET_KEY` nicht gesetzt: Anwendungsstart **abbrechen** (nicht nur warnen)
2. Ausnahme nur bei `DEBUG=1`
3. Startup-Check in `run.py` ergänzen
4. Betriebsdokumentation: `SECRET_KEY` zwingend als Umgebungsvariable

**Aufwand**: ca. 0,25 Personentage
**Priorität**: vor Go-Live

## 4 Detailbeschreibung der Schwachstellen hoher Priorität

### 4.1 VULN-005 – Aktivierbarer Debug-Modus

**Beschreibung**: Über `DEBUG=1` kann der Flask-Debug-Server aktiviert
werden, der bei Fehlern Stack-Traces im Browser anzeigt und eine
interaktive Debug-Konsole bereitstellt.

**Wirkung**: Information Disclosure; bei aktiver Debug-Konsole
Remote-Code-Execution über PIN-geschützten Endpunkt möglich.

**Remediation**:
- Betriebsauflage: `DEBUG` niemals setzen in Produktion
- Hinweis in Startmeldung, wenn Debug aktiv
- Build-Artefakt ohne Debug-Abhängigkeiten prüfen

**Aufwand**: ca. 0,25 Personentage
**Priorität**: 30 Tage

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

### 5.1 VULN-007 – SMTP-Passwort im Klartext

**Beschreibung**: Das SMTP-Passwort wird in `app_settings` als Klartext
gespeichert (`webapp/email_service.py`).

**Remediation**: Fernet-Verschlüsselung analog LDAP-Bind-Passwort
(`webapp/ldap_auth.py:_fernet()`).

**Aufwand**: ca. 0,5 Personentage · **Priorität**: 90 Tage

### 5.2 VULN-008 – Fehlende HTTP-Security-Header

**Beschreibung**: Keine Setzung von `Content-Security-Policy`,
`Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`,
`Referrer-Policy`, `Permissions-Policy`.

**Remediation**:
- Bei Betrieb hinter Reverse-Proxy: im Proxy setzen
- Bei direktem Betrieb: Flask-`after_request`-Hook oder `flask-talisman`

**Aufwand**: ca. 1 Personentag · **Priorität**: 90 Tage

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

### 5.6 VULN-012 – LDAP-Zertifikatsprüfung

**Beschreibung**: Das Feld `ldap_config.ssl_verify` ist konfigurierbar;
wird es deaktiviert, sind Man-in-the-Middle-Angriffe auf LDAPS möglich.

**Remediation**:
- Default auf `1` setzen
- Deaktivierung in UI mit Warnhinweis
- Loggen, wenn `ssl_verify=0` aktiv ist

## 6 Schwachstellen niedriger Priorität

### 6.1 VULN-013 – Session-Timeouts

**Beschreibung**: Es sind keine expliziten Session-Idle-Timeouts gesetzt.

**Remediation**:
```python
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=4)
app.config['SESSION_COOKIE_SECURE'] = True       # bei HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
```

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

- [ ] VULN-001 Passwort-Hashing auf Argon2id migrieren
- [ ] VULN-002 CSRF-Schutz aktivieren
- [ ] VULN-003 Demo-Zugangsdaten deaktivierbar machen
- [ ] VULN-004 `SECRET_KEY`-Check beim Start erzwingen
- [ ] VULN-005 Produktions-Deployment ohne `DEBUG`
- [ ] Produktiv-Zertifikat aus interner CA einbinden
- [ ] Demo-Zugänge in Produktivinstallation gelöscht
- [ ] `SECRET_KEY` aus HSM/KeyVault

### Sprint 2 (30 Tage)

- [ ] VULN-006 Rate-Limiting auf Login + Update-Upload
- [ ] VULN-012 LDAP-Verify-Default `1`
- [ ] VULN-013 Session-Timeouts konfigurieren
- [ ] Monatlicher pip-audit-Lauf etabliert

### Sprint 3 (90 Tage)

- [ ] VULN-007 SMTP-Passwort verschlüsseln
- [ ] VULN-008 HTTP-Security-Header
- [ ] VULN-009 Upload-Rate-Limiting
- [ ] VULN-010 Validierungslayer
- [ ] VULN-011 Konkretes Exception-Handling in Admin-Routen

### Sprint 4 (bis nächster Major-Release)

- [ ] VULN-014 Test-Suite pytest mit ≥ 70 % Abdeckung der Kernlogik
- [ ] Statische Analyse in CI (ruff, bandit, mypy)
- [ ] `admin.py`-Refaktorierung nach Fachdomänen
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
