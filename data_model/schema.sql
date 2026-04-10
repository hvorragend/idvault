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
-- Scanner-Tabelle (Stub – wird vom idv_scanner.py befüllt;
-- kann in derselben oder einer separaten DB liegen.
-- Hier als Minimalstruktur für FK-Integrität)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS idv_files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash           TEXT NOT NULL,
    full_path           TEXT NOT NULL,
    file_name           TEXT NOT NULL,
    extension           TEXT NOT NULL,
    share_root          TEXT,
    relative_path       TEXT,
    size_bytes          INTEGER,
    created_at          TEXT,
    modified_at         TEXT,
    file_owner          TEXT,
    office_author       TEXT,
    office_last_author  TEXT,
    office_created      TEXT,
    office_modified     TEXT,
    has_macros          INTEGER DEFAULT 0,
    has_external_links  INTEGER DEFAULT 0,
    sheet_count         INTEGER,
    named_ranges_count  INTEGER,
    first_seen_at       TEXT NOT NULL DEFAULT (datetime('now','utc')),
    last_seen_at        TEXT NOT NULL DEFAULT (datetime('now','utc')),
    last_scan_run_id    INTEGER,
    status              TEXT DEFAULT 'active',
    UNIQUE(full_path)
);

-- -----------------------------------------------------------------------------
-- 0. STAMMDATEN / LOOKUP-TABELLEN
-- -----------------------------------------------------------------------------

-- Organisationseinheiten (Fachbereiche / Abteilungen)
CREATE TABLE IF NOT EXISTS org_units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kuerzel     TEXT NOT NULL UNIQUE,               -- z.B. "FIL", "KRE", "VWL"
    bezeichnung TEXT NOT NULL,                      -- z.B. "Filialvertrieb"
    ebene       TEXT,                               -- "Vorstand" | "Bereich" | "Abteilung"
    parent_id   INTEGER REFERENCES org_units(id),  -- Hierarchie
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
    created_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
);

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

-- Standard-Rollen
INSERT OR IGNORE INTO org_units (kuerzel, bezeichnung) VALUES
    ('UNBEK', '(unbekannt / nicht zugeordnet)');

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
    -- Klassifizierung IDV-Typ
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
    -- Steuerungsrelevanz (MaRisk AT 7.2 / BAIT Tz. 52)
    -- -------------------------------------------------------------------------
    steuerungsrelevant      INTEGER NOT NULL DEFAULT 0, -- 0=nein, 1=ja
    steuerungsrelevanz_begr TEXT,                       -- Begründung (Pflicht wenn =1)
    -- Dimensionen der Steuerungsrelevanz
    relevant_guv            INTEGER NOT NULL DEFAULT 0, -- GuV-Relevanz
    relevant_meldewesen     INTEGER NOT NULL DEFAULT 0, -- Meldewesen/Regulatorik
    relevant_risikomanagement INTEGER NOT NULL DEFAULT 0, -- Risikomanagement

    -- -------------------------------------------------------------------------
    -- Rechnungslegungsrelevanz
    -- -------------------------------------------------------------------------
    rechnungslegungsrelevant       INTEGER NOT NULL DEFAULT 0,
    rechnungslegungsrelevanz_begr  TEXT,

    -- -------------------------------------------------------------------------
    -- GDA – Grad der Abhängigkeit (BAIT / eigene Definition)
    -- -------------------------------------------------------------------------
    -- 1 = Unterstützend     (Prozess läuft auch ohne IDV, mit Mehraufwand)
    -- 2 = Relevant          (Prozessunterstützung, Alternativprozess vorhanden)
    -- 3 = Wesentlich        (Kernprozessunterstützung, kein vollständiger Ersatz)
    -- 4 = Vollständig       (Prozess ohne IDV nicht durchführbar)
    gda_wert                INTEGER NOT NULL DEFAULT 1 CHECK(gda_wert BETWEEN 1 AND 4),
    gda_begruendung         TEXT,

    -- -------------------------------------------------------------------------
    -- Geschäftsprozess-Zuordnung
    -- -------------------------------------------------------------------------
    gp_id                   INTEGER REFERENCES geschaeftsprozesse(id),
    gp_freitext             TEXT,                       -- Falls GP noch nicht im Katalog

    -- -------------------------------------------------------------------------
    -- DORA-Klassifizierung (Art. 28/30 DORA)
    -- -------------------------------------------------------------------------
    dora_kritisch_wichtig   INTEGER NOT NULL DEFAULT 0, -- i.S.v. DORA Art. 28 Abs. 2
    dora_begruendung        TEXT,

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
    tags                    TEXT                         -- JSON-Array: ["Jahresabschluss","Meldewesen"]
);

-- Indizes IDV-Register
CREATE INDEX IF NOT EXISTS idx_idv_status      ON idv_register(status);
CREATE INDEX IF NOT EXISTS idx_idv_gda         ON idv_register(gda_wert);
CREATE INDEX IF NOT EXISTS idx_idv_steuerung   ON idv_register(steuerungsrelevant);
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

-- Vollständige IDV-Übersicht mit allen wichtigen Fehlern
CREATE VIEW IF NOT EXISTS v_idv_uebersicht AS
SELECT
    r.idv_id,
    r.bezeichnung,
    r.idv_typ,
    r.status,
    r.gda_wert,
    CASE r.gda_wert
        WHEN 1 THEN 'Unterstützend'
        WHEN 2 THEN 'Relevant'
        WHEN 3 THEN 'Wesentlich'
        WHEN 4 THEN 'Vollständig abhängig'
    END                         AS gda_bezeichnung,
    CASE r.steuerungsrelevant WHEN 1 THEN 'Ja' ELSE 'Nein' END  AS steuerungsrelevant,
    CASE r.rechnungslegungsrelevant WHEN 1 THEN 'Ja' ELSE 'Nein' END AS rl_relevant,
    CASE r.dora_kritisch_wichtig WHEN 1 THEN 'Ja' ELSE 'Nein' END AS dora_kritisch,
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

-- Kritische IDVs (GDA=4 ODER steuerungsrelevant ODER DORA-kritisch)
CREATE VIEW IF NOT EXISTS v_kritische_idvs AS
SELECT * FROM v_idv_uebersicht
WHERE gda_wert = 4
   OR steuerungsrelevant = 'Ja'
   OR dora_kritisch = 'Ja'
ORDER BY gda_wert DESC, risikoklasse;

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
    CASE WHEN r.gda_begruendung IS NULL OR r.gda_begruendung = '' THEN 1 ELSE 0 END AS fehlt_gda_begruendung,
    CASE WHEN r.steuerungsrelevant = 1 AND (r.steuerungsrelevanz_begr IS NULL OR r.steuerungsrelevanz_begr = '') THEN 1 ELSE 0 END AS fehlt_steuerungsbegruendung,
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
    OR (r.steuerungsrelevant = 1 AND (r.steuerungsrelevanz_begr IS NULL OR r.steuerungsrelevanz_begr = ''))
  );

-- Prüffälligkeiten nächste 90 Tage
CREATE VIEW IF NOT EXISTS v_prueffaelligkeiten AS
SELECT
    r.idv_id,
    r.bezeichnung,
    r.gda_wert,
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
