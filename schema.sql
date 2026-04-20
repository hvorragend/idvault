-- =============================================================================
-- IDV-Register Datenmodell
-- Volksbank Gronau-Ahaus eG
-- Basis: MaRisk AT 7.2, DORA Art. 28/30, BAIT Tz. 52-56
-- =============================================================================
-- Konventionen:
--   TEXT        für alle Strings, Enums und Datumsfelder (ISO 8601)
--   INTEGER     für Flags (0/1) und Ganzzahlen
--   REAL        für Prozentangaben / Bewertungsscores
--   created_at / updated_at immer in UTC (ISO 8601)
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode  = WAL;

-- -----------------------------------------------------------------------------
-- Scanner-Tabellen (Stub – wird vom idv_scanner.py befüllt;
-- kann in derselben oder einer separaten DB liegen.)
-- -----------------------------------------------------------------------------

-- Scan-Läufe (jede Ausführung des Scanners ist ein Eintrag)
CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    scan_paths      TEXT,           -- JSON-Array der gescannten Pfade
    total_files     INTEGER DEFAULT 0,
    new_files       INTEGER DEFAULT 0,
    changed_files   INTEGER DEFAULT 0,
    moved_files     INTEGER DEFAULT 0,
    restored_files  INTEGER DEFAULT 0,
    archived_files  INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    scan_status     TEXT NOT NULL DEFAULT 'completed'
    -- 'running' | 'completed' | 'cancelled' | 'checkpoint'
);

CREATE TABLE IF NOT EXISTS idv_files (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash               TEXT NOT NULL,
    full_path               TEXT NOT NULL,
    file_name               TEXT NOT NULL,
    extension               TEXT NOT NULL,
    share_root              TEXT,
    relative_path           TEXT,
    size_bytes              INTEGER,
    created_at              TEXT,
    modified_at             TEXT,
    file_owner              TEXT,
    office_author           TEXT,
    office_last_author      TEXT,
    office_created          TEXT,
    office_modified         TEXT,
    has_macros              INTEGER DEFAULT 0,
    has_external_links      INTEGER DEFAULT 0,
    sheet_count             INTEGER,
    named_ranges_count      INTEGER,
    formula_count           INTEGER DEFAULT 0,       -- Anzahl Formelzellen (Excel)
    has_sheet_protection    INTEGER DEFAULT 0,
    protected_sheets_count  INTEGER DEFAULT 0,
    sheet_protection_has_pw INTEGER DEFAULT 0,
    workbook_protected      INTEGER DEFAULT 0,
    -- Cognos IDA-Report (*.ida)
    ist_cognos_report         INTEGER DEFAULT 0,
    cognos_report_name        TEXT,
    cognos_paket_pfad         TEXT,
    cognos_abfragen_anzahl    INTEGER,
    cognos_datenpunkte_anzahl INTEGER,
    cognos_filter_anzahl      INTEGER,
    cognos_seiten_anzahl      INTEGER,
    cognos_parameter_anzahl   INTEGER,
    cognos_namespace_version  TEXT,
    first_seen_at           TEXT NOT NULL DEFAULT (datetime('now','utc')),
    last_seen_at            TEXT NOT NULL DEFAULT (datetime('now','utc')),
    last_scan_run_id        INTEGER,
    status                  TEXT DEFAULT 'active',
    bearbeitungsstatus      TEXT NOT NULL DEFAULT 'Neu',
    source                  TEXT NOT NULL DEFAULT 'filesystem', -- 'filesystem' | 'sharepoint' | 'teams'
    sharepoint_item_id      TEXT,                              -- stabile Graph-API-ID (Teams/SharePoint)
    UNIQUE(full_path)
);

CREATE INDEX IF NOT EXISTS idx_files_sp_item ON idv_files(sharepoint_item_id);

-- Delta-Token pro Drive für inkrementellen Graph-API-Sync (Teams-Scanner)
CREATE TABLE IF NOT EXISTS teams_delta_tokens (
    drive_id    TEXT PRIMARY KEY,
    delta_token TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- 0. STAMMDATEN / LOOKUP-TABELLEN
-- -----------------------------------------------------------------------------

-- Organisationseinheiten (Fachbereiche / Abteilungen)
CREATE TABLE IF NOT EXISTS org_units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bezeichnung TEXT NOT NULL,
    parent_id   INTEGER REFERENCES org_units(id),
    aktiv       INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- Personen / Benutzer (IDV-Verantwortliche, Prüfer, Genehmiger)
CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kuerzel         TEXT NOT NULL UNIQUE,            -- z.B. "MMA"
    nachname        TEXT NOT NULL,
    vorname         TEXT NOT NULL,
    email           TEXT,
    telefon         TEXT,
    org_unit_id     INTEGER REFERENCES org_units(id),
    rolle           TEXT,                            -- "IDV-Koordinator" | "Fachverantwortlicher" | "IT-Sicherheit" | "Revision"
    aktiv           INTEGER NOT NULL DEFAULT 1,
    user_id         TEXT,                            -- Login-Name (LDAP / Windows)
    ad_name         TEXT,                            -- AD-Distinguished-Name
    password_hash   TEXT,                            -- SHA-256 (Fallback-Login)
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_user_id
    ON persons(user_id) WHERE user_id IS NOT NULL;

-- Geschäftsprozesse (GP-Katalog)
CREATE TABLE IF NOT EXISTS geschaeftsprozesse (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gp_nummer       TEXT NOT NULL UNIQUE,            -- z.B. "GP-KRE-001"
    bezeichnung     TEXT NOT NULL,                   -- z.B. "Kreditentscheidung Firmenkunden"
    bereich         TEXT,                            -- "Markt" | "Marktfolge" | "Steuerung" | "Betrieb"
    org_unit_id     INTEGER REFERENCES org_units(id),
    -- DORA-Klassifizierung
    ist_kritisch    INTEGER NOT NULL DEFAULT 0,     -- kritisch/wichtig i.S.v. DORA Art. 28
    ist_wesentlich  INTEGER NOT NULL DEFAULT 0,     -- wesentlich i.S.v. MaRisk
    -- Schutzbedarf (A=Verfügbarkeit, C=Vertraulichkeit, I=Integrität, N=Authentizität)
    schutzbedarf_a  TEXT,
    schutzbedarf_c  TEXT,
    schutzbedarf_i  TEXT,
    schutzbedarf_n  TEXT,
    -- Bewertung
    beschreibung    TEXT,
    aktiv           INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- Technologieplattformen / Hostsysteme
CREATE TABLE IF NOT EXISTS plattformen (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bezeichnung TEXT NOT NULL UNIQUE,   -- z.B. "Windows 11", "SharePoint Online", "OSPlus"
    typ         TEXT,                   -- "Desktop" | "Server" | "Cloud" | "Mobile"
    hersteller  TEXT,
    aktiv       INTEGER NOT NULL DEFAULT 1
);

-- Standard-Einträge Plattformen
INSERT OR IGNORE INTO plattformen (bezeichnung, typ, hersteller) VALUES
    ('Microsoft Excel',     'Desktop', 'Microsoft'),
    ('Microsoft Access',    'Desktop', 'Microsoft'),
    ('Power BI Desktop',    'Desktop', 'Microsoft'),
    ('HCL Notes',           'Desktop', 'HCL'),
    ('Business Intelligence','Desktop','BI'),
    ('Shell-Skripte',       'Konsole', ''),
    ('UiPath Studio',       'IDE',     'UiPath'),
    ('Python 3.11',         'Server',  'PSF');

-- Risikoklassen (konfigurierbar)
CREATE TABLE IF NOT EXISTS risikoklassen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bezeichnung     TEXT NOT NULL UNIQUE, -- z.B. "Kritisch", "Hoch", "Mittel", "Gering"
    farbe_hex       TEXT,                 -- z.B. "#FF0000"
    sort_order      INTEGER NOT NULL DEFAULT 0,
    beschreibung    TEXT
);

-- Standard-Einträge Risikoklassen
INSERT OR IGNORE INTO risikoklassen (bezeichnung, farbe_hex, sort_order) VALUES
    ('Kritisch', '#C00000', 1),
    ('Hoch',     '#FF0000', 2),
    ('Mittel',   '#FFA500', 3),
    ('Gering',   '#00B050', 4);

-- Standard-Eintrag für unbekannte / nicht zugeordnete OE
INSERT OR IGNORE INTO org_units (id, bezeichnung) VALUES
    (1, '(unbekannt / nicht zugeordnet)');

-- Anwendungs-Einstellungen (SMTP etc.)
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO app_settings (key, value) VALUES
    ('smtp_host',     ''),
    ('smtp_port',     '587'),
    ('smtp_user',     ''),
    ('smtp_password', ''),
    ('smtp_from',     ''),
    ('smtp_tls',      'starttls'),
    ('notify_enabled_neue_datei',             '1'),
    ('notify_enabled_pruefung_faellig',       '1'),
    ('notify_enabled_freigabe_schritt',       '1'),
    ('notify_enabled_freigabe_abgeschlossen', '1'),
    ('notify_enabled_bewertung',              '1'),
    ('notify_enabled_massnahme_ueberfaellig', '1'),
    ('auto_ignore_no_formula', '0'),
    -- Keys, die seit 2026-04 aus der config.json in die DB gewandert sind:
    ('login_rate_limit',        '5 per minute;30 per hour'),
    ('upload_rate_limit',       '10 per minute;60 per hour'),
    ('allow_sidecar_updates',   '1'),
    ('path_mappings',           '[]'),
    ('scanner_config',          '{}'),
    ('teams_config',            '{}'),
    ('teams_client_secret_enc', '');

-- SMTP-Versandlog (letzte Sendevorgänge, max. 200 Einträge)
CREATE TABLE IF NOT EXISTS smtp_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at    TEXT    NOT NULL,
    recipients TEXT    NOT NULL,
    subject    TEXT    NOT NULL,
    success    INTEGER NOT NULL DEFAULT 0,
    error_msg  TEXT
);

-- Konfigurierbare Klassifizierungskriterien
CREATE TABLE IF NOT EXISTS klassifizierungen (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bereich      TEXT NOT NULL,
    wert         TEXT NOT NULL,
    bezeichnung  TEXT,
    beschreibung TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    aktiv        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(bereich, wert)
);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, sort_order) VALUES
    ('idv_typ', 'unklassifiziert',   0),
    ('idv_typ', 'Excel-Tabelle',     1),
    ('idv_typ', 'Excel-Makro',       2),
    ('idv_typ', 'Excel-Modell',      3),
    ('idv_typ', 'Access-Datenbank',  4),
    ('idv_typ', 'Python-Skript',     5),
    ('idv_typ', 'SQL-Skript',        6),
    ('idv_typ', 'Power-BI-Bericht',  7),
    ('idv_typ', 'Cognos-Report',     8),
    ('idv_typ', 'Shell-Skript',      9),
    ('idv_typ', 'Gruppenrichtlinie', 10),
    ('idv_typ', 'Sonstige',          11);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, bezeichnung, sort_order) VALUES
    ('pruefintervall_monate', '3',  '3 Monate (quartalsweise)', 1),
    ('pruefintervall_monate', '6',  '6 Monate (halbjährlich)',  2),
    ('pruefintervall_monate', '12', '12 Monate (jährlich)',     3),
    ('pruefintervall_monate', '24', '24 Monate (alle 2 Jahre)', 4);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, sort_order) VALUES
    ('nutzungsfrequenz', 'täglich',       1),
    ('nutzungsfrequenz', 'wöchentlich',   2),
    ('nutzungsfrequenz', 'monatlich',     3),
    ('nutzungsfrequenz', 'quartalsweise', 4),
    ('nutzungsfrequenz', 'jährlich',      5),
    ('nutzungsfrequenz', 'anlassbezogen', 6);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, sort_order) VALUES
    ('pruefungsart', 'Erstprüfung',      1),
    ('pruefungsart', 'Regelprüfung',     2),
    ('pruefungsart', 'Anlassprüfung',    3),
    ('pruefungsart', 'Revisionsprüfung', 4);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, sort_order) VALUES
    ('pruefungs_ergebnis', 'Ohne Befund',       1),
    ('pruefungs_ergebnis', 'Mit Befund',        2),
    ('pruefungs_ergebnis', 'Kritischer Befund', 3),
    ('pruefungs_ergebnis', 'Nicht bestanden',   4);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, sort_order) VALUES
    ('massnahmentyp', 'Technisch',       1),
    ('massnahmentyp', 'Organisatorisch', 2),
    ('massnahmentyp', 'Dokumentation',   3),
    ('massnahmentyp', 'Ablösung',        4),
    ('massnahmentyp', 'Sonstiges',       5);

INSERT OR IGNORE INTO klassifizierungen (bereich, wert, sort_order) VALUES
    ('massnahmen_prioritaet', 'Kritisch', 1),
    ('massnahmen_prioritaet', 'Hoch',     2),
    ('massnahmen_prioritaet', 'Mittel',   3),
    ('massnahmen_prioritaet', 'Niedrig',  4);

-- -----------------------------------------------------------------------------
-- 1. IDV-REGISTER (Kerntabelle)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idv_register (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- -------------------------------------------------------------------------
    -- Identifikation
    -- -------------------------------------------------------------------------
    idv_id                  TEXT NOT NULL UNIQUE,       -- "IDV-2025-001" (generiert)
    bezeichnung             TEXT NOT NULL,              -- Sprechender Name
    kurzbeschreibung        TEXT,                       -- 2-3 Sätze Zweckbeschreibung
    version                 TEXT NOT NULL DEFAULT '1.0',

    -- Verknüpfung zur Scanner-Tabelle (kann NULL sein bei manuell erfassten IDVs)
    file_id                 INTEGER REFERENCES idv_files(id),
    -- Alternativer Pfad bei mehreren verknüpften Dateien (JSON-Array)
    weitere_dateien         TEXT,                       -- JSON: ["\\server\...", ...]

    -- -------------------------------------------------------------------------
    -- Klassifizierung IDV-Typ (technische Kategorie)
    -- -------------------------------------------------------------------------
    idv_typ                 TEXT NOT NULL DEFAULT 'unklassifiziert',
    -- Zulässige Werte:
    -- 'Excel-Tabelle'        Reine Datentabelle ohne Makros
    -- 'Excel-Makro'          Excel mit VBA-Makros (XLSM/XLSB)
    -- 'Excel-Modell'         Komplexes Berechnungsmodell
    -- 'Access-Datenbank'     MDB/ACCDB
    -- 'Python-Skript'        Eigenentwicklung Python
    -- 'SQL-Skript'           Direkte DB-Abfragen
    -- 'Power-BI-Bericht'     PBIX mit Datentransformation
    -- 'Sonstige'
    -- 'unklassifiziert'      Noch nicht bewertet

    -- -------------------------------------------------------------------------
    -- Entwicklungsart (regulatorische Kategorie nach MaRisk / DORA / BAIT)
    -- -------------------------------------------------------------------------
    -- Grenzt den Datensatz begrifflich von den Notes-IdVault-Einträgen und den
    -- zentral verwalteten Anwendungen ab. Der Übergang Arbeitshilfe → IDV wird
    -- automatisch aus der Wesentlichkeitsprüfung abgeleitet.
    entwicklungsart         TEXT NOT NULL DEFAULT 'arbeitshilfe'
        CHECK (entwicklungsart IN
            ('eigenprogrammierung','auftragsprogrammierung','idv','arbeitshilfe')),
    -- 'eigenprogrammierung'   Interne IT, zentraler IT-Prozess (MaRisk AT 7.2)
    -- 'auftragsprogrammierung' Externer Dienstleister, DORA-Drittparteienmgmt.
    -- 'idv'                   Fachbereich, dezentral, wesentlich
    -- 'arbeitshilfe'          Fachbereich, dezentral, unterhalb Wesentlichkeit

    -- -------------------------------------------------------------------------
    -- Wesentlichkeitsbeurteilung
    -- Alle Kriterien werden dynamisch über die Tabellen
    --   wesentlichkeitskriterien / wesentlichkeitskriterium_details
    --   idv_wesentlichkeit / idv_wesentlichkeit_detail
    -- abgebildet und sind im Admin-Bereich konfigurierbar.
    -- -------------------------------------------------------------------------

    -- -------------------------------------------------------------------------
    -- Geschäftsprozess-Zuordnung
    -- -------------------------------------------------------------------------
    gp_id                   INTEGER REFERENCES geschaeftsprozesse(id),
    gp_freitext             TEXT,                       -- Falls GP noch nicht im Katalog

    -- -------------------------------------------------------------------------
    -- Risikobewertung
    -- -------------------------------------------------------------------------
    risikoklasse_id         INTEGER REFERENCES risikoklassen(id),
    -- Risikodimensionen (je 1–5, 5=höchstes Risiko)
    risiko_verfuegbarkeit   INTEGER CHECK(risiko_verfuegbarkeit BETWEEN 1 AND 5),
    risiko_integritaet      INTEGER CHECK(risiko_integritaet BETWEEN 1 AND 5),
    risiko_vertraulichkeit  INTEGER CHECK(risiko_vertraulichkeit BETWEEN 1 AND 5),
    risiko_nachvollziehbarkeit INTEGER CHECK(risiko_nachvollziehbarkeit BETWEEN 1 AND 5),
    risiko_kommentar        TEXT,

    -- -------------------------------------------------------------------------
    -- Verantwortlichkeiten
    -- -------------------------------------------------------------------------
    org_unit_id             INTEGER REFERENCES org_units(id),  -- Zuständige OE
    fachverantwortlicher_id INTEGER REFERENCES persons(id),    -- Fachliche Verantwortung
    idv_entwickler_id       INTEGER REFERENCES persons(id),    -- Entwickler / Ersteller
    idv_koordinator_id      INTEGER REFERENCES persons(id),    -- IDV-Koordinator OE
    stellvertreter_id       INTEGER REFERENCES persons(id),    -- Stellvertretung

    -- -------------------------------------------------------------------------
    -- Technische Angaben
    -- -------------------------------------------------------------------------
    plattform_id            INTEGER REFERENCES plattformen(id),
    programmiersprache      TEXT,                       -- "VBA", "Python", "SQL", "DAX"
    datenbankanbindung      INTEGER NOT NULL DEFAULT 0, -- externe DB-Verbindung?
    datenbankanbindung_beschr TEXT,
    netzwerkzugriff         INTEGER NOT NULL DEFAULT 0, -- Netzwerkzugriff / API-Calls?
    schnittstellen          TEXT,                       -- JSON-Array Schnittstellen
    datenquellen            TEXT,                       -- Freitext: woher kommen die Daten?
    datenempfaenger         TEXT,                       -- Freitext: wohin gehen die Daten?

    -- -------------------------------------------------------------------------
    -- Datenschutz / Datenkategorien
    -- -------------------------------------------------------------------------
    enthaelt_personendaten  INTEGER NOT NULL DEFAULT 0,
    datenschutz_kategorie   TEXT,                       -- "keine" | "allgemein" | "besonders sensibel"
    datenschutz_kommentar   TEXT,

    -- -------------------------------------------------------------------------
    -- Nutzung & Betrieb
    -- -------------------------------------------------------------------------
    nutzungsfrequenz        TEXT,                       -- "täglich" | "wöchentlich" | "monatlich" | "quartalsweise" | "anlassbezogen"
    nutzeranzahl            INTEGER,                    -- Anzahl aktiver Nutzer
    nutzungsumfang          TEXT,                       -- Freitext
    produktiv_seit          TEXT,                       -- Datum ISO 8601
    letzte_aenderung_fachlich TEXT,                     -- Fachlich relevante Änderung

    -- -------------------------------------------------------------------------
    -- Dokumentation & Qualitätssicherung
    -- -------------------------------------------------------------------------
    dokumentation_vorhanden INTEGER NOT NULL DEFAULT 0,
    dokumentation_pfad      TEXT,
    testkonzept_vorhanden   INTEGER NOT NULL DEFAULT 0,
    versionskontrolle       INTEGER NOT NULL DEFAULT 0, -- Git o.ä.
    zugriffsschutz          INTEGER NOT NULL DEFAULT 0, -- Passwortschutz / Rechteverwaltung
    zugriffsschutz_beschr   TEXT,
    vier_augen_prinzip      INTEGER NOT NULL DEFAULT 0,

    -- -------------------------------------------------------------------------
    -- Ablösung / Lebenszyklus
    -- -------------------------------------------------------------------------
    abloesung_geplant       INTEGER NOT NULL DEFAULT 0,
    abloesung_zieldatum     TEXT,
    abloesung_durch         TEXT,                       -- "OSPlus-Erweiterung", "Eigenentwicklung neu", etc.
    abloesung_kommentar     TEXT,

    -- -------------------------------------------------------------------------
    -- Workflow / Freigabestatus
    -- -------------------------------------------------------------------------
    -- 'Entwurf'             Ersterfassung, noch nicht geprüft
    -- 'In Prüfung'          Liegt beim IDV-Koordinator / Fachverantwortlichen
    -- 'Genehmigt'           Freigegeben (ggf. mit Auflagen)
    -- 'Genehmigt mit Auflagen'
    -- 'Abgelehnt'           Nicht als IDV eingestuft oder nicht genehmigungsfähig
    -- 'Abgekündigt'         IDV wird abgelöst / abgeschaltet
    -- 'Archiviert'          Nicht mehr aktiv, historisch
    status                  TEXT NOT NULL DEFAULT 'Entwurf',
    status_geaendert_am     TEXT,
    status_geaendert_von_id INTEGER REFERENCES persons(id),

    -- -------------------------------------------------------------------------
    -- Prüfintervall / Wiedervorlage
    -- -------------------------------------------------------------------------
    pruefintervall_monate   INTEGER NOT NULL DEFAULT 12, -- Standard: jährlich
    naechste_pruefung       TEXT,                        -- ISO 8601 Datum
    letzte_pruefung         TEXT,                        -- ISO 8601 Datum

    -- -------------------------------------------------------------------------
    -- Metadaten
    -- -------------------------------------------------------------------------
    erfasst_von_id          INTEGER REFERENCES persons(id),
    erstellt_am             TEXT NOT NULL DEFAULT (datetime('now','utc')),
    aktualisiert_am         TEXT NOT NULL DEFAULT (datetime('now','utc')),
    interne_notizen         TEXT,                        -- Nur intern, nicht im Report
    tags                    TEXT,                        -- JSON-Array: ["Jahresabschluss","Meldewesen"]

    -- -------------------------------------------------------------------------
    -- Erweiterte Felder (Betrieb / Workflow / Versioning)
    -- -------------------------------------------------------------------------
    gobd_relevant               INTEGER NOT NULL DEFAULT 0,
    erstellt_fuer               TEXT,
    schnittstellen_beschr       TEXT,
    teststatus                  TEXT NOT NULL DEFAULT 'Wertung ausstehend',
    vorgaenger_idv_id           INTEGER REFERENCES idv_register(id),
    -- Änderungsart bei neuer Version
    letzte_aenderungsart        TEXT,          -- 'wesentlich' | 'unwesentlich'
    letzte_aenderungsbegruendung TEXT
);

-- Indizes IDV-Register
CREATE INDEX IF NOT EXISTS idx_idv_status      ON idv_register(status);
CREATE INDEX IF NOT EXISTS idx_idv_naechste_pr ON idv_register(naechste_pruefung);
CREATE INDEX IF NOT EXISTS idx_idv_gp          ON idv_register(gp_id);
CREATE INDEX IF NOT EXISTS idx_idv_fachvera    ON idv_register(fachverantwortlicher_id);

-- -----------------------------------------------------------------------------
-- 2. IDV-ÄNDERUNGSHISTORIE (Audit Trail)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idv_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id          INTEGER NOT NULL REFERENCES idv_register(id),
    aktion          TEXT NOT NULL,  -- 'erstellt' | 'geaendert' | 'status_geaendert' | 'geprueft' | 'kommentar'
    geaenderte_felder TEXT,         -- JSON: {"field": {"alt": ..., "neu": ...}}
    kommentar       TEXT,
    durchgefuehrt_von_id INTEGER REFERENCES persons(id),
    durchgefuehrt_am TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_history_idv ON idv_history(idv_id);

-- -----------------------------------------------------------------------------
-- 3. PRÜFUNGEN (Reviews)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pruefungen (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id              INTEGER NOT NULL REFERENCES idv_register(id),
    pruefungsart        TEXT NOT NULL,  -- 'Erstprüfung' | 'Regelprüfung' | 'Anlassprüfung' | 'Revisionspr.'
    pruefungsdatum      TEXT NOT NULL,
    pruefer_id          INTEGER REFERENCES persons(id),
    ergebnis            TEXT NOT NULL,  -- 'Ohne Befund' | 'Mit Befund' | 'Kritischer Befund' | 'Nicht bestanden'
    befunde             TEXT,           -- Freitext Befundbeschreibung
    massnahmen_erforderlich INTEGER NOT NULL DEFAULT 0,
    frist_massnahmen    TEXT,           -- ISO 8601 Datum
    abgeschlossen       INTEGER NOT NULL DEFAULT 0,
    abschlussdatum      TEXT,
    naechste_pruefung   TEXT,           -- ISO 8601 Datum
    kommentar           TEXT,
    erstellt_am         TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_pruef_idv  ON pruefungen(idv_id);
CREATE INDEX IF NOT EXISTS idx_pruef_dat  ON pruefungen(pruefungsdatum);

-- -----------------------------------------------------------------------------
-- 4. MASSNAHMEN (aus Prüfungen oder präventiv)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS massnahmen (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id              INTEGER NOT NULL REFERENCES idv_register(id),
    pruefung_id         INTEGER REFERENCES pruefungen(id),  -- NULL = präventive Maßnahme
    titel               TEXT NOT NULL,
    beschreibung        TEXT,
    massnahmentyp       TEXT,   -- 'Technisch' | 'Organisatorisch' | 'Dokumentation' | 'Ablösung'
    prioritaet          TEXT NOT NULL DEFAULT 'Mittel',  -- 'Kritisch' | 'Hoch' | 'Mittel' | 'Niedrig'
    verantwortlicher_id INTEGER REFERENCES persons(id),
    faellig_am          TEXT,   -- ISO 8601
    status              TEXT NOT NULL DEFAULT 'Offen',  -- 'Offen' | 'In Bearbeitung' | 'Erledigt' | 'Zurückgestellt'
    erledigt_am         TEXT,
    erledigt_von_id     INTEGER REFERENCES persons(id),
    erledigung_kommentar TEXT,
    erstellt_am         TEXT NOT NULL DEFAULT (datetime('now','utc')),
    aktualisiert_am     TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_mass_idv    ON massnahmen(idv_id);
CREATE INDEX IF NOT EXISTS idx_mass_status ON massnahmen(status);
CREATE INDEX IF NOT EXISTS idx_mass_faehl  ON massnahmen(faellig_am);

-- -----------------------------------------------------------------------------
-- 5. IDV-ABHÄNGIGKEITEN (IDV ↔ IDV Beziehungen)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idv_abhaengigkeiten (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    quell_idv_id    INTEGER NOT NULL REFERENCES idv_register(id),
    ziel_idv_id     INTEGER NOT NULL REFERENCES idv_register(id),
    abhaengigkeitstyp TEXT NOT NULL,  -- 'Datenlieferant' | 'Datenempfänger' | 'Steuert' | 'Wird gesteuert von'
    beschreibung    TEXT,
    erstellt_am     TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE(quell_idv_id, ziel_idv_id, abhaengigkeitstyp)
);

-- -----------------------------------------------------------------------------
-- 6. GENEHMIGUNGEN / FREIGABEN (4-Augen-Workflow)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS genehmigungen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id          INTEGER NOT NULL REFERENCES idv_register(id),
    genehmigungsart TEXT NOT NULL,  -- 'Erstfreigabe' | 'Wiederfreigabe' | 'Wesentliche Änderung' | 'Ablösung'
    antragsteller_id INTEGER REFERENCES persons(id),
    antragsdatum    TEXT NOT NULL,
    -- Genehmigungsstufe 1: IDV-Koordinator / Fachverantwortlicher
    genehmiger1_id  INTEGER REFERENCES persons(id),
    genehmigt1_am   TEXT,
    genehmigt1_status TEXT,         -- 'Genehmigt' | 'Abgelehnt' | 'Ausstehend'
    genehmigt1_kommentar TEXT,
    -- Genehmigungsstufe 2: IT-Sicherheit / Revision (bei GDA=4 oder kritisch/wichtig)
    genehmiger2_id  INTEGER REFERENCES persons(id),
    genehmigt2_am   TEXT,
    genehmigt2_status TEXT,         -- 'Genehmigt' | 'Abgelehnt' | 'Ausstehend' | 'Nicht erforderlich'
    genehmigt2_kommentar TEXT,
    -- Gesamtstatus
    gesamtstatus    TEXT NOT NULL DEFAULT 'Ausstehend',
    abschlussdatum  TEXT,
    erstellt_am     TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_genehm_idv ON genehmigungen(idv_id);

-- -----------------------------------------------------------------------------
-- 7. DOKUMENTE / ANHÄNGE (Verweise auf Dokumente im Netz/SharePoint)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dokumente (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id          INTEGER NOT NULL REFERENCES idv_register(id),
    dokumenttyp     TEXT NOT NULL,  -- 'Fachkonzept' | 'Testprotokoll' | 'Freigabeprotokoll' | 'Risikoanalyse' | 'Sonstiges'
    bezeichnung     TEXT NOT NULL,
    pfad_oder_url   TEXT NOT NULL,  -- UNC-Pfad oder SharePoint-URL
    version         TEXT,
    erstellt_am_dok TEXT,
    hochgeladen_von_id INTEGER REFERENCES persons(id),
    hochgeladen_am  TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- -----------------------------------------------------------------------------
-- 8. VIEWS (vordefinierte Auswertungen)
-- -----------------------------------------------------------------------------

-- Vollständige IDV-Übersicht
CREATE VIEW IF NOT EXISTS v_idv_uebersicht AS
SELECT
    r.id                        AS idv_db_id,
    r.idv_id,
    r.bezeichnung,
    r.idv_typ,
    r.status,
    CASE WHEN EXISTS (
        SELECT 1 FROM idv_wesentlichkeit iw
        WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
    ) THEN 'Ja' ELSE 'Nein' END AS ist_wesentlich,
    rk.bezeichnung              AS risikoklasse,
    gp.gp_nummer,
    gp.bezeichnung              AS geschaeftsprozess,
    ou.bezeichnung              AS org_einheit,
    p_fv.nachname || ', ' || p_fv.vorname AS fachverantwortlicher,
    p_en.nachname || ', ' || p_en.vorname AS entwickler,
    r.naechste_pruefung,
    CASE
        WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
        WHEN r.naechste_pruefung < date('now', '+30 days') THEN 'BALD FÄLLIG'
        ELSE 'OK'
    END                         AS pruefstatus,
    r.abloesung_geplant,
    r.abloesung_zieldatum,
    f.file_name,
    f.full_path,
    f.modified_at               AS datei_geaendert,
    r.erstellt_am,
    r.aktualisiert_am
FROM idv_register r
LEFT JOIN risikoklassen        rk  ON r.risikoklasse_id = rk.id
LEFT JOIN geschaeftsprozesse   gp  ON r.gp_id = gp.id
LEFT JOIN org_units            ou  ON r.org_unit_id = ou.id
LEFT JOIN persons              p_fv ON r.fachverantwortlicher_id = p_fv.id
LEFT JOIN persons              p_en ON r.idv_entwickler_id = p_en.id
LEFT JOIN idv_files            f   ON r.file_id = f.id
WHERE r.status NOT IN ('Archiviert');

-- Wesentliche IDVs (mindestens ein Wesentlichkeitskriterium erfüllt)
CREATE VIEW IF NOT EXISTS v_kritische_idvs AS
SELECT * FROM v_idv_uebersicht
WHERE ist_wesentlich = 'Ja'
ORDER BY risikoklasse;

-- Offene Maßnahmen mit Fälligkeit
CREATE VIEW IF NOT EXISTS v_offene_massnahmen AS
SELECT
    m.id            AS massnahme_id,
    r.idv_id,
    r.bezeichnung   AS idv_bezeichnung,
    m.titel,
    m.prioritaet,
    m.status,
    m.faellig_am,
    CASE
        WHEN m.faellig_am < date('now') THEN 'ÜBERFÄLLIG'
        WHEN m.faellig_am < date('now', '+14 days') THEN 'BALD FÄLLIG'
        ELSE 'OK'
    END             AS faelligkeitsstatus,
    p.nachname || ', ' || p.vorname AS verantwortlicher,
    m.beschreibung
FROM massnahmen m
JOIN idv_register r ON m.idv_id = r.id
LEFT JOIN persons p ON m.verantwortlicher_id = p.id
WHERE m.status IN ('Offen', 'In Bearbeitung')
ORDER BY m.faellig_am ASC;

-- IDVs ohne vollständige Klassifizierung (Qualitätssicherung)
CREATE VIEW IF NOT EXISTS v_unvollstaendige_idvs AS
SELECT
    r.idv_id,
    r.bezeichnung,
    r.status,
    CASE WHEN r.fachverantwortlicher_id IS NULL THEN 1 ELSE 0 END AS fehlt_fachverantwortlicher,
    CASE WHEN r.gp_id IS NULL AND r.gp_freitext IS NULL THEN 1 ELSE 0 END AS fehlt_geschaeftsprozess,
    CASE WHEN r.idv_typ = 'unklassifiziert' THEN 1 ELSE 0 END AS fehlt_typ,
    CASE WHEN EXISTS (
        SELECT 1 FROM idv_wesentlichkeit iw
        JOIN wesentlichkeitskriterien k ON k.id = iw.kriterium_id
        WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
          AND k.begruendung_pflicht = 1
          AND (iw.begruendung IS NULL OR iw.begruendung = '')
    ) THEN 1 ELSE 0 END AS fehlt_wesentlichkeitsbegruendung,
    CASE WHEN r.risikoklasse_id IS NULL THEN 1 ELSE 0 END AS fehlt_risikoklasse,
    r.erstellt_am,
    r.aktualisiert_am
FROM idv_register r
WHERE r.status NOT IN ('Archiviert')
  AND (
    r.fachverantwortlicher_id IS NULL
    OR (r.gp_id IS NULL AND r.gp_freitext IS NULL)
    OR r.idv_typ = 'unklassifiziert'
    OR r.risikoklasse_id IS NULL
    OR EXISTS (
        SELECT 1 FROM idv_wesentlichkeit iw
        JOIN wesentlichkeitskriterien k ON k.id = iw.kriterium_id
        WHERE iw.idv_db_id = r.id AND iw.erfuellt = 1
          AND k.begruendung_pflicht = 1
          AND (iw.begruendung IS NULL OR iw.begruendung = '')
    )
  );

-- -----------------------------------------------------------------------------
-- 9. KONFIGURIERBARE WESENTLICHKEITSKRITERIEN (MaRisk AT 7.2 / DORA)
-- -----------------------------------------------------------------------------

-- Vom Administrator vollständig konfigurierbare Wesentlichkeitskriterien.
-- Alle Kriterien – auch die gemäß MaRisk/DORA – sind als Datensätze in dieser
-- Tabelle hinterlegt. Beim initialen Setup werden drei Beispielkriterien
-- angelegt, die anschließend umbenannt, ergänzt oder deaktiviert werden können.
CREATE TABLE IF NOT EXISTS wesentlichkeitskriterien (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bezeichnung         TEXT NOT NULL,           -- Anzeigename
    beschreibung        TEXT,                    -- Erläuterung / Hilfetext im Formular
    begruendung_pflicht INTEGER NOT NULL DEFAULT 0, -- Begründung erforderlich wenn erfüllt?
    sort_order          INTEGER NOT NULL DEFAULT 0,
    aktiv               INTEGER NOT NULL DEFAULT 1,
    erstellt_am         TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- Checkbox-Details je Kriterium (z.B. "Generierung von Buchungsbelegen",
-- "Import aus Schnittstellen"). Jeder Eintrag ist eine optionale Checkbox,
-- die innerhalb eines Kriteriums zusätzlich ausgewählt werden kann.
CREATE TABLE IF NOT EXISTS wesentlichkeitskriterium_details (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kriterium_id    INTEGER NOT NULL REFERENCES wesentlichkeitskriterien(id) ON DELETE CASCADE,
    bezeichnung     TEXT NOT NULL,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    aktiv           INTEGER NOT NULL DEFAULT 1,
    erstellt_am     TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (kriterium_id, bezeichnung)
);

CREATE INDEX IF NOT EXISTS idx_wk_details_krit ON wesentlichkeitskriterium_details(kriterium_id);

-- Antworten je IDV auf konfigurierbare Wesentlichkeitskriterien
CREATE TABLE IF NOT EXISTS idv_wesentlichkeit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_db_id       INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
    kriterium_id    INTEGER NOT NULL REFERENCES wesentlichkeitskriterien(id),
    erfuellt        INTEGER NOT NULL DEFAULT 0,   -- 0 = nein, 1 = ja
    begruendung     TEXT,
    geaendert_am    TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (idv_db_id, kriterium_id)
);

CREATE INDEX IF NOT EXISTS idx_wesentl_idv ON idv_wesentlichkeit(idv_db_id);

-- Angekreuzte Details je IDV (N:M zwischen IDV und Detail-Definitionen)
CREATE TABLE IF NOT EXISTS idv_wesentlichkeit_detail (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_db_id       INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
    detail_id       INTEGER NOT NULL REFERENCES wesentlichkeitskriterium_details(id) ON DELETE CASCADE,
    UNIQUE (idv_db_id, detail_id)
);

CREATE INDEX IF NOT EXISTS idx_wkd_idv ON idv_wesentlichkeit_detail(idv_db_id);

-- Beispiel-Kriterien (können im Admin-Bereich angepasst oder deaktiviert werden).
-- Pro Bezeichnung wird nur einmal eingefügt; bei Umbenennung im Admin bleibt der
-- Eintrag bestehen und wird bei späteren Starts nicht erneut erzeugt.
INSERT INTO wesentlichkeitskriterien
    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
SELECT 'Rechnungslegungs-Relevanz (GoB)',
       'Anwendung verarbeitet automatisierte Daten, die nach der Verarbeitung Eingang in die Buchführung finden, z. B. Generierung von Buchungsbelegen/ -listen, Import aus Schnittstellen etc. oder wenn anhand von Anwendungen Bilanznachweise (z. B. Berechnung von Rückstellungen) erstellt werden; allerdings nur, falls keine weiteren Nachweise vorhanden sind (s.a. IDW RS FAIT 1)',
       1, 1, 1
WHERE NOT EXISTS (SELECT 1 FROM wesentlichkeitskriterien
                  WHERE bezeichnung = 'Rechnungslegungs-Relevanz (GoB)');

INSERT INTO wesentlichkeitskriterien
    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
SELECT 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk',
       'Anwendung verarbeitet Daten, deren Ergebnisse für wesentliche geschäftspolitische Entscheidungen bzw. die Unternehmenssteuerung inklusive IKS-Maßnahmen zur Überwachung und Kontrolle der Geschäftstätigkeit herangezogen werden. Relevant sind dabei insbesondere Auswertungen, die zur Erfüllung von bankaufsichtsrechtlichen Anforderungen der MaRisk Verwendung finden. Hierzu zählen beispielsweise Risikoberichte und weitere Auswertungen/Anwendungen, deren Erstellung auf Grund der Regelungen bzw. zur Erfüllung der Anforderungen der MaRisk zwingend erforderlich sind.',
       1, 2, 1
WHERE NOT EXISTS (SELECT 1 FROM wesentlichkeitskriterien
                  WHERE bezeichnung = 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk');

INSERT INTO wesentlichkeitskriterien
    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
SELECT 'Kritische oder wichtige Funktionen',
       'Mindestens eine kritische oder wichtige Funktion ist vollständig von dem IKT-Asset/der IKT-Dienstleistung abhängig (=Abhängigkeitsgrad 4).',
       1, 3, 1
WHERE NOT EXISTS (SELECT 1 FROM wesentlichkeitskriterien
                  WHERE bezeichnung = 'Kritische oder wichtige Funktionen');

-- Beispiel-Details zu den o.g. Kriterien
INSERT OR IGNORE INTO wesentlichkeitskriterium_details (kriterium_id, bezeichnung, sort_order)
SELECT id, 'Generierung von Buchungsbelegen / -listen', 1
  FROM wesentlichkeitskriterien WHERE bezeichnung = 'Rechnungslegungs-Relevanz (GoB)';
INSERT OR IGNORE INTO wesentlichkeitskriterium_details (kriterium_id, bezeichnung, sort_order)
SELECT id, 'Import aus Schnittstellen', 2
  FROM wesentlichkeitskriterien WHERE bezeichnung = 'Rechnungslegungs-Relevanz (GoB)';
INSERT OR IGNORE INTO wesentlichkeitskriterium_details (kriterium_id, bezeichnung, sort_order)
SELECT id, 'Erstellung von Bilanznachweisen (z. B. Berechnung von Rückstellungen)', 3
  FROM wesentlichkeitskriterien WHERE bezeichnung = 'Rechnungslegungs-Relevanz (GoB)';

INSERT OR IGNORE INTO wesentlichkeitskriterium_details (kriterium_id, bezeichnung, sort_order)
SELECT id, 'Risikobericht / Risikoauswertung', 1
  FROM wesentlichkeitskriterien WHERE bezeichnung = 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk';
INSERT OR IGNORE INTO wesentlichkeitskriterium_details (kriterium_id, bezeichnung, sort_order)
SELECT id, 'Meldewesen / bankaufsichtsrechtliche Auswertung', 2
  FROM wesentlichkeitskriterien WHERE bezeichnung = 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk';
INSERT OR IGNORE INTO wesentlichkeitskriterium_details (kriterium_id, bezeichnung, sort_order)
SELECT id, 'Grundlage für geschäftspolitische Entscheidungen', 3
  FROM wesentlichkeitskriterien WHERE bezeichnung = 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk';

-- -----------------------------------------------------------------------------
-- 10. TEST- UND FREIGABEVERFAHREN (MaRisk AT 7.2 / BAIT / DORA)
-- Schrittfolge (Phase 1 → Phase 2 → Phase 3):
--   Phase 1: Fachlicher Test → Technischer Test
--   Phase 2: Fachliche Abnahme → Technische Abnahme
--   Phase 3: Archivierung Originaldatei (revisionssichere Ablage gem. MaRisk AT 7.2)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idv_freigaben (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id                  INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
    schritt                 TEXT NOT NULL,
    -- 'Fachlicher Test' | 'Technischer Test'
    -- | 'Fachliche Abnahme' | 'Technische Abnahme'
    -- | 'Archivierung Originaldatei'
    -- 'Ausstehend' | 'Erledigt' | 'Nicht erledigt' | 'Abgebrochen'
    status                  TEXT NOT NULL DEFAULT 'Ausstehend',
    -- Wer hat das Verfahren gestartet / diesen Schritt beauftragt
    beauftragt_von_id       INTEGER REFERENCES persons(id),
    beauftragt_am           TEXT NOT NULL DEFAULT (datetime('now','utc')),
    -- Wer soll diesen Schritt durchführen (Empfänger / Prüfer)
    zugewiesen_an_id        INTEGER REFERENCES persons(id),
    -- Wer hat den Schritt abgeschlossen
    durchgefuehrt_von_id    INTEGER REFERENCES persons(id),
    durchgefuehrt_am        TEXT,
    kommentar               TEXT,
    befunde                 TEXT,
    -- Nachweise (Textfeld + Datei-Upload)
    nachweise_text          TEXT,
    nachweis_datei_pfad     TEXT,       -- relativer Pfad zur hochgeladenen Datei
    nachweis_datei_name     TEXT,       -- Originaldateiname
    -- Archivierung Originaldatei (nur für Schritt 'Archivierung Originaldatei')
    -- NULL  = nicht anwendbar (Schritt ist kein Archiv-Schritt)
    -- 1     = Originaldatei wurde revisionssicher archiviert (Upload + SHA-256)
    -- 0     = Originaldatei nicht verfügbar (z.B. Cognos-Bericht in agree21Analysen);
    --         Begründung wird in `befunde` festgehalten
    datei_verfuegbar        INTEGER,
    archiv_datei_pfad       TEXT,       -- relativer Pfad im Archiv-Ordner
    archiv_datei_name       TEXT,       -- Originaldateiname der archivierten Datei
    archiv_datei_sha256     TEXT,       -- SHA-256-Hash zur Integritätssicherung
    -- Admin-Abbruch
    abgebrochen_von_id      INTEGER REFERENCES persons(id),
    abgebrochen_am          TEXT,
    abbruch_kommentar       TEXT,
    erstellt_am             TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_freigaben_idv    ON idv_freigaben(idv_id);
CREATE INDEX IF NOT EXISTS idx_freigaben_status ON idv_freigaben(status, schritt);

-- -----------------------------------------------------------------------------
-- 11. MEHRFACH-DATEI-VERKNÜPFUNGEN (IDV ↔ mehrere Scanner-Dateien)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idv_file_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_db_id   INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES idv_files(id),
    linked_at   TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    UNIQUE(idv_db_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_file_links_idv  ON idv_file_links(idv_db_id);
CREATE INDEX IF NOT EXISTS idx_file_links_file ON idv_file_links(file_id);

-- Performance-Index für Eingang-Ansicht (große Dateimengen)
CREATE INDEX IF NOT EXISTS idx_files_status_bearb
    ON idv_files(status, bearbeitungsstatus, has_macros, first_seen_at);

-- Prüffälligkeiten nächste 90 Tage
CREATE VIEW IF NOT EXISTS v_prueffaelligkeiten AS
SELECT
    r.idv_id,
    r.bezeichnung,
    r.status,
    r.naechste_pruefung,
    r.letzte_pruefung,
    r.pruefintervall_monate,
    p.nachname || ', ' || p.vorname AS fachverantwortlicher,
    ou.bezeichnung AS org_einheit,
    CASE
        WHEN r.naechste_pruefung < date('now') THEN 'ÜBERFÄLLIG'
        WHEN r.naechste_pruefung < date('now', '+30 days') THEN 'In 30 Tagen'
        WHEN r.naechste_pruefung < date('now', '+90 days') THEN 'In 90 Tagen'
    END AS faelligkeit
FROM idv_register r
LEFT JOIN persons p ON r.fachverantwortlicher_id = p.id
LEFT JOIN org_units ou ON r.org_unit_id = ou.id
WHERE r.naechste_pruefung < date('now', '+90 days')
  AND r.status NOT IN ('Archiviert', 'Abgekündigt')
ORDER BY r.naechste_pruefung ASC;

-- -----------------------------------------------------------------------------
-- 12. LDAP-KONFIGURATION & GRUPPEN-ROLLEN-MAPPING
-- -----------------------------------------------------------------------------

-- Genau ein Eintrag (id=1) – LDAP-Server-Konfiguration
CREATE TABLE IF NOT EXISTS ldap_config (
    id              INTEGER PRIMARY KEY DEFAULT 1 CHECK(id = 1),
    enabled         INTEGER NOT NULL DEFAULT 0,      -- 0 = deaktiviert, lokaler Login
    server_url      TEXT NOT NULL DEFAULT '',         -- ldaps://ldap.ihre-bank.de
    port            INTEGER NOT NULL DEFAULT 636,
    base_dn         TEXT NOT NULL DEFAULT '',         -- OU=Benutzer,DC=ihre-bank,DC=de
    bind_dn         TEXT NOT NULL DEFAULT '',         -- CN=svcacc,...
    bind_password   TEXT NOT NULL DEFAULT '',         -- Fernet-verschlüsselt
    user_attr       TEXT NOT NULL DEFAULT 'sAMAccountName',
    ssl_verify      INTEGER NOT NULL DEFAULT 1,       -- 1 = Zertifikat prüfen
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- Gruppen-DN → idvault-Rolle
CREATE TABLE IF NOT EXISTS ldap_group_role_mapping (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_dn    TEXT NOT NULL UNIQUE,    -- vollständiger DN der AD-Gruppe
    group_name  TEXT,                   -- Anzeigename (manuell oder aus LDAP)
    rolle       TEXT NOT NULL,          -- IDV-Administrator | IDV-Koordinator | ...
    sort_order  INTEGER NOT NULL DEFAULT 99
);

-- -----------------------------------------------------------------------------
-- 13. TESTDOKUMENTATION (MaRisk AT 7.2 / BAIT)
-- Fachliche Testfälle (mehrere je IDV) und Technischer Test (einer je IDV)
-- -----------------------------------------------------------------------------

-- Fachliche Testfälle: mehrere je IDV, mit fortlaufender Testfall-Nummer
CREATE TABLE IF NOT EXISTS fachliche_testfaelle (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id              INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
    testfall_nr         INTEGER NOT NULL,           -- fortlaufend je IDV (1, 2, 3 …)
    beschreibung        TEXT,                       -- Was wird getestet? (nullable: leerer Eintrag beim Phase-1-Start)
    parametrisierung    TEXT,                       -- Einstellungen / Konfigurationen
    testdaten           TEXT,                       -- Eingabedaten
    erwartetes_ergebnis TEXT,
    erzieltes_ergebnis  TEXT,
    bewertung           TEXT NOT NULL DEFAULT 'Offen',  -- 'Offen' | 'Erledigt'
    massnahmen          TEXT,                       -- Abgeleitete Maßnahmen (leer wenn erledigt)
    tester              TEXT,                       -- Name des Testers (Freitext)
    testdatum           TEXT,                       -- ISO 8601 Datum
    nachweis_datei_pfad TEXT,                       -- Relativer Pfad zur Nachweis-Datei
    nachweis_datei_name TEXT,                       -- Originaldateiname
    erstellt_am         TEXT NOT NULL DEFAULT (datetime('now','utc')),
    aktualisiert_am     TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (idv_id, testfall_nr)
);

CREATE INDEX IF NOT EXISTS idx_fachtestf_idv ON fachliche_testfaelle(idv_id);

-- Technischer Test: genau ein Eintrag je IDV (UNIQUE auf idv_id)
CREATE TABLE IF NOT EXISTS technischer_test (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_id              INTEGER NOT NULL REFERENCES idv_register(id) ON DELETE CASCADE,
    ergebnis            TEXT NOT NULL DEFAULT 'Offen',  -- 'Offen' | 'Erledigt' | 'Entfällt'
    kurzbeschreibung    TEXT,                       -- 1–2 Sätze, was technisch geprüft wurde
    pruefer             TEXT,                       -- Name des Prüfers (Freitext)
    pruefungsdatum      TEXT,                       -- ISO 8601 Datum
    nachweis_datei_pfad TEXT,                       -- Relativer Pfad zur Nachweis-Datei
    nachweis_datei_name TEXT,                       -- Originaldateiname
    erstellt_am         TEXT NOT NULL DEFAULT (datetime('now','utc')),
    aktualisiert_am     TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (idv_id)
);

-- -----------------------------------------------------------------------------
-- Cognos-Berichte (Berichtsübersicht-Import aus agree21Analysen)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cognos_berichte (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Import-Metadaten
    import_datei_name           TEXT,
    importiert_am               TEXT NOT NULL DEFAULT (datetime('now','utc')),
    importiert_von_id           INTEGER REFERENCES persons(id),
    -- Felder aus Berichtsübersicht (TSV)
    umfeld                      TEXT,
    bank_id                     TEXT,
    anwendung                   TEXT,
    berichtsname                TEXT NOT NULL,
    suchpfad                    TEXT,
    package                     TEXT,
    eigentuemer                 TEXT,
    berichtsbeschreibung        TEXT,
    erstelldatum                TEXT,
    aenderungsdatum             TEXT,
    letztes_ausfuehrungsdatum   TEXT,
    letzter_ausfuehrungsstatus  TEXT,
    anz_abfragen                INTEGER,
    anz_datenelemente           INTEGER,
    anz_felder_klarnamen        INTEGER,
    anz_filter                  INTEGER,
    summe_ausdruckslaenge       INTEGER,
    komplexitaet                REAL,
    datum_berichtsabzug         TEXT,
    -- Optionale Verknüpfungen
    idv_file_id                 INTEGER REFERENCES idv_files(id),
    idv_register_id             INTEGER REFERENCES idv_register(id),
    -- Status
    bearbeitungsstatus          TEXT NOT NULL DEFAULT 'Neu',
    UNIQUE(bank_id, berichtsname, suchpfad)
);

CREATE INDEX IF NOT EXISTS idx_cognos_berichte_anwendung ON cognos_berichte(anwendung);
CREATE INDEX IF NOT EXISTS idx_cognos_berichte_bank_id   ON cognos_berichte(bank_id);
CREATE INDEX IF NOT EXISTS idx_cognos_berichte_status    ON cognos_berichte(bearbeitungsstatus);

-- -----------------------------------------------------------------------------
-- 14. GLOSSAR-EINTRÄGE (konfigurierbar)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS glossar_eintraege (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    begriff      TEXT NOT NULL UNIQUE,
    entwickler   TEXT NOT NULL DEFAULT '',
    ort          TEXT NOT NULL DEFAULT '',
    fokus        TEXT NOT NULL DEFAULT '',
    beschreibung TEXT NOT NULL DEFAULT '',
    im_register  INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    aktiv        INTEGER NOT NULL DEFAULT 1
);

INSERT OR IGNORE INTO glossar_eintraege
    (begriff, entwickler, ort, fokus, beschreibung, im_register, sort_order)
VALUES
    ('Anwendungsentwicklung',
     'IT-Abt. / Extern',
     'Zentraler IT-Prozess',
     'Gesamter Lebenszyklus (SDLC)',
     'Oberbegriff für den gesamten Prozess: Anforderung, Konzept, Programmierung, Test, Rollout und Betrieb. Unterliegt MaRisk AT 7.2 (Trennprinzip) und DORA (Software-Entwicklungssicherheit).',
     0, 1),
    ('Eigenprogrammierung',
     'Interne IT',
     'Zentraler IT-Prozess',
     'Code-Qualität, Funktionstrennung',
     'Das Schreiben des Quellcodes durch internes Personal der IT-Abteilung. Schutzziele (Vertraulichkeit, Integrität, Verfügbarkeit) müssen je Eigenentwicklung nachweisbar sein.',
     1, 2),
    ('Auftragsprogrammierung',
     'Externer Dienstleister',
     'Extern',
     'Auslagerungsmanagement, DORA',
     'Externe Code-Erstellung im Rahmen des IKT-Drittparteien-Risikomanagements. Verantwortung verbleibt beim Institut – detaillierte Abnahme und Sicherheitsüberprüfung (Code-Reviews) sind verpflichtend.',
     1, 3),
    ('IDV (Individuelle Datenverarbeitung)',
     'Fachbereich',
     'Dezentral',
     'Schatten-IT vermeiden, Kontrollen',
     'Durch den Fachbereich entwickelte, wesentliche Anwendungen – z. B. komplexe Excel-Makros, Access-Datenbanken, SQL-Skripte. Unterliegt dem IDV-Rahmenwerk nach MaRisk AT 7.2 / BAIT (Dokumentation, Funktionstrennung, Freigabe).',
     1, 4),
    ('Arbeitshilfe',
     'Fachbereich',
     'Dezentral (End-User)',
     'Wesentlichkeitsprüfung',
     'Einfache Werkzeuge zur Unterstützung täglicher Aufgaben. Sobald eine Arbeitshilfe rechnungsrelevant wird, komplexe Logik enthält oder zur Risikosteuerung dient, wird sie über die Wesentlichkeitsprüfung zur IDV.',
     1, 5);
