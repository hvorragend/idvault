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
-- Scanner-Tabellen (Stub – wird vom network_scanner.py befüllt;
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

-- Änderungsprotokoll pro Scanner-Fund (jedes Auftauchen/Ändern einer Datei)
CREATE TABLE IF NOT EXISTS idv_file_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER NOT NULL REFERENCES idv_files(id),
    scan_run_id     INTEGER NOT NULL,
    change_type     TEXT NOT NULL,  -- new | changed | unchanged | moved | restored | archiviert
    old_hash        TEXT,
    new_hash        TEXT,
    changed_at      TEXT NOT NULL,
    details         TEXT            -- JSON mit geänderten Feldern
);

CREATE INDEX IF NOT EXISTS idx_history_file ON idv_file_history(file_id);

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
    stellvertreter_id INTEGER REFERENCES persons(id), -- allg. Stellvertreter (MaRisk AT 7.2)
    abwesend_bis    TEXT,                             -- ISO-Date bis wann abwesend (z.B. "2026-05-01")
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
    ('notify_enabled_owner_digest',           '1'),
    ('self_service_enabled',                  '0'),
    -- Issue #351: Stille Freigabe (Selbstzertifizierung + Sicht-Freigabe)
    -- als verkuerztes Verfahren fuer nicht-wesentliche Eigenentwicklungen.
    -- Default aus (Opt-In pro Bank).
    ('silent_release_enabled',                '0'),
    ('self_service_frequency_days',           '7'),
    ('self_service_last_digest_date',         ''),
    ('auto_ignore_no_formula', '0'),
    -- Verschlankter Patch-Workflow (#320): JSON-Array mit den Schritten,
    -- die bei einer als 'patch' eingestuften Version durchlaufen werden.
    -- Default konservativ: Technischer Test + Fachliche Abnahme + Archivierung.
    ('freigabe_patch_schritte',
     '["Technischer Test","Fachliche Abnahme","Archivierung Originaldatei"]'),
    -- Keys, die seit 2026-04 aus der config.json in die DB gewandert sind:
    ('login_rate_limit',        '5 per minute;30 per hour'),
    ('upload_rate_limit',       '10 per minute;60 per hour'),
    ('allow_sidecar_updates',   '1'),
    ('path_mappings',           '[]'),
    ('scanner_config',          '{}'),
    ('teams_config',            '{}'),
    ('teams_client_secret_enc', ''),
    ('glossar_hintergrund_text',
     'Regulatorischer Hintergrund. Im Umfeld von MaRisk AT 7.2, BAIT und DORA bestimmt die Entwicklungsart, welche Kontrollen (Testpflichten, Dokumentation, Funktionstrennung, Auslagerungsmanagement) greifen. Der Übergang Arbeitshilfe → IDV erfolgt automatisch über die Wesentlichkeitsprüfung.'),
    ('glossar_info_unten',
     'Wann wird aus einer Arbeitshilfe eine IDV?

Eine einfache Arbeitshilfe – etwa eine Excel-Tabelle zur Formatierung – unterliegt nur geringen Kontrollen. Sobald die Tabelle jedoch rechnungsrelevant ist (HGB / GoBD), komplexe Berechnungslogik enthält, zur Risikosteuerung oder zur Meldeerstellung genutzt wird oder auf personenbezogene oder besonders sensible Daten zugreift, wird sie zur IDV und unterliegt dem vollständigen Rahmenwerk (Dokumentation, Test, Freigabe, Vier-Augen-Prinzip).

Die im Register hinterlegten Wesentlichkeitskriterien entscheiden automatisch: bei mindestens einem erfüllten aktiven Kriterium wechselt die Entwicklungsart auf IDV, andernfalls auf Arbeitshilfe. Manuell gesetzte Werte (Eigenprogrammierung, Auftragsprogrammierung) bleiben unverändert.');

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
    datenquellen            TEXT,                       -- Freitext: woher kommen die Daten?
    datenempfaenger         TEXT,                       -- Freitext: wohin gehen die Daten?

    -- -------------------------------------------------------------------------
    -- Datenschutz / Datenkategorien
    -- -------------------------------------------------------------------------
    datenschutz_kategorie   TEXT,                       -- "keine" | "allgemein" | "besonders sensibel"

    -- -------------------------------------------------------------------------
    -- Nutzung & Betrieb
    -- -------------------------------------------------------------------------
    nutzungsfrequenz        TEXT,                       -- "täglich" | "wöchentlich" | "monatlich" | "quartalsweise" | "anlassbezogen"
    nutzeranzahl            INTEGER,                    -- Anzahl aktiver Nutzer
    produktiv_seit          TEXT,                       -- Datum ISO 8601

    -- -------------------------------------------------------------------------
    -- Dokumentation & Qualitätssicherung
    -- -------------------------------------------------------------------------
    dokumentation_vorhanden INTEGER NOT NULL DEFAULT 0,
    dokumentation_pfad      TEXT,
    testkonzept_vorhanden   INTEGER NOT NULL DEFAULT 0,
    versionskontrolle       INTEGER NOT NULL DEFAULT 0, -- Git o.ä.
    anwenderdokumentation   INTEGER NOT NULL DEFAULT 0,
    datenschutz_beachtet    INTEGER NOT NULL DEFAULT 0,
    zellschutz_formeln      INTEGER NOT NULL DEFAULT 0,

    -- -------------------------------------------------------------------------
    -- Ablösung / Lebenszyklus
    -- -------------------------------------------------------------------------
    abloesung_geplant       INTEGER NOT NULL DEFAULT 0,
    abloesung_zieldatum     TEXT,
    abloesung_durch         TEXT,                       -- "OSPlus-Erweiterung", "Eigenentwicklung neu", etc.

    -- -------------------------------------------------------------------------
    -- Workflow / Freigabestatus
    -- -------------------------------------------------------------------------
    -- 'Entwurf'             Ersterfassung, noch nicht geprüft
    -- 'In Prüfung'          Liegt beim IDV-Koordinator / Fachverantwortlichen
    -- 'Freigegeben'         Test- und Freigabeverfahren erfolgreich abgeschlossen
    -- 'Freigegeben mit Auflagen'
    -- 'Abgelehnt'           Nicht als IDV eingestuft oder nicht freigabefähig
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
    erstellt_fuer               TEXT,
    schnittstellen_beschr       TEXT,
    teststatus                  TEXT NOT NULL DEFAULT 'Wertung ausstehend',
    vorgaenger_idv_id           INTEGER REFERENCES idv_register(id),
    -- Änderungsart bei neuer Version
    letzte_aenderungsart        TEXT,          -- 'wesentlich' | 'unwesentlich'
    letzte_aenderungsbegruendung TEXT,
    -- Umfang des aktuellen Freigabeverfahrens (#320)
    -- 'grundlegend' = voller 3-Phasen-Workflow (Default, Erstfreigabe)
    -- 'patch'       = verkürzter Workflow gemäß app_settings.freigabe_patch_schritte
    freigabe_aenderungskategorie  TEXT,
    -- Pflichtfeld bei 'patch': warum reicht ein Patch-Verfahren?
    freigabe_patch_begruendung    TEXT,
    -- Welches Freigabeverfahren wurde fuer die letzte Freigabe genutzt?
    -- 'Standard'         = regulaeres 3-Phasen-Verfahren
    -- 'Stille Freigabe'  = verkuerztes Verfahren (Issue #351, nur fuer
    --                      nicht-wesentliche IDVs, Opt-In via App-Setting)
    freigabe_verfahren            TEXT NOT NULL DEFAULT 'Standard'
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
    bearbeiter_name TEXT,           -- Klartextname (auch für Config-User ohne persons-Eintrag)
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
DROP VIEW IF EXISTS v_idv_uebersicht;
CREATE VIEW v_idv_uebersicht AS
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
LEFT JOIN geschaeftsprozesse   gp  ON r.gp_id = gp.id
LEFT JOIN org_units            ou  ON r.org_unit_id = ou.id
LEFT JOIN persons              p_fv ON r.fachverantwortlicher_id = p_fv.id
LEFT JOIN persons              p_en ON r.idv_entwickler_id = p_en.id
LEFT JOIN idv_files            f   ON r.file_id = f.id
;

-- Wesentliche IDVs (mindestens ein Wesentlichkeitskriterium erfüllt)
DROP VIEW IF EXISTS v_kritische_idvs;
CREATE VIEW v_kritische_idvs AS
SELECT * FROM v_idv_uebersicht
WHERE ist_wesentlich = 'Ja'
  AND status != 'Archiviert'
ORDER BY bezeichnung;

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
DROP VIEW IF EXISTS v_unvollstaendige_idvs;
CREATE VIEW v_unvollstaendige_idvs AS
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
    r.erstellt_am,
    r.aktualisiert_am
FROM idv_register r
WHERE r.status NOT IN ('Archiviert')
  AND (
    r.fachverantwortlicher_id IS NULL
    OR (r.gp_id IS NULL AND r.gp_freitext IS NULL)
    OR r.idv_typ = 'unklassifiziert'
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
       'Anwendung verarbeitet automatisierte Daten, die nach der Verarbeitung Eingang in die Buchführung finden, z. B. Generierung von Buchungsbelegen/ -listen, Import aus Schnittstellen etc. oder wenn anhand von Anwendungen Bilanznachweise (z. B. Berechnung von Rückstellungen) erstellt werden; allerdings nur, falls keine weiteren Nachweise vorhanden sind (s.a. IDW RS FAIT 1). Die Anwendung unterliegt den GoBD-Anforderungen (Buchführungspflicht, steuerrechtliche Aufbewahrungspflicht).',
       0, 1, 1
WHERE NOT EXISTS (SELECT 1 FROM wesentlichkeitskriterien
                  WHERE bezeichnung = 'Rechnungslegungs-Relevanz (GoB)');

INSERT INTO wesentlichkeitskriterien
    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
SELECT 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk',
       'Anwendung verarbeitet Daten, deren Ergebnisse für wesentliche geschäftspolitische Entscheidungen bzw. die Unternehmenssteuerung inklusive IKS-Maßnahmen zur Überwachung und Kontrolle der Geschäftstätigkeit herangezogen werden. Relevant sind dabei insbesondere Auswertungen, die zur Erfüllung von bankaufsichtsrechtlichen Anforderungen der MaRisk Verwendung finden. Hierzu zählen beispielsweise Risikoberichte und weitere Auswertungen/Anwendungen, deren Erstellung auf Grund der Regelungen bzw. zur Erfüllung der Anforderungen der MaRisk zwingend erforderlich sind.',
       0, 2, 1
WHERE NOT EXISTS (SELECT 1 FROM wesentlichkeitskriterien
                  WHERE bezeichnung = 'Risiko / Steuerungs-Relevanz im Sinne der MaRisk');

INSERT INTO wesentlichkeitskriterien
    (bezeichnung, beschreibung, begruendung_pflicht, sort_order, aktiv)
SELECT 'Kritische oder wichtige Funktionen',
       'Mindestens eine kritische oder wichtige Funktion ist vollständig von dem IKT-Asset/der IKT-Dienstleistung abhängig (=Abhängigkeitsgrad 4).',
       0, 3, 1
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
    -- Pool-Zuweisung (alternativ zu zugewiesen_an_id)
    pool_id                 INTEGER REFERENCES freigabe_pools(id),
    -- Wer hat den Schritt aus dem Pool übernommen (Claim)
    bearbeitet_von_id       INTEGER REFERENCES persons(id),
    bearbeitet_am           TEXT,
    -- Admin-Abbruch
    abgebrochen_von_id      INTEGER REFERENCES persons(id),
    abgebrochen_am          TEXT,
    abbruch_kommentar       TEXT,
    erstellt_am             TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_freigaben_idv    ON idv_freigaben(idv_id);
CREATE INDEX IF NOT EXISTS idx_freigaben_status ON idv_freigaben(status, schritt);
CREATE INDEX IF NOT EXISTS idx_freigaben_pool   ON idv_freigaben(pool_id);

-- -----------------------------------------------------------------------------
-- 11. FREIGABE-POOLS (Pool-basierte Zuweisung von Prüfschritten)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS freigabe_pools (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    beschreibung    TEXT,
    aktiv           INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE TABLE IF NOT EXISTS freigabe_pool_members (
    pool_id     INTEGER NOT NULL REFERENCES freigabe_pools(id)  ON DELETE CASCADE,
    person_id   INTEGER NOT NULL REFERENCES persons(id)          ON DELETE CASCADE,
    PRIMARY KEY (pool_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_pool_members_person ON freigabe_pool_members(person_id);

-- -----------------------------------------------------------------------------
-- 12. MEHRFACH-DATEI-VERKNÜPFUNGEN (IDV ↔ mehrere Scanner-Dateien)
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

-- -----------------------------------------------------------------------------
-- 12b. AKZEPTANZ „KEIN ZELL-/BLATTSCHUTZ"
-- -----------------------------------------------------------------------------
-- Aufsichtsrechtlich (MaRisk AT 7.2 / DORA) muss der Fachverantwortliche
-- bei der Fachlichen Abnahme bewusst bestätigen, dass eine Excel-Datei
-- ohne Blatt- oder Arbeitsmappenschutz produktiv geht. Pro IDV und Datei
-- wird genau eine Akzeptanz-Entscheidung festgehalten (mit optionaler
-- Begründung und Prüfprotokoll-Verweis auf die zugehörige Freigabe).

CREATE TABLE IF NOT EXISTS idv_zellschutz_akzeptanz (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    idv_db_id           INTEGER NOT NULL REFERENCES idv_register(id)  ON DELETE CASCADE,
    file_id             INTEGER NOT NULL REFERENCES idv_files(id)      ON DELETE CASCADE,
    freigabe_id         INTEGER          REFERENCES idv_freigaben(id)  ON DELETE SET NULL,
    akzeptiert_von_id   INTEGER          REFERENCES persons(id),
    akzeptiert_am       TEXT NOT NULL DEFAULT (datetime('now','utc')),
    begruendung         TEXT,
    UNIQUE(idv_db_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_zellschutz_akz_idv  ON idv_zellschutz_akzeptanz(idv_db_id);
CREATE INDEX IF NOT EXISTS idx_zellschutz_akz_file ON idv_zellschutz_akzeptanz(file_id);

-- Performance-Index für Eingang-Ansicht (große Dateimengen)
CREATE INDEX IF NOT EXISTS idx_files_status_bearb
    ON idv_files(status, bearbeitungsstatus, has_macros, first_seen_at);

-- Scan-Auswertungen nach Share/Owner (Bulk-Operationen, Funde-Filter)
CREATE INDEX IF NOT EXISTS idx_files_scan_metadata
    ON idv_files(share_root, file_owner);

-- Hash-basierte Dubletten-Erkennung (Auto-Gruppierung bei Registrierung)
CREATE INDEX IF NOT EXISTS idx_files_file_hash
    ON idv_files(file_hash);

-- Scan-Run-Archive nach Startzeit (Historie / Export)
CREATE INDEX IF NOT EXISTS idx_scan_runs_started_at
    ON scan_runs(started_at);

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

-- Prüfzeugnis der technischen Abnahme (Issue #349):
-- Persistiert ausschliesslich Abweichungen gegenueber dem maschinellen
-- Scanner-Befund. Eine fehlende Zeile bedeutet "maschinell bestaetigt,
-- ungeaendert"; eine Zeile mit manual_override=1 haelt die manuelle
-- Korrektur (Grund + Prueferin/Pruefer) fest. So bleibt im Audit-Trail
-- nachvollziehbar, welche Befunde der Prueferin/dem Pruefer als
-- Maschinenergebnis genuegt haben und wo manuell eingegriffen wurde.
CREATE TABLE IF NOT EXISTS tests_prefilled_findings (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id              INTEGER NOT NULL REFERENCES technischer_test(id) ON DELETE CASCADE,
    file_id              INTEGER NOT NULL REFERENCES idv_files(id)        ON DELETE CASCADE,
    check_kind           TEXT    NOT NULL,  -- siehe webapp/routes/tests.py::PRUEFZEUGNIS_CHECKS
    machine_result       TEXT,              -- Rohwert zum Zeitpunkt der Pruefung (Text/JSON)
    source_scan_run_id   INTEGER,           -- idv_files.last_scan_run_id beim Speichern
    manual_override      INTEGER NOT NULL DEFAULT 0,  -- 0 = akzeptiert, 1 = widerlegt
    manual_comment       TEXT,              -- Pflichtfeld bei manual_override=1
    confirmed_by_id      INTEGER REFERENCES persons(id),
    recorded_at          TEXT NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (test_id, file_id, check_kind)
);

CREATE INDEX IF NOT EXISTS idx_tests_prefilled_test
    ON tests_prefilled_findings(test_id);
CREATE INDEX IF NOT EXISTS idx_tests_prefilled_file
    ON tests_prefilled_findings(file_id);

-- Testfall-Vorlagen: wiederverwendbare Vorlage-Bibliothek je IDV-Typ
-- UNIQUE(titel, art) ermöglicht idempotente INSERT OR IGNORE-Seeds
CREATE TABLE IF NOT EXISTS testfall_vorlagen (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    titel                TEXT    NOT NULL,
    idv_typ              TEXT,                                   -- NULL = für alle Typen
    art                  TEXT    NOT NULL CHECK(art IN ('fachlich','technisch')),
    beschreibung         TEXT,
    parametrisierung     TEXT,
    testdaten            TEXT,
    erwartetes_ergebnis  TEXT,
    aktiv                INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    updated_at           TEXT,
    UNIQUE(titel, art)
);

CREATE INDEX IF NOT EXISTS idx_testfall_vorlagen_lookup
    ON testfall_vorlagen(aktiv, art, idv_typ);

-- Seed: regulatorisch konforme Vorlagen (MaRisk AT 7.2 Tz. 7 · BAIT Kap. 10 · GoBD · DORA)
INSERT OR IGNORE INTO testfall_vorlagen
    (titel, idv_typ, art, beschreibung, parametrisierung, testdaten, erwartetes_ergebnis)
VALUES
    (
        'Excel-Makro: Kernprüfung',
        'Excel-Makro',
        'fachlich',
        '<p><strong>Regulatorische Grundlage:</strong> MaRisk AT&nbsp;7.2 Tz.&nbsp;7 · BAIT Kap.&nbsp;10 · GoBD</p><p>Prüfung der Arbeitsmappe inkl. VBA-Makros auf korrekte Berechnung und regulatorische Konformität:</p><ul><li><strong>Versionsstand</strong>: Dateiversion und Änderungsdatum stimmen mit IDV-Register überein</li><li><strong>Makro-Signatur</strong>: Alle VBA-Module digital signiert; keine unsignierten Module vorhanden</li><li><strong>Externe Verknüpfungen</strong>: Alle Verknüpfungen dokumentiert und funktionsfähig (kein #REF!); Anzahl lt. Scanner-Import</li><li><strong>Formelintegrität</strong>: Ergebniszellen gegen Referenzmappe abgeglichen (Abweichungsanalyse je Blatt)</li><li><strong>Blattschutz</strong>: Ergebnisblätter gegen versehentliches Überschreiben geschützt</li><li><strong>GoBD-Konformität</strong>: Berechnungsweg nachvollziehbar; keine versteckten Zellen mit unkommentierter Logik</li><li><strong>Änderungsdokumentation</strong>: Änderungshistorie im Dokument oder IDV-Register vollständig vorhanden</li></ul>',
        '<p>Makros aktiv · Dateiversion lt. IDV-Register · Stichtag: <em>[Datum eintragen]</em></p><p>Scanner-Metadaten: Makros <em>(ja/nein)</em> · Blattschutz <em>(ja/nein)</em> · externe Verknüpfungen <em>(Anzahl)</em></p><p>CIA-Schutzbedarf lt. IDV-Register: Vertraulichkeit <em>[H/M/N]</em> · Integrität <em>[H/M/N]</em> · Verfügbarkeit <em>[H/M/N]</em></p>',
        '<p>Referenz-Mappe aus letztem freigegebenem Stand (Vorgängerversion lt. IDV-Register).</p><p>Produktionsdaten zum Stichtag · ggf. anonymisiert bei hoher Vertraulichkeitsstufe.</p>',
        '<p>Versionsstand stimmt mit IDV-Register überein · Ergebnisabweichung ≤ Rundungstoleranz · keine ungültigen Makro-Signaturen · keine undokumentierten externen Verknüpfungen · Blattschutz auf allen Ergebnisblättern aktiv · GoBD-konform nachvollziehbare Berechnung.</p>'
    ),
    (
        'Excel-Tabelle: Formel-Review',
        'Excel-Tabelle',
        'fachlich',
        '<p><strong>Regulatorische Grundlage:</strong> MaRisk AT&nbsp;7.2 Tz.&nbsp;7 · BAIT Kap.&nbsp;10 · GoBD · HGB §&nbsp;239</p><p>Review aller Formelzellen sowie Prüfung der Ordnungsmäßigkeit gemäß GoBD:</p><ul><li><strong>Formelzellen</strong>: Alle Formeln nachvollziehbar und gegen Referenzwerte validiert</li><li><strong>Quelldatenabgleich</strong>: Eingabedaten stimmen mit Quelldatensatz überein (Zeilenanzahl, Summen, Datumsbereiche)</li><li><strong>Vollständigkeit</strong>: Datenbasis vollständig übernommen – kein implizites Zeilen-Limit, kein ungewolltes Abschneiden</li><li><strong>Blattschutz</strong>: Ergebnisblätter gegen versehentliche Änderung geschützt</li><li><strong>Benannte Bereiche</strong>: Alle named ranges dokumentiert und korrekt referenziert</li><li><strong>Externe Verknüpfungen</strong>: Keine unbegründeten Verknüpfungen zu externen Dateien</li><li><strong>GoBD-Unveränderlichkeit</strong>: Keine überschreibbaren Zwischenergebnisse ohne Schutzmaßnahme (HGB §&nbsp;239)</li></ul>',
        '<p>Keine Makros · Blattschutz aktiv · Stichtag: <em>[Datum eintragen]</em></p><p>CIA-Schutzbedarf lt. IDV-Register: Vertraulichkeit <em>[H/M/N]</em> · Integrität <em>[H/M/N]</em> · Verfügbarkeit <em>[H/M/N]</em></p>',
        '<p>Stichprobe aus dem produktiven Einsatzdatensatz · Referenzwerte aus validiertem Vorsystem.</p><p>Bei Rechnungslegungsrelevanz: vollständige Datenbasis (keine Stichprobe).</p>',
        '<p>Alle Formelzellen nachvollziehbar · Quelldaten vollständig übernommen (Abweichung 0) · Blattschutz auf allen Ergebnisblättern aktiv · keine undokumentierten externen Verknüpfungen · GoBD-konforme Nachvollziehbarkeit gegeben.</p>'
    ),
    (
        'Access-Datenbank: Abfragen & Berichte',
        'Access-Datenbank',
        'fachlich',
        '<p>Prüfung der Kernabfragen, Berichte und Datenverbindungen.</p><ul><li>Korrektheit der Abfrageergebnisse gegen Referenz</li><li>Verknüpfungen zu Fremdtabellen (ODBC / CSV)</li><li>Berechtigungen auf Datenquelle</li></ul>',
        '<p>Standardverbindung, lesende Rechte.</p>',
        '<p>Produktionsdaten zum Stichtag.</p>',
        '<p>Alle Kernabfragen liefern erwartete Zeilenanzahl und Summe.</p>'
    ),
    (
        'SQL-Skript: Ausführungsprüfung',
        'SQL-Skript',
        'fachlich',
        '<p>Fachliche Plausibilität der Abfrageergebnisse.</p><ul><li>Kein implizites Row-Count-Limit</li><li>Zeitraum-Parameter korrekt gesetzt</li><li>Ergebnis gegen Referenz-Abfrage abgeglichen</li></ul>',
        '<p>Parametrisiert auf Stichtag; lesender DB-Zugriff.</p>',
        '<p>Produktions-DB, Lesezugriff auf relevante Schemata.</p>',
        '<p>Ergebnismenge stimmt mit Referenzabfrage überein (Abweichung 0).</p>'
    ),
    (
        'Python-Skript: End-to-End-Lauf',
        'Python-Skript',
        'fachlich',
        '<p>End-to-End-Ausführung des Skripts mit Referenzinput.</p><ul><li>Abhängigkeiten dokumentiert (requirements.txt)</li><li>Deterministisches Ergebnis</li><li>Logging / Fehlerpfade geprüft</li></ul>',
        '<p>Python ≥ 3.10, requirements erfüllt.</p>',
        '<p>Referenzdatei aus letztem Stichtag.</p>',
        '<p>Ergebnisdatei identisch zur Referenz (Hash-Match).</p>'
    ),
    (
        'Power-BI-Bericht: KPI-Abgleich',
        'Power-BI-Bericht',
        'fachlich',
        '<p>KPI-Abgleich gegen Referenzsystem.</p><ul><li>Datenquelle und Refresh-Logik</li><li>Zentrale KPIs (Summen, Durchschnitte)</li><li>Filterwirkung prüfen</li></ul>',
        '<p>Veröffentlichte Version; Datenquelle wie Produktion.</p>',
        '<p>Referenz-Dashboard zum Stichtag.</p>',
        '<p>Zentrale KPIs stimmen mit Referenz überein; Abweichung ≤ Toleranz.</p>'
    ),
    (
        'Cognos-Report: Fachliche Vollständigkeitsprüfung',
        'Cognos-Report',
        'fachlich',
        '<p><strong>Regulatorische Grundlage:</strong> MaRisk AT&nbsp;7.2 Tz.&nbsp;7 · BAIT Kap.&nbsp;10</p><p>Fachliche Vollständigkeitsprüfung des Cognos-Berichts gegen Referenzlauf und Quelldaten:</p><ul><li><strong>Berichtskennzahlen</strong>: Alle wesentlichen KPIs (Summen, Anzahlen, Durchschnitte) gegen Referenz-Export abgeglichen</li><li><strong>Datenvollständigkeit</strong>: Zeilenanzahl und Datumsbereiche stimmen mit Quelldaten überein; kein implizites Zeilen-Limit aktiv</li><li><strong>Filterparameter</strong>: Alle Filter und Prompts korrekt gesetzt und vollständig dokumentiert</li><li><strong>Abfragelogik</strong>: Anzahl Abfragen, Datenelemente und Filter lt. Cognos-Import auf Plausibilität geprüft</li><li><strong>Ausführungsprotokoll</strong>: Kein Fehler oder Warnung im Cognos-Ausführungslog</li><li><strong>Berechtigungen</strong>: Zugriff nur für autorisierte Rollen; keine unberechtigten Zugriffe im Log nachweisbar</li><li><strong>Quelldaten-Abgleich</strong>: Ergebnisse mit Quelldaten aus dem Package (agree21 Analytics) plausibilisiert</li><li><strong>Ausführungsstatus</strong>: Letzter Ausführungsstatus lt. Cognos-Import: Erfolgreich</li></ul>',
        '<p>Freigegebener Parameter-Satz lt. IDV-Register · Berichtsversion und Suchpfad lt. Cognos-Import (agree21 Analytics)</p><p>Stichtag / Berichtszeitraum: <em>[Datum eintragen]</em></p><p>Anzahl Abfragen: <em>[aus Cognos-Import]</em> · Anzahl Datenelemente: <em>[aus Cognos-Import]</em> · Anzahl Filter: <em>[aus Cognos-Import]</em></p><p>CIA-Schutzbedarf lt. IDV-Register: Vertraulichkeit <em>[H/M/N]</em> · Integrität <em>[H/M/N]</em> · Verfügbarkeit <em>[H/M/N]</em></p>',
        '<p>Referenz-Export aus letztem freigegebenem Lauf (Vorgängerversion lt. IDV-Register).</p><p>Quelldaten aus dem zugrunde liegenden Datenpaket (Package) zum Stichtag.</p><p>Cognos-Ausführungsprotokoll (Log-Export) · Benutzerberechtigungsnachweis (Cognos-Rollenliste).</p>',
        '<p>Alle Berichtskennzahlen stimmen mit Referenz überein (Abweichung ≤ Toleranz) · Ausführungsprotokoll ohne Fehler/Warnungen · Filterparameter vollständig und korrekt · Zugriff nur für autorisierte Rollen · Datenbasis vollständig (Zeilenanzahl und Summen plausibel gegen Quelldaten).</p>'
    ),
    -- Technische Vorlagen (nur Beschreibung — parametrisierung/testdaten/ergebnis leer)
    (
        'Excel: Technische Prüfung (BAIT/DORA)',
        NULL,
        'technisch',
        '<p><strong>Regulatorische Grundlage:</strong> BAIT Kap.&nbsp;4 · BAIT Kap.&nbsp;10 · DORA Art.&nbsp;6</p><p><strong>Technische Prüfpunkte:</strong></p><ul><li><strong>Makro-Signatur</strong>: Alle VBA-Module digital signiert; keine unsignierten Module</li><li><strong>Externe Verknüpfungen</strong>: Alle Verknüpfungen dokumentiert; keine gebrochenen Links</li><li><strong>Blattschutz</strong>: Ergebnisblätter auf allen relevanten Tabellenblättern aktiv</li><li><strong>Formelschutz</strong>: Formelzellen gegen versehentliches Überschreiben geschützt</li><li><strong>Keine Hardcoded-Pfade</strong>: Alle Dateipfade parametrisiert oder relativ</li><li><strong>Berechtigungskonzept</strong>: Dateizugriff auf autorisierte Rollen beschränkt (lt. Schutzbedarf)</li><li><strong>CIA-Schutzbedarf</strong>: Technische Maßnahmen zu Vertraulichkeit, Integrität und Verfügbarkeit lt. IDV-Register dokumentiert</li><li><strong>Versionskontrolle</strong>: Änderungshistorie nachvollziehbar (Datei-Eigenschaften oder IDV-Register)</li><li><strong>Wiederherstellbarkeit</strong>: Backup-Verfahren dokumentiert; Wiederherstellung verifiziert</li><li><strong>DORA-Kritikalität</strong>: Bei GDA&nbsp;≥&nbsp;3 oder DORA-kritisch/wichtig – erweiterte Abhängigkeitsanalyse und Notfallplan dokumentiert</li></ul>',
        '', '', ''
    ),
    (
        'Skript: technische Basisprüfung',
        NULL,
        'technisch',
        '<p><strong>Technische Prüfpunkte:</strong></p><ul><li>Versionierung / Git vorhanden</li><li>Abhängigkeiten gepinnt</li><li>Keine Zugangsdaten im Code</li><li>Logging auf stdout / Datei</li><li>Fehlerpfade abgedeckt</li></ul>',
        '', '', ''
    ),
    (
        'Cognos-Report: Technische Basisprüfung',
        'Cognos-Report',
        'technisch',
        '<p><strong>Regulatorische Grundlage:</strong> BAIT Kap.&nbsp;4 (Berechtigungen) · BAIT Kap.&nbsp;10 (IDV) · DORA Art.&nbsp;6</p><p><strong>Technische Prüfpunkte:</strong></p><ul><li><strong>Berechtigungskonzept</strong>: Cognos-Rollen und -Gruppen dokumentiert; nur autorisierte Nutzer haben Lesezugriff auf Report und zugrunde liegendes Package</li><li><strong>Datenquellensicherheit</strong>: Verbindungsparameter ohne Klartext-Zugangsdaten; Zugriff über dediziertes Service-Konto</li><li><strong>Datenleitweg (Data Lineage)</strong>: Alle verwendeten Datenquellen (Package, Schema, Tabellen) vollständig dokumentiert</li><li><strong>Abfrageperformanz</strong>: Ausführungsdauer innerhalb definierter SLA; keine Resource-Limit-Verstöße im Ausführungslog</li><li><strong>Ausführungsplanung</strong>: Scheduling (falls vorhanden) dokumentiert; kein unkontrollierter Ad-hoc-Betrieb bei kritischen IDVs</li><li><strong>Datenqualität</strong>: Keine systematischen NULL-Werte oder Duplikate in definierten Schlüsselfeldern</li><li><strong>Versionierung</strong>: Berichtsversion und Suchpfad synchron mit IDV-Register; Änderungen über dokumentierten Change-Management-Prozess</li><li><strong>DORA-Kritikalität</strong>: Bei GDA&nbsp;≥&nbsp;3 oder DORA-kritisch/wichtig – erweiterte Prüfung der Abhängigkeiten, Ausfallszenarien und Wiederherstellbarkeit dokumentiert</li></ul>',
        '', '', ''
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

-- 15. NOTIFICATION_LOG (Dedup für tägliche Fristen-Benachrichtigungen)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notification_log (
    kind      TEXT NOT NULL,       -- 'massnahme_ueberfaellig' | 'pruefung_faellig'
    ref_id    INTEGER NOT NULL,    -- id der Maßnahme bzw. IDV
    sent_date TEXT NOT NULL,       -- ISO-Datum des Versands (YYYY-MM-DD)
    PRIMARY KEY (kind, ref_id, sent_date)
);

CREATE INDEX IF NOT EXISTS idx_notif_log_sent_date ON notification_log(sent_date);

-- -----------------------------------------------------------------------------
-- 16. SELF-SERVICE (Owner-Mail-Digest + Magic-Link, Issue #315)
-- -----------------------------------------------------------------------------

-- Einmalige Magic-Links aus Owner-Digest-Mails (HMAC-signiert via itsdangerous).
-- Wir speichern nur den jti, nicht den Token selbst.
CREATE TABLE IF NOT EXISTS self_service_tokens (
    jti            TEXT    PRIMARY KEY,
    person_id      INTEGER NOT NULL REFERENCES persons(id),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    expires_at     TEXT    NOT NULL,
    first_used_at  TEXT,
    revoked_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_self_service_tokens_person
    ON self_service_tokens(person_id);
CREATE INDEX IF NOT EXISTS idx_self_service_tokens_expires
    ON self_service_tokens(expires_at);

-- Audit-Trail für Self-Service-Aktionen (Quelle "mail-link").
CREATE TABLE IF NOT EXISTS self_service_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER REFERENCES persons(id),
    file_id     INTEGER REFERENCES idv_files(id),
    aktion      TEXT NOT NULL,     -- 'ignoriert' | 'zur_registrierung'
    quelle      TEXT NOT NULL DEFAULT 'mail-link',
    jti         TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

CREATE INDEX IF NOT EXISTS idx_self_service_audit_person
    ON self_service_audit(person_id, created_at);
CREATE INDEX IF NOT EXISTS idx_self_service_audit_file
    ON self_service_audit(file_id);

-- -----------------------------------------------------------------------------
-- 17. IDV-DRAFT (temporärer Entwurf beim Anlegen neuer IDV-Einträge)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS idv_draft (
    user_id     TEXT PRIMARY KEY,
    draft_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','utc')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

-- -----------------------------------------------------------------------------
-- 18. PFAD-PROFILE (Vorbelegung für Bulk-Registrierung von Scan-Funden, #314)
-- -----------------------------------------------------------------------------
-- Verknüpft einen Pfad-Präfix (z. B. "\\srv\share\Abteilung_Kredit\") mit
-- Default-Kopfdaten (OE, Fachverantwortlicher, Koordinator, Entwicklungsart,
-- Prüfintervall). Beim Öffnen der Bulk-Registrierung wird das am besten
-- passende Profil gezogen (längstes aktives Präfix gewinnt;
-- siehe webapp/routes/eigenentwicklung.py::_best_fund_pfad_profil).

CREATE TABLE IF NOT EXISTS fund_pfad_profile (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    pfad_praefix             TEXT    NOT NULL UNIQUE,
    org_unit_id              INTEGER REFERENCES org_units(id),
    fachverantwortlicher_id  INTEGER REFERENCES persons(id),
    idv_koordinator_id       INTEGER REFERENCES persons(id),
    entwicklungsart          TEXT,
    pruefintervall_monate    INTEGER,
    bemerkung                TEXT,
    aktiv                    INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    created_by_id            INTEGER REFERENCES persons(id),
    updated_at               TEXT
);

CREATE INDEX IF NOT EXISTS idx_fund_pfad_profile_aktiv
    ON fund_pfad_profile(aktiv);
