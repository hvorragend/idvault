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
| AD-Name | Active-Directory-Kontoname (z.B. `DOMAIN\mmu`), für spätere AD-Integration |
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
2. **Klassifizierung** — GDA-Wert (1–4), Steuerungsrelevanz, RL-Relevanz, DORA
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
- **Archiv** — Dateien, die beim letzten Scan nicht mehr gefunden wurden
  (verschoben, umbenannt oder gelöscht). Die Verknüpfung zum IDV-Register
  bleibt erhalten. Taucht eine Datei wieder auf, wird sie automatisch reaktiviert.

Voraussetzung: Scanner und Webapp müssen dieselbe Datenbank nutzen.
Dazu in `scanner/config.json` setzen:
```json
{ "db_path": "../instance/idvault.db" }
```

→ Weitere Details: [`scanner/README.md`](scanner/README.md)

---

### Administration

Verwaltung der Stammdaten:
- **Personen** — Fachverantwortliche, Entwickler, Koordinatoren, Prüfer (inkl. User-ID, E-Mail, AD-Name, Passwort)
- **Org-Einheiten** — Abteilungen und Bereiche (anlegen, bearbeiten, deaktivieren)
- **Geschäftsprozesse** — Prozesskatalog (Basis für Kritikalitätsbewertung)
- **Plattformen** — Technologie-Katalog (Excel, Python, Power BI …)
- **E-Mail-Einstellungen** — SMTP-Konfiguration für automatische Benachrichtigungen

→ Detailbeschreibung: [Benutzer- und Berechtigungskonzept](#benutzer--und-berechtigungskonzept), [E-Mail-Benachrichtigungen](#e-mail-benachrichtigungen), [Administration](#administration-1), [Mitarbeiterdaten importieren](#mitarbeiterdaten-importieren-csv)

---

## Typischer Workflow

```
1. Scanner läuft (wöchentlich per Scheduled Task)
        ↓
2. Scanner-Funde → „Als IDV registrieren"
        ↓
3. IDV-Formular ausfüllen (GDA, Klassifizierung, Verantwortliche)
        ↓
4. Status: Entwurf → In Prüfung → Genehmigt
        ↓
5. Regelprüfung fällig (nach pruefintervall_monate)
        ↓
6. Prüfung dokumentieren → Ergebnis + nächstes Prüfdatum
        ↓
7. Bei Befund: Maßnahme anlegen → verfolgen bis Erledigt
        ↓
8. Dashboard zeigt Gesamtstatus jederzeit aktuell
```

---

## Technisches Datenmodell

Das Schema liegt in `schema.sql` und wird beim Start automatisch initialisiert (`db.py`).

### Drei Schichten

| Schicht | Tabellen |
|---|---|
| **Stammdaten** | `org_units`, `persons`, `geschaeftsprozesse`, `plattformen`, `risikoklassen` |
| **Kernregister** | `idv_register` — eine Zeile pro IDV, ~70 Attribute |
| **Workflow & Audit** | `idv_history`, `pruefungen`, `massnahmen`, `genehmigungen` |

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

### Workflow-Status

```
Entwurf
  │
  ▼
In Prüfung ──► Abgelehnt
  │
  ▼
Genehmigt ◄── Genehmigt mit Auflagen
  │
  ▼
Abgekündigt
  │
  ▼
Archiviert
```

### Designentscheidungen

- **SQLite im WAL-Modus** — keine eigene Infrastruktur; PostgreSQL-Migration möglich bei >50 gleichzeitigen Schreibern
- **ISO 8601 für alle Datumsfelder** — timezone-sicher, Python- und OS-unabhängig
- **JSON-Felder für strukturierte Listen** — `tags`, `schnittstellen`, `weitere_dateien` und History-Deltas
- **Trennung Scanner / Register** — `idv_files` hält Rohdaten, `idv_register` die kuratierte Klassifizierung; Scanner kann unbeaufsichtigt laufen ohne das Register zu berühren

---

## Komponenten

| Verzeichnis / Datei | Inhalt |
|---|---|
| `webapp/` | Flask-Webanwendung (Blueprints, Templates, DB-Schicht) |
| `scanner/` | IDV-Scanner für Netzlaufwerke |
| `schema.sql` | SQLite-Schema (IDV-Register, Workflow-Tabellen) |
| `db.py` | Datenbankschicht (gemeinsam von Scanner und Webapp genutzt) |
| `run.py` | Startskript für die Webapp |
