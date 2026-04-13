# idvault

Erfassung, Klassifizierung und Überwachung von Individuellen Datenverarbeitungen (IDV)
nach **MaRisk AT 7.2** und **DORA** — gebaut für Volksbanken und Sparkassen.

---

## Schnellstart

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:5000
# → Login: admin / idvault2025
```

---

## Benutzer- und Berechtigungskonzept

### Rollen

idvault unterscheidet fünf Rollen. Jede Person erhält genau eine Rolle,
die in der Mitarbeiterverwaltung (`Administration → Personen`) hinterlegt wird.

| Rolle | Beschreibung |
|---|---|
| **IDV-Administrator** | Systemadministration; vollständiger Zugriff auf alle Funktionen und den Admin-Bereich |
| **IDV-Koordinator** | Verantwortlich für das IDV-Register; schreibt und genehmigt alle IDVs |
| **Fachverantwortlicher** | Erstellt und pflegt eigene IDVs; sieht nur IDVs, in denen er als Fachverantwortlicher, Entwickler, Koordinator oder Stellvertreter eingetragen ist |
| **Revision** | Lesender Zugriff auf alle IDVs; keine Bearbeitungsmöglichkeit |
| **IT-Sicherheit** | Lesender Zugriff auf alle IDVs; keine Bearbeitungsmöglichkeit |

### Berechtigungsmatrix

| Funktion | Administrator | Koordinator | Fachverantwortlicher | Revision | IT-Sicherheit |
|---|:---:|:---:|:---:|:---:|:---:|
| Dashboard anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ |
| Alle IDVs anzeigen | ✓ | ✓ | — | ✓ | ✓ |
| Eigene IDVs anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ |
| IDV anlegen / bearbeiten | ✓ | ✓ | — | — | — |
| IDV-Status ändern | ✓ | ✓ | — | — | — |
| Prüfungen anlegen | ✓ | ✓ | — | — | — |
| Prüfungen anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ |
| Maßnahmen anlegen | ✓ | ✓ | — | — | — |
| Maßnahmen anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ |
| Scanner-Funde anzeigen | ✓ | ✓ | ✓ | ✓ | ✓ |
| Scan starten (Schaltfläche in Scanner-Views) | ✓ | ✓ | — | — | — |
| IDV aus Scannerfund registrieren | ✓ | ✓ | — | — | — |
| Excel-Export | ✓ | ✓ | ✓ | ✓ | ✓ |
| Administration (Stammdaten) | ✓ | ✓ | — | — | — |
| Stammdaten löschen / deaktivieren | ✓ | — | — | — | — |
| E-Mail-Einstellungen (SMTP) | ✓ | — | — | — | — |
| Mitarbeiter-Import (CSV) | ✓ | — | — | — | — |
| LDAP konfigurieren / Gruppen-Mapping | ✓ | — | — | — | — |
| Mitarbeiter aus LDAP importieren | ✓ | — | — | — | — |
| Lokalen Notfall-Zugang aktivieren | ✓ | — | — | — | — |

> **Eigene IDVs** = IDVs, in denen die Person als Fachverantwortlicher,
> Entwickler, IDV-Koordinator oder Stellvertreter eingetragen ist.

### Sichtbarkeit von IDVs

```
IDV-Administrator / Koordinator / Revision / IT-Sicherheit
  → sehen ALLE IDVs in der Grundgesamtheit

Fachverantwortlicher (und alle Rollen ohne eigene Kategorie)
  → sehen nur IDVs, bei denen gilt:
     fachverantwortlicher_id = eigene Person-ID
     ODER idv_entwickler_id  = eigene Person-ID
     ODER idv_koordinator_id = eigene Person-ID
     ODER stellvertreter_id  = eigene Person-ID
```

### Login und Benutzerverwaltung

**Produktivmodus:** Jede Person mit hinterlegter `User-ID` und gesetztem
Passwort kann sich einloggen. Das Passwort wird als SHA-256-Hash gespeichert
und niemals im Klartext abgelegt.

Passwörter werden über `Administration → Person bearbeiten` gesetzt.

**LDAP / Active Directory:** Wenn LDAP aktiviert ist, erfolgt die Anmeldung
mit dem Windows-Benutzernamen und -Passwort — kein separates idvault-Passwort
nötig. Bei einem LDAP-Serverausfall wechselt idvault automatisch auf lokale
Passwörter. Für einen Bypass bei laufendem LDAP-Server steht ein manuell
aktivierbarer Notfall-Zugang bereit.
→ Einrichtung: [LDAP / Active Directory](#ldap--active-directory)

**Demo-Fallback** (für Erstinstallation / wenn keine Persons-Einträge mit
Passwort vorhanden sind):

| Benutzername | Passwort | Rolle |
|---|---|---|
| `admin` | `idvault2025` | IDV-Administrator |
| `koordinator` | `demo` | IDV-Koordinator |
| `fachverantwortlicher` | `demo` | Fachverantwortlicher |

> Demo-Passwörter vor dem Produktiveinsatz deaktivieren: Personen mit User-ID
> und Passwort anlegen, danach DEMO_USERS in `webapp/routes/auth.py` leeren.

---

## LDAP / Active Directory

idvault kann Benutzer direkt gegen ein Active Directory authentifizieren — per
**LDAPS (Port 636)**, ohne Browser-Redirect und ohne separates idvault-Passwort.
Benutzer geben weiterhin Benutzername und Passwort in das gewohnte Login-Formular
ein; idvault prüft die Credentials im Hintergrund per LDAP-Bind.

### Voraussetzungen

- Zugang zu einem LDAP-Server (Active Directory) mit LDAPS (Port 636)
- Ein Service-Account (technischer Benutzer) mit Leserechten auf das Verzeichnis
- AD-Gruppen, denen die Benutzer zugeordnet werden sollen (eine Gruppe je idvault-Rolle)

### Einrichtung (Schritt für Schritt)

**1. Abhängigkeiten installieren**

```bash
pip install -r requirements.txt
```

Die Pakete `ldap3` und `cryptography` werden automatisch mit installiert.

**2. LDAP-Server konfigurieren**

```
Administration → LDAP / Active Directory → LDAP konfigurieren
```

| Feld | Beschreibung | Beispiel |
|---|---|---|
| LDAP aktivieren | Schaltet die LDAP-Authentifizierung ein/aus | ☑ |
| Server-URL | Adresse des LDAP-Servers (mit Protokoll) | `ldaps://ldap.ihre-bank.de` |
| Port | LDAPS: 636 (Standard), LDAP: 389 | `636` |
| Base-DN | Suchbasis für Benutzerkonten | `OU=Benutzer,DC=ihre-bank,DC=de` |
| Technischer Benutzer (Bind-DN) | Service-Account für LDAP-Suche | `CN=svc-idvault,OU=ServiceAccounts,DC=ihre-bank,DC=de` |
| Kennwort | Passwort des Service-Accounts (verschlüsselt gespeichert) | |
| Benutzer-Attribut | Attribut für den Anmeldenamen | `sAMAccountName` (empfohlen) |
| TLS-Zertifikat prüfen | Zertifikat des Servers verifizieren (empfohlen) | ☑ |

Über den Button **„Verbindung testen"** kann die Konfiguration direkt geprüft
werden, ohne LDAP aktivieren zu müssen.

**3. Gruppen-Rollen-Mapping anlegen**

```
Administration → LDAP / Active Directory → Gruppen-Mapping
```

Jede idvault-Rolle wird einer AD-Gruppe zugeordnet. Benutzer erhalten automatisch
die Rolle der ersten passenden Gruppe (Reihenfolge ist konfigurierbar).

| idvault-Rolle | Beispiel-Gruppen-DN |
|---|---|
| IDV-Administrator | `CN=IDV-Administratoren,OU=Gruppen,DC=ihre-bank,DC=de` |
| IDV-Koordinator | `CN=IDV-Koordinatoren,OU=Gruppen,DC=ihre-bank,DC=de` |
| Fachverantwortlicher | `CN=IDV-Fachverantwortliche,OU=Gruppen,DC=ihre-bank,DC=de` |
| Revision | `CN=IDV-Revision,OU=Gruppen,DC=ihre-bank,DC=de` |
| IT-Sicherheit | `CN=IDV-IT-Sicherheit,OU=Gruppen,DC=ihre-bank,DC=de` |

> Den vollständigen DN einer Gruppe ermitteln Sie in PowerShell:
> ```powershell
> Get-ADGroup -Identity "IDV-Administratoren" | Select DistinguishedName
> ```

**4. LDAP aktivieren und speichern**

Sobald mindestens ein Gruppen-Mapping vorhanden ist, kann LDAP aktiviert werden.
Das Login-Formular zeigt dann den Hinweis „Active Directory aktiv".

### Login-Ablauf

```
1. Benutzer gibt AD-Anmeldename + Windows-Passwort in idvault ein
2. idvault verbindet per LDAPS mit dem konfigurierten Server
3. Service-Account sucht den Benutzer per sAMAccountName
4. LDAP-Bind mit dem gefundenen User-DN + eingegebenem Passwort
   (prüft Credentials; das Passwort verlässt idvault nie im Klartext)
5. Bei Erfolg: Gruppen-Mitgliedschaften auslesen (memberOf)
6. Gruppen-DNs mit dem Mapping abgleichen → idvault-Rolle bestimmen
7. Person in idvault automatisch anlegen oder aktualisieren (JIT)
8. Session setzen, weiterleiten zum Dashboard
```

### Automatische Benutzeranlage (JIT Provisioning)

Beim ersten erfolgreichen LDAP-Login wird die Person automatisch in der
`persons`-Tabelle angelegt — kein manueller CSV-Import nötig. Folgende
Felder werden aus dem AD-Konto übernommen:

| AD-Attribut | idvault-Feld |
|---|---|
| `givenName` | Vorname |
| `sn` (surname) | Nachname |
| `mail` | E-Mail |
| `telephoneNumber` | Telefon |
| `sAMAccountName` | User-ID, AD-Name |

Bei späteren Logins werden die Stammdaten (Name, E-Mail, Telefon) aktualisiert,
falls sie sich im AD geändert haben. Die Rolle wird ebenfalls angepasst, wenn
sich die Gruppen-Mitgliedschaft geändert hat.

### Fallback und Notfall-Zugang

#### Automatischer Fallback bei LDAP-Serverausfall

Ist der LDAP-Server **nicht erreichbar** (Netzwerkfehler, Server down), wechselt
idvault automatisch auf den lokalen Login — ohne Konfigurationsänderung. In diesem
Fall greifen:

- Personen mit gesetztem `password_hash` (vergeben über `Administration → Person bearbeiten`)
- Der eingebaute Demo-Account `admin` / `idvault2025`

#### Manuell aktivierbarer Notfall-Zugang

Wenn der LDAP-Server **erreichbar** ist, aber ein lokaler Bypass benötigt wird
(z.B. für einen Break-Glass-Account), kann im Admin-Bereich ein Notfall-Zugang
freigeschaltet werden:

```
Administration → LDAP / Active Directory → „Lokalen Notfall-Zugang im Login-Fenster anzeigen" aktivieren
```

Danach erscheint im Login-Formular ein aufklappbarer Bereich **„Lokaler Notfall-Zugang"**
mit einem eigenen Benutzername/Passwort-Formular. Dieser Pfad umgeht LDAP vollständig
und prüft ausschließlich den lokalen `password_hash`.

**Notfall-Konto einrichten:**

1. Person in idvault anlegen (oder vorhandene Person verwenden)
2. `Administration → Person bearbeiten → Passwort` setzen
3. Notfall-Zugang im Admin-Bereich aktivieren (s. o.)

> Den Toggle nur im Bedarfsfall aktivieren und nach dem Einsatz wieder deaktivieren.

#### LDAP deaktivieren

Unter `Administration → LDAP konfigurieren` kann LDAP jederzeit deaktiviert werden.
Alle vorhandenen Personenkonten und Passwort-Hashes bleiben erhalten.

### Mitarbeiter aus LDAP importieren

Über `Administration → LDAP / Active Directory → Mitarbeiter importieren` können alle
aktivierten AD-Benutzerkonten auf einmal in die idvault-Personen-Tabelle übernommen werden.

**Ablauf:**

1. Seite aufrufen — idvault lädt automatisch alle aktiven AD-Konten (deaktivierte
   Konten per `userAccountControl`-Flag werden übersprungen)
2. Optional: LDAP-Filter eingeben, um nur bestimmte Abteilungen zu laden,
   z.B. `(department=Finanzen)` oder `(&(l=Berlin)(department=IT))`
3. Benutzer in der Vorschautabelle auswählen (bereits vorhandene Personen sind grün markiert)
4. **„Ausgewählte importieren"** klicken

**Was passiert beim Import:**

| Situation | Ergebnis |
|---|---|
| Person mit gleicher User-ID noch nicht vorhanden | Neuanlage mit Kürzel aus Initialen |
| Person mit gleicher User-ID bereits vorhanden | Aktualisierung (Name, E-Mail, Telefon, Rolle) |
| Rolle aus Gruppen-Mapping ermittelbar | Rolle wird automatisch gesetzt |
| Kein passendes Gruppen-Mapping | Feld bleibt leer (manuell nachpflegen) |

> Der Import erfordert eine funktionierende LDAP-Konfiguration mit Service-Account.
> Als Alternative steht weiterhin der [CSV-Import](#mitarbeiterdaten-importieren-csv) zur Verfügung.

---

### Sicherheitshinweise

| Aspekt | Umsetzung |
|---|---|
| Transportverschlüsselung | LDAPS (TLS) auf Port 636 — Passwort wird niemals unverschlüsselt übertragen |
| Service-Account-Passwort | Fernet/AES-128-verschlüsselt in der Datenbank, Schlüssel aus `SECRET_KEY` abgeleitet |
| Benutzerkennwort | Wird nur für den LDAP-Bind verwendet und nicht gespeichert |
| Deaktivierte AD-Konten | Werden erkannt (userAccountControl-Flag) und abgewiesen |
| Kein passendes Gruppen-Mapping | Login schlägt mit Hinweismeldung fehl — kein stiller Zugriff |

> **Wichtig:** Den `SECRET_KEY` der Anwendung sicher und dauerhaft hinterlegen
> (Umgebungsvariable `SECRET_KEY`). Bei Änderung des Keys muss das
> Service-Account-Passwort unter `Administration → LDAP konfigurieren` neu
> eingegeben werden, da der gespeicherte verschlüsselte Wert nicht mehr lesbar ist.

---

## E-Mail-Benachrichtigungen

idvault kann automatisch E-Mails versenden — z.B. wenn der Scanner eine neue
Datei erkennt, eine Prüfung fällig wird oder eine Maßnahme überfällig ist.

### SMTP-Konfiguration

**Option A – Über die Administrationsoberfläche** (empfohlen):

```
Administration → E-Mail-Einstellungen (SMTP)
```

| Feld | Beschreibung |
|---|---|
| SMTP-Host | Mailserver-Adresse, z.B. `mail.volksbank.de` |
| Port | Standard: 587 (STARTTLS) oder 465 (SSL) |
| SMTP-Benutzer | Login-Konto am Mailserver |
| Passwort | Passwort des Mailkontos |
| Absenderadresse | `From:`-Adresse, z.B. `idvault@volksbank.de` |
| STARTTLS | Aktivieren für Port 587, deaktivieren für Port 465 (SSL) |
| Neue Dateien benachrichtigen | Aktiviert automatische Scanner-Benachrichtigungen |

**Option B – Umgebungsvariablen** (überschreiben die DB-Einstellungen):

```bash
IDV_SMTP_HOST=mail.volksbank.de
IDV_SMTP_PORT=587
IDV_SMTP_USER=idvault@volksbank.de
IDV_SMTP_PASSWORD=geheim
IDV_SMTP_FROM=idvault@volksbank.de
IDV_SMTP_TLS=1      # 1 = STARTTLS, 0 = SSL
```

### Benachrichtigungstypen

| Ereignis | Empfänger | Auslöser |
|---|---|---|
| Neue Datei im Scanner erkannt | Alle IDV-Koordinatoren und Administratoren mit hinterlegter E-Mail | Manuell über Button „Benachrichtigung senden" in den Scanner-Funden |
| Prüfung fällig | Fachverantwortlicher des IDV | Kann per Skript/Cronjob ausgelöst werden (API: `notify_review_due`) |
| Maßnahme überfällig | Verantwortlicher der Maßnahme | Kann per Skript/Cronjob ausgelöst werden (API: `notify_measure_overdue`) |

> Damit E-Mails ankommen, muss die E-Mail-Adresse der Person in der
> Mitarbeiterverwaltung hinterlegt sein (`Administration → Person bearbeiten → E-Mail`).

---

## Administration

### Stammdaten bearbeiten

Alle Stammdaten-Tabellen sind vollständig editierbar:

| Bereich | Anlegen | Bearbeiten | Deaktivieren |
|---|:---:|:---:|:---:|
| Personen | ✓ | ✓ | ✓ (nur Admin) |
| Organisationseinheiten | ✓ | ✓ | ✓ (nur Admin) |
| Geschäftsprozesse | ✓ | ✓ | ✓ (nur Admin) |
| Plattformen | ✓ | ✓ | ✓ (nur Admin) |

Deaktivierte Einträge werden in den Formularen nicht mehr zur Auswahl angeboten,
bleiben aber in historischen Daten erhalten (Referenzintegrität).

### Mitarbeiterverwaltung – Felder

Jede Person hat folgende Felder:

| Feld | Bedeutung |
|---|---|
| Kürzel | Eindeutiges 2–5-Buchstaben-Kürzel (z.B. `MMU`) |
| Nachname / Vorname | Klarer Name |
| E-Mail (SMTP-Adresse) | Für E-Mail-Benachrichtigungen |
| User-ID | Login-Name für idvault (z.B. `mmu`) |
| AD-Name | AD-Anmeldename (z.B. `mmu`); wird bei LDAP-Login automatisch befüllt und als stabiler Schlüssel für die Kontenzuordnung genutzt |
| Rolle | Eine der fünf Rollen (siehe Berechtigungskonzept) |
| Org-Einheit | Zugeordnete Abteilung / Bereich |
| Aktiv | Inaktive Personen können sich nicht einloggen |

---

## Mitarbeiterdaten importieren (CSV)

Über `Administration → Mitarbeiter aus CSV importieren` können Mitarbeiterdaten
aus einer CSV-Datei importiert werden.

### CSV-Format

Trennzeichen: **Semikolon** (`;`) oder **Komma** (`,`) — wird automatisch erkannt.
Zeichensatz: UTF-8 (mit oder ohne BOM).

**Spalten:**

| Spalte | Pflicht | Beschreibung | Alias |
|---|:---:|---|---|
| `user_id` | — | Login-Name für idvault | `userid`, `benutzername` |
| `email` | — | SMTP-E-Mail-Adresse | `smtp`, `smtp_adresse`, `mailadresse` |
| `ad_name` | — | AD-Kontoname | `adname`, `ad` |
| `oe_kuerzel` | — | Kürzel der Org-Einheit (muss in der OE-Tabelle vorhanden sein) | `oe`, `abteilung` |
| `nachname` | ✓* | Nachname | `name` |
| `vorname` | — | Vorname | |
| `kuerzel` | — | Eindeutiges Kürzel (wird aus `user_id` abgeleitet wenn leer) | |
| `rolle` | — | Rolle (Standard: `Fachverantwortlicher`) | |

*Pflicht wenn keine `user_id` angegeben.

**Beispiel-Inhalt:**

```
user_id;email;ad_name;oe_kuerzel;nachname;vorname;kuerzel;rolle
mmu;max.mustermann@bank.de;DOMAIN\mmu;KRE;Mustermann;Max;MMU;Fachverantwortlicher
abe;anna.beispiel@bank.de;DOMAIN\abe;VWL;Beispiel;Anna;ABE;IDV-Koordinator
```

Eine **CSV-Vorlage** steht über den Button „CSV-Vorlage" zum Download bereit.

### Import-Logik

- Wird eine Person mit gleicher `user_id` oder gleichem `kuerzel` gefunden → **Update** (fehlende Felder werden ergänzt, vorhandene bleiben erhalten)
- Andernfalls → **Neuanlage**
- Passwörter werden beim Import **nicht** gesetzt (müssen manuell über „Person bearbeiten" vergeben werden)
- OE-Kürzel, die nicht in der OE-Tabelle vorhanden sind, werden ignoriert

---

### Dashboard

Einstiegsseite mit Kennzahlen auf einen Blick:
- Anzahl aktiver IDVs nach Status (Entwurf / In Prüfung / Genehmigt)
- Kritische IDVs (GDA 4, steuerungsrelevant, DORA-kritisch)
- Überfällige und bald fällige Prüfungen
- Offene Maßnahmen mit Eskalationsstatus

---

### IDV-Grundgesamtheit

Liste aller registrierten IDVs. Filterbar nach Status, GDA-Wert, Typ und
Compliance-Profil (DORA-kritisch, steuerungsrelevant, unvollständig).

**Neue IDV erfassen:** Über *„Neue IDV"* oder direkt aus einem Scannerfund heraus
(siehe Scanner-Funde). Das Formular führt durch fünf Abschnitte:

1. **Stammdaten** — Bezeichnung, Typ, Version, Kurzbeschreibung
2. **Wesentlichkeitsbeurteilung** — Steuerungsrelevant, Rechnungslegungsrelevant, DORA-kritisch/wichtig (mit Grad der Abhängigkeit)
3. **Risikobewertung** — Risikoklasse, Verfügbarkeit, Integrität, Vertraulichkeit
4. **Technik & Betrieb** — Plattform, Nutzungsfrequenz, Zugriffsschutz, Makros
5. **Verantwortliche** — Org-Einheit, Fachverantwortlicher, Entwickler, Koordinator

---

### Prüfungen

**Wozu:** MaRisk AT 7.2 schreibt vor, dass IDVs in regelmäßigen Abständen geprüft
werden. Das Prüfintervall (z.B. 6 oder 12 Monate) wird pro IDV festgelegt.
Die Prüfungen-Ansicht zeigt alle Prüfungen IDV-übergreifend — nützlich um
z.B. alle überfälligen Prüfungen auf einen Blick zu sehen.

**Wie eine Prüfung angelegt wird:**

```
IDV-Grundgesamtheit → IDV auswählen → Detailseite → „Neue Prüfung"
```

Eine Prüfung dokumentiert:

| Feld | Beschreibung |
|---|---|
| Prüfungsart | Regelprüfung / Anlassprüfung / Erstprüfung |
| Prüfungsdatum | Datum der Durchführung |
| Prüfer | Person aus dem Personenkatalog |
| Ergebnis | Ohne Befund / Mit Befund / Kritischer Befund / Nicht bestanden |
| Befundbeschreibung | Freitext zu festgestellten Mängeln |
| Nächste Prüfung | Datum → wird automatisch ins IDV-Register übernommen |
| Kommentar | Interne Anmerkungen |

Nach dem Speichern wird `naechste_pruefung` im IDV-Register aktualisiert und der
Prüfstatus im Dashboard und in den Übersichtslisten neu berechnet.

**Filter in der Listenansicht:**
- *Standard:* alle Prüfungen der letzten 100 Einträge
- *Überfällig:* IDVs, deren `naechste_pruefung` in der Vergangenheit liegt

---

### Maßnahmen

**Wozu:** Wenn eine Prüfung Mängel ergibt oder Risiken proaktiv erkannt werden,
entstehen daraus Maßnahmen. Die Maßnahmen-Ansicht zeigt alle offenen Maßnahmen
IDV-übergreifend — nützlich für den IDV-Koordinator als Gesamtüberblick.

**Wie eine Maßnahme angelegt wird:**

```
IDV-Grundgesamtheit → IDV auswählen → Detailseite → „Neue Maßnahme"
```

Eine Maßnahme enthält:

| Feld | Beschreibung |
|---|---|
| Titel | Kurze Beschreibung der Maßnahme |
| Beschreibung | Ausführliche Erläuterung |
| Maßnahmentyp | z.B. Dokumentation / Zugriffsschutz / Ablösung |
| Priorität | Kritisch / Hoch / Mittel / Niedrig |
| Verantwortlicher | Person aus dem Personenkatalog |
| Fällig am | Zieldatum für die Erledigung |
| Status | Offen → In Bearbeitung → Erledigt |

**Status-Workflow:**

```
Offen → In Bearbeitung → Erledigt
```

Über den Button *„Als erledigt markieren"* auf der IDV-Detailseite wird
eine Maßnahme mit Erledigungsdatum abgeschlossen.

**Filter in der Listenansicht:**
- *Standard:* alle offenen und in Bearbeitung befindlichen Maßnahmen
- *Überfällig:* Maßnahmen, deren Fälligkeitsdatum überschritten ist

---

### Scanner-Funde

Zeigt alle Dateien, die der IDV-Scanner auf Netzlaufwerken gefunden hat.
Über den Button *„Als IDV registrieren"* wird das IDV-Formular mit vorausgefüllten
Daten (Dateiname, IDV-Typ aus Erweiterung, Makro-Flag) geöffnet.

**Scan starten**

In allen Scanner-Views (Funde, Eingang, Scan-Läufe, Bewertete, Zusammenfassen)
ist für **Administratoren** und **Koordinatoren** oben rechts eine
Schaltfläche *„Scan starten"* sichtbar. Ein Klick startet den Scanner im
Hintergrund; der Button zeigt einen Spinner und eine Fertigmeldung,
sobald der Scan abgeschlossen ist. Lesende Rollen (Revision, IT-Sicherheit)
sehen die Schaltfläche nicht.

> Voraussetzung: Scan-Pfade müssen unter
> *Administration → Scanner-Einstellungen* konfiguriert sein.
> Fehlen Pfade, ist der Button deaktiviert.

**Filter:**
- Alle aktiven Dateien
- Noch nicht registriert (kein IDV-Eintrag verknüpft)
- Mit Makros (VBA)
- Bereits registriert
- Zur Registrierung vorgemerkt
- **Archiv** — Dateien, die beim letzten Scan nicht mehr gefunden wurden
  (verschoben, umbenannt oder gelöscht). Die Verknüpfung zum IDV-Register
  bleibt erhalten. Taucht eine Datei wieder auf, wird sie automatisch reaktiviert.

Voraussetzung: Scanner und Webapp müssen dieselbe Datenbank nutzen.
Dazu in `scanner/config.json` setzen:
```json
{ "db_path": "../instance/idvault.db" }
```

→ Weitere Details: [`scanner/README.md`](scanner/README.md)

### Scanner-Eingang und Vormerkung

Der **Eingang** (*Scanner → Eingang*) zeigt ausschließlich unbearbeitete
Dateien (`bearbeitungsstatus = 'Neu'`). Von hier aus werden Dateien triagiert:

| Aktion | Wirkung |
|---|---|
| **Zur Registrierung vormerken** | Setzt `bearbeitungsstatus` auf `Zur Registrierung`. Die Datei verschwindet aus dem Eingang und erscheint im separaten Zähler „Zur Registrierung". |
| **Ignorieren** | Setzt `bearbeitungsstatus` auf `Ignoriert`. Die Datei wird bei künftigen Scans nicht erneut als „Neu" angezeigt. |
| **Direkt registrieren** | Öffnet das IDV-Formular; nach dem Speichern wird `bearbeitungsstatus` automatisch auf `Registriert` gesetzt. |

**Bearbeitungsstatus einer Datei (Lebenszyklus):**

```
Neu → Zur Registrierung → Registriert
 │                              ↑
 ├── direkt registrieren ───────┘
 └── Ignoriert
```

- **Neu** — Vom Scanner entdeckt, noch nicht gesichtet.
- **Zur Registrierung** — Vorgemerkt: Die Datei soll als IDV erfasst werden,
  die eigentliche Registrierung steht noch aus. Dient als Arbeitsliste.
- **Registriert** — Einem IDV-Register-Eintrag zugeordnet.
- **Ignoriert** — Bewusst ausgeschlossen (z. B. keine IDV-relevante Datei).

Die Vormerkung ist eine reine Organisationshilfe (Triage). Es werden dabei
keine fachlichen Daten erzeugt — lediglich der Bearbeitungsstatus wechselt.
Die Bulk-Aktion erlaubt es, mehrere Dateien gleichzeitig vorzumerken.

---

### Administration

Verwaltung der Stammdaten:
- **Personen** — Fachverantwortliche, Entwickler, Koordinatoren, Prüfer (inkl. User-ID, E-Mail, AD-Name, Passwort)
- **Org-Einheiten** — Abteilungen und Bereiche (anlegen, bearbeiten, deaktivieren)
- **Geschäftsprozesse** — Prozesskatalog (Basis für Kritikalitätsbewertung)
- **Plattformen** — Technologie-Katalog (Excel, Python, Power BI …)
- **E-Mail-Einstellungen** — SMTP-Konfiguration für automatische Benachrichtigungen
- **LDAP / Active Directory** — LDAPS-Verbindung und Gruppen-Rollen-Mapping
- **Software-Update** — Anwendungs-Updates einspielen ohne EXE-Austausch

→ Detailbeschreibung: [Benutzer- und Berechtigungskonzept](#benutzer--und-berechtigungskonzept), [LDAP / Active Directory](#ldap--active-directory), [E-Mail-Benachrichtigungen](#e-mail-benachrichtigungen), [Administration](#administration-1), [Mitarbeiterdaten importieren](#mitarbeiterdaten-importieren-csv)

---

## Software-Update

idvault unterstützt einen **Update-Mechanismus ohne EXE-Austausch**, der speziell
für Umgebungen mit AppLocker oder eingeschränkten Berechtigungen entwickelt wurde.

### Funktionsprinzip

Die `idvault.exe` wird **niemals verändert** — AppLocker-Hash-Regeln bleiben dauerhaft gültig.
Aktualisierungen werden stattdessen als Python-Dateien und Templates in einem
`updates/`-Ordner neben der EXE abgelegt. Beim nächsten Start lädt die Anwendung
diese Dateien bevorzugt vor den gebündelten.

```
idvault.exe          ← unveränderlich (AppLocker-Ausnahme bleibt gültig)
instance/
  idvault.db
updates/             ← wird beim Update-Import angelegt
  version.json       ← aktive Versionsinformation
  webapp/
    routes/
      admin.py       ← überschreibt die gebündelte Version
  templates/
    admin/
      update.html    ← überschreibt das gebündelte Template
```

### Update einspielen

Voraussetzung: Zugang zur Web-Oberfläche mit der Rolle **IDV-Administrator**.

```
System → Software-Update → ZIP-Datei auswählen → „ZIP hochladen & einspielen"
```

Anschließend:

```
„App neu starten" klicken
```

Der Browser leitet nach einigen Sekunden automatisch weiter. Das Update ist damit aktiv.

### GitHub-Repository-ZIP direkt verwenden

Der einfachste Weg ist der direkte Download-Link des GitHub-Repositories:

```
https://github.com/hvorragend/idvault/archive/refs/heads/main.zip
```

Dieses ZIP kann ohne Anpassung hochgeladen werden. Die Anwendung:
- erkennt automatisch das `idvault-main/`-Präfix und entfernt es
- überspringt nicht-relevante Dateien (`.md`, `.txt`, `.spec`, `.gitignore` usw.) stillschweigend
- mappt `webapp/templates/` auf `templates/` wie vom Sidecar-Lader erwartet

### Manuelles ZIP-Paket-Format

Für selektive Updates (nur einzelne Dateien) kann auch ein eigenes ZIP erstellt werden:

```
update-v0.2.0.zip
├── version.json                 ← Versionsmetadaten
├── webapp/
│   └── routes/
│       └── admin.py             ← überschreibt webapp.routes.admin
└── templates/
    └── admin/
        └── update.html          ← überschreibt Template
```

> Templates liegen im manuellen ZIP unter `templates/` (nicht `webapp/templates/`).
> Im GitHub-ZIP sind sie unter `webapp/templates/` — das wird automatisch umgemappt.

**`version.json`-Format:**

```json
{
  "version": "0.2.0",
  "released": "2026-04-14",
  "changelog": [
    {
      "version": "0.2.0",
      "date": "2026-04-14",
      "changes": [
        "Bugfix: Datumsfilter bei leeren Feldern",
        "Neue Exportoption im Bericht"
      ]
    }
  ]
}
```

### Erlaubte Dateitypen im ZIP

| Typ | Erlaubt |
|---|:---:|
| `.py` | ✓ |
| `.html` | ✓ |
| `.json` | ✓ |
| `.sql` | ✓ |
| `.css` / `.js` | ✓ |
| `.exe`, `.dll`, `.bat`, `.sh` | — |

Dateien außerhalb dieser Liste werden abgelehnt, bevor etwas extrahiert wird.

### Rollback

```
System → Software-Update → „Rollback (Update entfernen)"
```

Der `updates/`-Ordner wird gelöscht. Nach erneutem Neustart läuft wieder die
gebündelte Version der EXE.

### Versionsinformation

Die Update-Seite zeigt immer beide Versionen:

| Bezeichnung | Bedeutung |
|---|---|
| **Gebündelte Version** | Version, mit der die EXE gebaut wurde (unveränderlich) |
| **Aktive Version** | Version des eingespielten Updates (aus `updates/version.json`) |

Ist kein Override aktiv, stimmen beide Werte überein.

### Sicherheitshinweise

- Nur Benutzer mit der Rolle **IDV-Administrator** können Updates einspielen.
- Jeder ZIP-Eintrag wird vor der Extraktion auf Dateityp und Path-Traversal geprüft.
- Die maximale Upload-Größe beträgt **32 MB** (Werkzeug-Limit).
- Der `updates/`-Ordner liegt neben der EXE — derselbe Benutzer, der die App
  startet, muss Schreibrechte in diesem Verzeichnis haben.

---

## Workflow und Statusfelder

idvault verwendet zwei parallele Statusfelder auf dem IDV-Register sowie
separate Status für Scanner-Dateien, Prüfungen und Maßnahmen. Die folgende
Dokumentation beschreibt den Gesamtablauf und was bei jedem Statuswechsel passiert.

### Gesamtablauf (Übersicht)

```
1. Scanner läuft (wöchentlich per Scheduled Task)
        ↓
2. Eingang sichten → „Zur Registrierung vormerken" oder ignorieren
        ↓
3. Vorgemerkte Dateien → „Als IDV registrieren"
        ↓
4. IDV-Formular ausfüllen (Wesentlichkeit, Klassifizierung, Verantwortliche)
        ↓
5. Status: Entwurf → In Prüfung → Genehmigt
        ↓
6. Bei wesentlicher IDV: Test- und Freigabeverfahren (4 Schritte)
        ↓
7. Regelprüfung fällig (nach pruefintervall_monate)
        ↓
8. Prüfung dokumentieren → Ergebnis + nächstes Prüfdatum
        ↓
9. Bei Befund: Maßnahme anlegen → verfolgen bis Erledigt
        ↓
10. Dashboard zeigt Gesamtstatus jederzeit aktuell
```

### Statusfeld 1: Scanner-Bearbeitungsstatus (`idv_files.bearbeitungsstatus`)

Betrifft vom Scanner gefundene **Dateien**, nicht IDV-Register-Einträge.

```
Neu → Zur Registrierung → Registriert
 │                              ↑
 ├── direkt registrieren ───────┘
 └── Ignoriert
```

| Status | Bedeutung | Auslöser |
|---|---|---|
| **Neu** | Vom Scanner entdeckt, noch nicht gesichtet | Automatisch beim Scan |
| **Zur Registrierung** | Vorgemerkt für spätere IDV-Erfassung | Manuell: Button „Zur Registrierung vormerken" |
| **Registriert** | Einem IDV-Register-Eintrag zugeordnet | Automatisch beim Anlegen/Verknüpfen einer IDV |
| **Ignoriert** | Bewusst ausgeschlossen (keine IDV) | Manuell: Button „Ignorieren" |

Wird eine Dateiverknüpfung von einer IDV entfernt, wechselt der Status
automatisch zurück auf `Neu`.

### Statusfeld 2: IDV-Workflow-Status (`idv_register.status`)

Zentrales Genehmigungsfeld. Bestimmt, ob eine IDV produktiv zugelassen ist.

```
Entwurf → In Prüfung → Genehmigt
               │              ↓
               ▼          Abgekündigt → Archiviert
           Abgelehnt

Genehmigt mit Auflagen → Genehmigt
```

| Status | Bedeutung | Was passiert beim Wechsel |
|---|---|---|
| **Entwurf** | Ersterfassung, noch nicht zur Prüfung eingereicht | Standardstatus bei Neuanlage. Keine Benachrichtigung. |
| **In Prüfung** | Liegt beim IDV-Koordinator zur Bewertung | History-Eintrag wird geschrieben. |
| **Genehmigt** | Freigegeben für den produktiven Einsatz | History-Eintrag. Bei Datei-Verknüpfung wird der Datei-Hash protokolliert. |
| **Genehmigt mit Auflagen** | Bedingt freigegeben; Auflagen müssen erfüllt werden | History-Eintrag wie „Genehmigt". |
| **Abgelehnt** | Nicht als IDV eingestuft oder nicht genehmigungsfähig | History-Eintrag. |
| **Abgekündigt** | IDV wird abgelöst oder abgeschaltet | History-Eintrag. IDV wird aus Prüfstatus-Berechnung ausgeschlossen. |
| **Archiviert** | Historisch, nicht mehr aktiv | History-Eintrag. IDV wird aus allen aktiven Listen und Statistiken ausgeblendet. |

Der Statuswechsel kann einzeln oder per Bulk-Aktion (mehrere IDVs gleichzeitig)
erfolgen. Jeder Wechsel erzeugt einen Eintrag in `idv_history`.

### Statusfeld 3: Teststatus (`idv_register.teststatus`)

Bildet den Fortschritt im Test- und Freigabeverfahren ab. Wird automatisch
vom Freigabeverfahren gesteuert und kann auch manuell gesetzt werden.

```
Wertung ausstehend → In Bearbeitung → Freigabe ausstehend → Freigegeben
                           ↑                    │
                           └── bei Ablehnung ────┘
                           └── bei Abbruch ──────┘
```

| Status | Bedeutung | Auslöser |
|---|---|---|
| **Wertung ausstehend** | Neu angelegt, noch keine inhaltliche Bearbeitung | Standardwert bei Neuanlage und bei neuer Version |
| **In Bearbeitung** | Wird aktiv bearbeitet / nachgebessert | Manuell oder automatisch nach Ablehnung/Abbruch im Freigabeverfahren |
| **Freigabe ausstehend** | Test-/Freigabeverfahren läuft | Automatisch beim Start von Phase 1 des Freigabeverfahrens |
| **Freigegeben** | Alle 4 Freigabe-Schritte bestanden | Automatisch nach Abschluss von Phase 2. Setzt gleichzeitig `dokumentation_vorhanden = 1`. |

Jeder Wechsel erzeugt einen History-Eintrag.

### Test- und Freigabeverfahren (`idv_freigaben`)

Nur für **wesentliche IDVs mit wesentlicher Änderung**. Vier Schritte in zwei
Phasen, die jeweils parallel ablaufen:

```
Phase 1 (parallel):          Phase 2 (parallel):
┌─────────────────┐          ┌──────────────────┐
│ Fachlicher Test │          │ Fachliche Abnahme│
└────────┬────────┘          └────────┬─────────┘
         │  beide bestanden?          │  beide bestanden?
┌────────┴────────┐    →     ┌────────┴─────────┐    →  IDV freigegeben
│Technischer Test │          │Techn. Abnahme    │
└─────────────────┘          └──────────────────┘
```

**Phase 2 kann erst gestartet werden, wenn beide Phase-1-Schritte bestanden sind.**

| Schritt-Status | Bedeutung |
|---|---|
| **Ausstehend** | Schritt angelegt, wartet auf Durchführung |
| **Bestanden** | Erfolgreich abgeschlossen |
| **Nicht bestanden** | Abgelehnt mit Befunden → Teststatus wechselt zurück auf `In Bearbeitung` |
| **Abgebrochen** | Durch Administrator abgebrochen → Teststatus wechselt zurück auf `In Bearbeitung` |

**Funktionstrennung:** Der als Entwickler eingetragene Mitarbeiter darf keine
Freigabe-Schritte abschließen oder ablehnen (Ausnahme: Administratoren).

**Seiteneffekte:**
- Start Phase 1 → Teststatus wechselt auf `Freigabe ausstehend`, E-Mail an zugewiesene Prüfer und Koordinatoren
- Start Phase 2 → E-Mail an zugewiesene Prüfer und Koordinatoren
- Alle 4 Schritte bestanden → Teststatus `Freigegeben`, `dokumentation_vorhanden` wird gesetzt, E-Mail an Koordinatoren/Admins/Entwickler
- Schritt nicht bestanden → Teststatus zurück auf `In Bearbeitung`
- Abbruch durch Admin → alle offenen Schritte auf `Abgebrochen`, Teststatus zurück auf `In Bearbeitung`

### Genehmigungen (`genehmigungen`)

Separater 4-Augen-Workflow (unabhängig vom Test-/Freigabeverfahren):

| Feld | Werte |
|---|---|
| **Genehmigungsart** | Erstfreigabe, Wiederfreigabe, Wesentliche Änderung, Ablösung |
| **Stufe 1** (IDV-Koordinator) | Ausstehend → Genehmigt / Abgelehnt |
| **Stufe 2** (IT-Sicherheit/Revision) | Ausstehend → Genehmigt / Abgelehnt / Nicht erforderlich |

Stufe 2 ist nur erforderlich bei GDA = 4 oder DORA-kritisch/wichtig.

### Prüfungen (`pruefungen`)

Regelmäßige Überprüfungen gemäß MaRisk AT 7.2.

| Feld | Werte |
|---|---|
| **Prüfungsart** | Erstprüfung, Regelprüfung, Anlassprüfung, Revisionsprüfung |
| **Ergebnis** | Ohne Befund, Mit Befund, Kritischer Befund, Nicht bestanden |

**Berechneter Prüfstatus** (in der View `v_idv_uebersicht`, kein eigenes Feld):

| Prüfstatus | Bedingung |
|---|---|
| **ÜBERFÄLLIG** | `naechste_pruefung` liegt in der Vergangenheit |
| **BALD FÄLLIG** | `naechste_pruefung` liegt innerhalb der nächsten 30 Tage |
| **OK** | `naechste_pruefung` liegt mehr als 30 Tage in der Zukunft |

Nach dem Speichern einer Prüfung wird `naechste_pruefung` im IDV-Register
aktualisiert. Bei überfälligen Prüfungen wird eine E-Mail an den
Fachverantwortlichen gesendet.

### Maßnahmen (`massnahmen`)

Entstehen aus Prüfungsbefunden oder proaktiver Risikobewertung.

```
Offen → In Bearbeitung → Erledigt
  │
  └── Zurückgestellt
```

| Status | Bedeutung |
|---|---|
| **Offen** | Maßnahme angelegt, noch nicht begonnen |
| **In Bearbeitung** | Wird aktiv umgesetzt |
| **Erledigt** | Abgeschlossen mit Erledigungsdatum |
| **Zurückgestellt** | Bewusst pausiert |

Bei überfälligen Maßnahmen (Fälligkeitsdatum überschritten, Status nicht
`Erledigt`) wird eine E-Mail an den Verantwortlichen gesendet.

### E-Mail-Benachrichtigungen im Workflow

| Ereignis | Empfänger |
|---|---|
| Neue Datei im Scanner erkannt | Koordinatoren und Administratoren |
| Prüfung überfällig | Fachverantwortlicher der IDV |
| Maßnahme überfällig | Verantwortlicher der Maßnahme |
| Freigabeverfahren gestartet (Phase 1/2) | Zugewiesene Prüfer + Koordinatoren |
| Freigabeverfahren vollständig bestanden | Koordinatoren, Administratoren, Entwickler |
| Datei-Bewertung | Verantwortlicher |

### Versionierung

Über *„Neue Version erstellen"* auf der IDV-Detailseite wird eine Kopie
der IDV mit neuer Versionsnummer angelegt. Dabei:

- `teststatus` wird auf `Wertung ausstehend` zurückgesetzt
- `letzte_aenderungsart` wird auf `wesentlich` oder `unwesentlich` gesetzt
- Die alte IDV wird als `vorgaenger_idv_id` verknüpft
- Bei `letzte_aenderungsart = 'unwesentlich'` entfällt das Freigabeverfahren

---

## Technisches Datenmodell

Das Schema liegt in `schema.sql` und wird beim Start automatisch initialisiert (`db.py`).

### Drei Schichten

| Schicht | Tabellen |
|---|---|
| **Stammdaten** | `org_units`, `persons`, `geschaeftsprozesse`, `plattformen`, `risikoklassen` |
| **Kernregister** | `idv_register` — eine Zeile pro IDV, ~70 Attribute |
| **Workflow & Audit** | `idv_history`, `pruefungen`, `massnahmen`, `genehmigungen` |
| **Authentifizierung** | `ldap_config` (LDAP-Server), `ldap_group_role_mapping` (Gruppen → Rollen) |

### Schema-Überblick

```
org_units ─────────────────────────────────────────────┐
persons ────────────────────────────────────────────┐  │
geschaeftsprozesse ──────────────────────────────┐  │  │
plattformen ─────────────────────────────────┐   │  │  │
risikoklassen ───────────────────────────┐   │   │  │  │
                                         │   │   │  │  │
idv_files ──────────────────────────────► idv_register ◄┘
                                              │
              ┌───────────────────────────────┼──────────────────────┐
              │                              │                      │
         idv_history                     pruefungen           massnahmen
```

### GDA-Wert (Grad der Abhängigkeit)

Abgeleitet aus der BAIT-Orientierungshilfe zur IDV-Risikoklassifizierung:

| Wert | Bezeichnung | Bedeutung |
|---|---|---|
| 1 | Unterstützend | Prozess läuft auch ohne IDV, mit erhöhtem manuellem Aufwand |
| 2 | Relevant | IDV unterstützt den Prozess; alternative Durchführung möglich |
| 3 | Wesentlich | Kernprozessunterstützung; keine vollständige manuelle Alternative |
| 4 | Vollständig abhängig | Prozess kann ohne diese IDV nicht ausgeführt werden |

GDA = 4 löst die zweite Genehmigungsstufe und eine verpflichtende DORA-Bewertung aus.

### Designentscheidungen

- **SQLite im WAL-Modus** — keine eigene Infrastruktur; PostgreSQL-Migration möglich bei >50 gleichzeitigen Schreibern
- **ISO 8601 für alle Datumsfelder** — timezone-sicher, Python- und OS-unabhängig
- **JSON-Felder für strukturierte Listen** — `tags`, `schnittstellen`, `weitere_dateien` und History-Deltas
- **Trennung Scanner / Register** — `idv_files` hält Rohdaten, `idv_register` die kuratierte Klassifizierung; Scanner kann unbeaufsichtigt laufen ohne das Register zu berühren

---

## Log-Dateien

Alle Log-Dateien liegen im Verzeichnis `instance/` neben der Datenbank.

| Datei | Inhalt | Rotation |
|---|---|---|
| `idvault.log` | Flask-App-Meldungen (WARNING und höher) | 1 MB pro Segment, 7 Backups (`idvault.log.1` … `.7`) |
| `idvault.log.1` … `.7` | Rotierte Segmente (automatisch verwaltet) | — |
| `idvault_crash.log` | Python-Tracebacks / PyInstaller-Startfehler (nur EXE-Betrieb) | Umbenennung zu `.1` bei > 2 MB beim nächsten Start |
| `idvault_crash.log.1` | Backup des vorherigen Crash-Logs | — |

> Ältere Dateien (`.1` … `.7`) werden von Python automatisch beim Überschreiten
> des Grenzwerts angelegt und verwaltet. Es ist kein Cron-Job oder Windows-Task
> erforderlich.

---

## Komponenten

| Verzeichnis / Datei | Inhalt |
|---|---|
| `webapp/` | Flask-Webanwendung (Blueprints, Templates, DB-Schicht) |
| `scanner/` | IDV-Scanner für Netzlaufwerke |
| `schema.sql` | SQLite-Schema (IDV-Register, Workflow-Tabellen) |
| `db.py` | Datenbankschicht (gemeinsam von Scanner und Webapp genutzt) |
| `run.py` | Startskript für die Webapp |
