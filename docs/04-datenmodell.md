# 04 – Datenmodell

---

## 1 Überblick

Das Datenmodell ist in **vier logische Schichten** gegliedert:

| Schicht | Tabellen |
|---|---|
| **Stammdaten** | `org_units`, `persons`, `geschaeftsprozesse`, `plattformen`, `risikoklassen`, `klassifizierungen`, `wesentlichkeitskriterien` |
| **Scanner** | `scan_runs`, `idv_files`, `idv_file_history`, `idv_file_links` |
| **Kernregister** | `idv_register` (~70 Attribute), `idv_wesentlichkeit` |
| **Workflow & Audit** | `idv_history`, `pruefungen`, `massnahmen`, `genehmigungen`, `idv_freigaben`, `fachliche_testfaelle`, `technischer_test` |
| **Authentifizierung** | `ldap_config`, `ldap_group_role_mapping` |
| **Konfiguration** | `app_settings` |

Die vollständige DDL-Definition liegt in `schema.sql` (~900 Zeilen).

## 2 Entity-Relationship-Diagramm (vereinfacht)

```
┌──────────────┐       ┌────────────────────────────┐       ┌──────────────┐
│  org_units   │◄──────│       idv_register         │──────►│  persons     │
└──────────────┘       │  (Kernregister)            │       └──────────────┘
                       │                            │
┌──────────────────────│ fk: gp_id, plattform_id,   │──────►┌──────────────┐
│ geschaeftsprozesse   │     risikoklasse_id,       │       │ risikoklassen│
└──────────────────────│     org_unit_id,           │       └──────────────┘
                       │     fachverantwortlicher_id│
┌─────────────┐◄───────│     idv_entwickler_id,     │
│ plattformen │        │     idv_koordinator_id,    │
└─────────────┘        │     stellvertreter_id,     │
                       │     file_id (Primärdatei)  │
                       └──────────┬─────────────────┘
                                  │
            ┌─────────────────────┼──────────────────┬─────────────────────┐
            ▼                     ▼                  ▼                     ▼
    ┌───────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────────┐
    │ idv_history   │    │  pruefungen  │    │ massnahmen   │    │ genehmigungen │
    │ (append-only) │    │              │    │              │    │               │
    └───────────────┘    └──────────────┘    └──────────────┘    └───────────────┘
                                  │
                         ┌────────┴──────────┐
                         ▼                   ▼
                 ┌──────────────┐    ┌────────────────┐
                 │ idv_freigaben│    │ idv_wesentlich-│
                 │ (4-Phasen)   │    │ keit           │
                 └──────────────┘    └────────────────┘

┌─────────────┐      ┌──────────────────┐       ┌────────────────────┐
│ scan_runs   │─────►│   idv_files      │──────►│  idv_file_history  │
└─────────────┘      │  (entdeckte      │       │  (Änderungsspuren) │
                     │   Dateien)       │       └────────────────────┘
                     └──────────────────┘
                              ▲
                              │ m:n
                     ┌──────────────────┐
                     │ idv_file_links   │
                     │ (IDV ↔ Datei)    │
                     └──────────────────┘

┌───────────────┐       ┌──────────────────────────┐
│ ldap_config   │       │ ldap_group_role_mapping  │
└───────────────┘       └──────────────────────────┘
```

## 3 Stammdaten-Tabellen

### 3.1 `org_units` – Organisationseinheiten

| Spalte | Typ | Constraint | Zweck |
|---|---|---|---|
| `id` | INTEGER | PK | Künstlicher Schlüssel |
| `oe_kuerzel` | TEXT | UNIQUE, NOT NULL | Fachkürzel (z. B. `KRE`) |
| `bezeichnung` | TEXT | NOT NULL | Klarname |
| `parent_id` | INTEGER | FK → `org_units(id)` | Hierarchie (optional) |
| `aktiv` | INTEGER | DEFAULT 1 | Soft-Delete |
| `erstellt_am` | TEXT | — | ISO 8601 |

### 3.2 `persons` – Mitarbeiter

| Spalte | Typ | Constraint | Zweck |
|---|---|---|---|
| `id` | INTEGER | PK | |
| `kuerzel` | TEXT | UNIQUE | Initialen-Kürzel |
| `nachname` / `vorname` | TEXT | — | Klarname |
| `email` | TEXT | — | SMTP-Adresse |
| `telefon` | TEXT | — | Telefonnummer |
| `user_id` | TEXT | UNIQUE | Login-Name (idvault / AD) |
| `ad_name` | TEXT | — | AD-Account-Name |
| `password_hash` | TEXT | — | SHA-256-Hash (aktuell; soll migriert werden auf Argon2id) |
| `rolle` | TEXT | — | Eine der 5 definierten Rollen |
| `org_unit_id` | INTEGER | FK → `org_units(id)` | Zugehörige OE |
| `aktiv` | INTEGER | DEFAULT 1 | Inaktive Nutzer können sich nicht einloggen |

> **Hinweis Sicherheit:** `password_hash` wird mittelfristig auf Argon2id
> migriert (vgl. [09 – Schwachstellenanalyse](09-schwachstellenanalyse.md)).

### 3.3 `geschaeftsprozesse` – Prozesskatalog

| Spalte | Typ | Zweck |
|---|---|---|
| `id` | INTEGER PK | |
| `gp_nummer` | TEXT UNIQUE | Prozess-Id laut Prozesshandbuch |
| `bezeichnung` | TEXT | |
| `ist_kritisch` | INTEGER | Grundlage für DORA-Ableitung |
| `ist_wesentlich` | INTEGER | Grundlage für MaRisk-Wesentlichkeit |

### 3.4 `plattformen` – Technologiekatalog

| Spalte | Typ | Zweck |
|---|---|---|
| `id` | INTEGER PK | |
| `bezeichnung` | TEXT UNIQUE | z. B. "Microsoft Excel", "Python 3.x", "Power BI" |

### 3.5 `risikoklassen`

| Spalte | Typ | Zweck |
|---|---|---|
| `id` | INTEGER PK | |
| `bezeichnung` | TEXT | "Kritisch", "Hoch", "Mittel", "Gering" |
| `farbe_hex` | TEXT | Darstellung im UI |

### 3.6 `klassifizierungen` – Konfigurierbare Enumerationen

| Spalte | Typ | Zweck |
|---|---|---|
| `bereich` | TEXT | Fachbereich (z. B. "idv_typ", "zugriffsschutz") |
| `wert` | TEXT | Anzeige-Wert |
| UNIQUE(`bereich`, `wert`) | | |

Dient dazu, Auswahlfelder anwendungsseitig zu pflegen, ohne Code-Änderung.

### 3.7 `wesentlichkeitskriterien`

Konfigurierbarer Fragebogen zur Wesentlichkeitsbeurteilung.

### 3.8 `app_settings` – Key-Value-Konfiguration

Hält nicht-sensible Betriebseinstellungen (SMTP-Konfiguration,
Benachrichtigungsflags, Scanner-Pfade).

## 4 Scanner-Tabellen

### 4.1 `scan_runs` – Scan-Protokoll

| Spalte | Typ | Zweck |
|---|---|---|
| `id` | INTEGER PK | |
| `started_at` | TEXT | ISO 8601 |
| `finished_at` | TEXT | ISO 8601 |
| `scan_status` | TEXT | laufend/abgeschlossen/abgebrochen |
| `total_files`, `new_files`, `changed_files`, `moved_files`, `restored_files`, `archived_files`, `errors` | INTEGER | Kennzahlen |

### 4.2 `idv_files` – Entdeckte Dateien

| Spalte | Typ | Zweck |
|---|---|---|
| `id` | INTEGER PK | Stabile ID, bleibt bei Move/Rename gleich |
| `full_path` | TEXT UNIQUE | Absoluter Pfad (UNC) |
| `file_name` | TEXT | |
| `file_hash` | TEXT | SHA-256 |
| `file_size` | INTEGER | Byte |
| `has_macros` | INTEGER | Excel-VBA |
| `has_external_links` | INTEGER | |
| `sheet_protection` | INTEGER | |
| `file_owner` | TEXT | Dateiersteller (Windows) |
| `first_seen_at` / `last_seen_at` | TEXT | ISO 8601 |
| `status` | TEXT | `active`, `archiviert` |
| `bearbeitungsstatus` | TEXT | `Neu`, `Zur Registrierung`, `Registriert`, `Ignoriert` |
| `last_scan_run_id` | INTEGER FK → `scan_runs(id)` | |

### 4.3 `idv_file_history` – Änderungsverlauf pro Datei

Eintrag je Scan-Lauf und Datei: `change_type` ∈ {`new`, `unchanged`,
`changed`, `moved`, `archiviert`, `restored`}, optional JSON-`details`.

### 4.4 `idv_file_links` – IDV ↔ Datei (n:m)

Erlaubt einer IDV mehrere Dateien zuzuordnen (z. B. Hauptdatei + Anleitung).

## 5 Kernregister `idv_register`

Die zentrale Tabelle enthält ~70 Attribute. Ausgewählte:

### 5.1 Identifikation

| Spalte | Zweck |
|---|---|
| `id` | Interner PK |
| `idv_id` | Öffentliche ID `IDV-YYYY-NNN` (UNIQUE) |
| `bezeichnung` | Sprechender Titel |
| `idv_typ` | Excel-Makro, Python-Skript, Access, Power-BI, SQL, … |
| `version` | Fachliche Versionsnummer |
| `vorgaenger_idv_id` | FK auf Vorgängerversion |

### 5.2 Fachliche Klassifizierung

| Spalte | Zweck |
|---|---|
| `steuerungsrelevant` | 0/1 + Begründung |
| `rechnungslegungsrelevant` | 0/1 + Begründung |
| `gda_wert` | 1..4 (BAIT-Orientierungshilfe) |
| `dora_kritisch_wichtig` | 0/1 (abgeleitet aus GP + GDA) |
| `gp_id` | FK → Geschäftsprozess |
| `risikoklasse_id` | FK → Risikoklasse |
| `verfuegbarkeit`, `integritaet`, `vertraulichkeit` | CIA-Schutzziele |
| `tags` | JSON-Array |

### 5.3 Verantwortliche

| Spalte | Zweck |
|---|---|
| `org_unit_id` | Zugehörige OE |
| `fachverantwortlicher_id` | Primäre Person |
| `idv_entwickler_id` | Entwickler (relevant für Funktionstrennung) |
| `idv_koordinator_id` | Koordinator |
| `stellvertreter_id` | Vertreter |

### 5.4 Technik & Betrieb

| Spalte | Zweck |
|---|---|
| `plattform_id` | FK → Plattform |
| `nutzungsfrequenz` | täglich/wöchentlich/monatlich/… |
| `zugriffsschutz` | Beschreibung |
| `hat_makros` | bool |
| `dokumentation_vorhanden` | bool |
| `file_id` | FK → primäre Datei (optional) |

### 5.5 Workflow

| Spalte | Zweck |
|---|---|
| `status` | `Entwurf` / `In Prüfung` / `Genehmigt` / `Genehmigt mit Auflagen` / `Abgelehnt` / `Abgekündigt` / `Archiviert` |
| `teststatus` | `Wertung ausstehend` / `In Bearbeitung` / `Freigabe ausstehend` / `Freigegeben` |
| `pruefintervall_monate` | Standard 12 |
| `naechste_pruefung` | ISO-Datum |
| `letzte_aenderungsart` | `wesentlich` / `unwesentlich` |

### 5.6 Audit-Metadaten

| Spalte | Zweck |
|---|---|
| `erstellt_am`, `erfasst_von_id` | Wer hat angelegt |
| `aktualisiert_am`, `geaendert_von_id` | Letzte Änderung |
| `status_geaendert_am`, `status_geaendert_von_id` | Statusübergang |
| `interne_notizen` | Nicht in Reports |

## 6 Workflow- und Audit-Tabellen

### 6.1 `idv_history` – Änderungsprotokoll (Append-Only)

| Spalte | Zweck |
|---|---|
| `id` | PK |
| `idv_id` | FK → `idv_register` |
| `aktion` | `erstellt` / `geaendert` / `status_geaendert` / `geprueft` |
| `geaenderte_felder` | JSON-Delta (altes → neues) |
| `durchgefuehrt_von_id` | FK → `persons` |
| `durchgefuehrt_am` | ISO 8601 UTC |
| `kommentar` | Freitext |

Die Tabelle wird ausschließlich per INSERT befüllt; UPDATE/DELETE sind
fachlich nicht vorgesehen.

### 6.2 `pruefungen` – Prüfungen

| Spalte | Zweck |
|---|---|
| `id` | PK |
| `idv_id` | FK |
| `pruefungsart` | `Erstprüfung` / `Regelprüfung` / `Anlassprüfung` / `Revisionsprüfung` |
| `pruefungsdatum` | ISO-Datum |
| `pruefer_id` | FK → `persons` |
| `ergebnis` | `Ohne Befund` / `Mit Befund` / `Kritischer Befund` / `Nicht bestanden` |
| `befundbeschreibung` | Freitext |
| `naechste_pruefung` | ISO-Datum |

### 6.3 `massnahmen` – Remediation

| Spalte | Zweck |
|---|---|
| `id` | PK |
| `idv_id` | FK |
| `pruefung_id` | FK (optional) |
| `titel`, `beschreibung` | |
| `massnahmentyp` | klassifiziert |
| `prioritaet` | `Kritisch` / `Hoch` / `Mittel` / `Niedrig` |
| `verantwortlicher_id` | FK → `persons` |
| `faehlig_am` | ISO-Datum |
| `status` | `Offen` / `In Bearbeitung` / `Erledigt` / `Zurückgestellt` |
| `erledigt_am` | ISO-Datum |

### 6.4 `genehmigungen` – 4-Augen-Workflow

| Spalte | Zweck |
|---|---|
| `id` | PK |
| `idv_id` | FK |
| `genehmigungsart` | `Erstfreigabe` / `Wiederfreigabe` / `Wesentliche Änderung` / `Ablösung` |
| `stufe1_genehmiger_id` | Koordinator |
| `stufe1_status` | Ausstehend/Genehmigt/Abgelehnt |
| `stufe2_genehmiger_id` | IT-Sicherheit/Revision |
| `stufe2_status` | Ausstehend/Genehmigt/Abgelehnt/Nicht erforderlich |

### 6.5 `idv_freigaben` – Test-, Abnahme- und Archivierungsverfahren

| Spalte | Zweck |
|---|---|
| `id` | PK |
| `idv_db_id` | FK → `idv_register(id)` |
| `phase` | 1 (Test) / 2 (Abnahme) / 3 (Archivierung) |
| `schritt` | `Fachlicher Test` / `Technischer Test` / `Fachliche Abnahme` / `Technische Abnahme` / `Archivierung Originaldatei` |
| `beauftragter_id` / `durchfuehrer_id` | Personen-FK |
| `status` | `Ausstehend` / `Bestanden` / `Nicht bestanden` / `Abgebrochen` |
| `nachweise_text` | Freitext |
| `nachweis_datei_pfad`, `nachweis_datei_name` | Upload-Referenz (Nachweis) |
| `datei_verfuegbar` | NULL = n/a, 1 = Originaldatei archiviert, 0 = nicht verfügbar (Cognos etc.) |
| `archiv_datei_pfad`, `archiv_datei_name` | Revisionssichere Ablage der Originaldatei (nur Schritt „Archivierung Originaldatei") |
| `archiv_datei_sha256` | SHA-256-Prüfsumme zur Integritätssicherung |

Phase 3 („Archivierung Originaldatei") wird automatisch angelegt, sobald beide
Phase-2-Schritte erledigt sind. Erst nach Abschluss dieses Schritts (Upload
der Originaldatei **oder** dokumentierte Nicht-Verfügbarkeit mit Begründung)
wird der Teststatus der IDV auf `Freigegeben` gesetzt.

### 6.6 `idv_wesentlichkeit` – Antworten zum Wesentlichkeits-Fragebogen

Verknüpft `idv_register` mit `wesentlichkeitskriterien` (UNIQUE pro
IDV+Kriterium).

### 6.7 `fachliche_testfaelle` / `technischer_test`

Strukturierte Testfall-Dokumentation zur IDV.

## 7 Authentifizierungs-Tabellen

### 7.1 `ldap_config`

Einzeilige Konfigurationstabelle (CHECK(id=1)):

| Spalte | Zweck |
|---|---|
| `enabled` | 0/1 |
| `server_url` | `ldaps://…` |
| `port` | Standard 636 |
| `base_dn` | Suchbasis |
| `bind_dn` | Service-Account-DN |
| `bind_password` | **Fernet-verschlüsselt** |
| `user_attr` | Standard `sAMAccountName` |
| `ssl_verify` | 0/1 |
| `emergency_login` | 0/1 |

### 7.2 `ldap_group_role_mapping`

| Spalte | Zweck |
|---|---|
| `group_dn` | AD-Gruppen-DN (UNIQUE) |
| `role` | idvault-Rolle |
| `priority` | Reihenfolge bei mehreren Treffern |

## 8 Views

Views kapseln komplexe Joins für Listenansichten und Reports:

| View | Inhalt |
|---|---|
| `v_idv_uebersicht` | Vollständige IDV-Daten inkl. OE, Verantwortliche, Prüfstatus |
| `v_kritische_idvs` | Filter auf GDA=4 ODER Steuerungsrelevant ODER DORA |
| `v_offene_massnahmen` | Offene/in Bearbeitung befindliche Maßnahmen mit Person |
| `v_unvollstaendige_idvs` | QA-Check: IDVs mit fehlenden Pflichtfeldern |
| `v_prueffaelligkeiten` | Prüfungs-Fälligkeiten der nächsten 90 Tage |

## 9 Indizes

Performance-relevante Indizes auf häufig gefilterte Spalten:

| Index | Tabelle (Spalte/n) | Zweck |
|---|---|---|
| `idx_idv_status` | `idv_register(status)` | Dashboard-Filter |
| `idx_idv_gda` | `idv_register(gda_wert)` | Kritische IDVs |
| `idx_idv_steuerung` | `idv_register(steuerungsrelevant)` | Compliance-Filter |
| `idx_idv_naechste_pr` | `idv_register(naechste_pruefung)` | Fälligkeitsliste |
| `idx_idv_gp` | `idv_register(gp_id)` | Prozess-Sicht |
| `idx_idv_fachvera` | `idv_register(fachverantwortlicher_id)` | Eigene-IDV-Filter |
| `idx_files_status_bearb` | `idv_files(status, bearbeitungsstatus, has_macros, first_seen_at)` | Scanner-Eingang |
| `idx_persons_user_id` | `persons(user_id) WHERE NOT NULL` | Login-Lookup |
| `idx_pruef_idv` / `idx_pruef_dat` | `pruefungen` | Prüfungsliste |
| `idx_mass_idv` / `idx_mass_status` / `idx_mass_faehl` | `massnahmen` | Maßnahmenliste |

## 10 Integritätsregeln

- **Fremdschlüssel**: über `PRAGMA foreign_keys = ON` durchgesetzt
- **CHECK-Constraints**: `gda_wert CHECK(gda_wert BETWEEN 1 AND 4)`, `ldap_config CHECK(id=1)` u. a.
- **UNIQUE-Constraints**: `idv_id`, `user_id`, `kuerzel`, `gp_nummer`, `group_dn`, `full_path`, `idv_file_links(idv_db_id, file_id)`
- **Referenzintegrität bei Soft-Delete**: Deaktivierte Stammdaten bleiben
  erhalten; historische Referenzen werden nicht gebrochen.

## 11 Migrationsstrategie

idvault nutzt seit Issue A6 **Alembic** als Migrationsframework
(`alembic/versions/`). Der Ablauf:

- `db.py::init_register_db()` startet beim App-Start `alembic upgrade head`.
  Für leere Datenbanken werden alle Revisionen (beginnend mit
  `0001_initial_schema`) ausgeführt.
- `0001_initial_schema` liest `schema.sql` und spielt die enthaltenen
  idempotenten Statements (`CREATE TABLE IF NOT EXISTS`,
  `CREATE INDEX IF NOT EXISTS`, `INSERT OR IGNORE`) als einzelne
  SQL-Anweisungen ein. `schema.sql` ist damit die Quelle der Initial-Revision
  und dient zusätzlich als menschenlesbare Gesamtübersicht des Schemas.
- Bestehende Legacy-Datenbanken (Stand vor Alembic) erkennt
  `init_register_db` an der fehlenden `alembic_version`-Tabelle und stampt
  sie automatisch auf den passenden Revisionsstand (`0001`, `0002` oder
  `head`), bevor `upgrade head` die offenen Migrationen ausführt. Die alten
  Python-Funktionen `_migrate_risikoklasse` und `_migrate_bearbeiter_name`
  entfallen dadurch.
- **Neue Schemaänderungen** werden ausschließlich als nummerierte
  Alembic-Revisions in `alembic/versions/` gepflegt; `schema.sql` darf
  weiterhin aktualisiert werden, damit es den aktuellen Zielzustand für
  neue Installationen beschreibt.

## 12 Datenklassifikation

| Tabelle / Feld | Klassifikation | Begründung |
|---|---|---|
| `persons.password_hash` | **Vertraulich** | Kompromittierung ermöglicht Login |
| `persons.email`, `persons.telefon` | **Personenbezogen** | DSGVO Art. 4 |
| `ldap_config.bind_password` | **Vertraulich (verschlüsselt)** | Service-Account-Passwort |
| `app_settings` (SMTP-Passwort) | **Vertraulich** | Derzeit als Klartext; Migration empfohlen |
| `idv_register.interne_notizen` | **Intern** | Nicht für Reports |
| alle übrigen Fachdaten | **Intern (Bank)** | IDV-Register an sich |

## 13 Backup-Empfehlung

| Art | Methode |
|---|---|
| Vollsicherung | Tägliche Kopie der `instance/`-Struktur bei gestoppter Anwendung **oder** Online-Backup mit `sqlite3 idvault.db ".backup /backup/..."` |
| Log-Sicherung | Logs mittels Logshipping in das bestehende SIEM |
| Rücksicherungstest | Halbjährlich in einer Testumgebung |
| Aufbewahrung | Mindestens 10 Jahre (Handelsrecht § 257 HGB) |
