"""self_service: Owner-Mail-Digest + Magic-Link-Tokens für Scanner-Funde (#315)

Revision ID: 0008_self_service
Revises: 0007_freigabe_claim
Create Date: 2026-04-23
"""

from __future__ import annotations

from alembic import op


revision = "0008_self_service"
down_revision = "0007_freigabe_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Magic-Link-Tokens für Self-Service-Mails: einmaliger Eintritt,
    # 7-Tage-TTL, nach erstem Klick markiert. Signatur-Teil (HMAC) trägt
    # die itsdangerous-Bibliothek — wir tracken nur den jti, damit wir
    # den Token revoken können.
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS self_service_tokens (
            jti            TEXT    PRIMARY KEY,
            person_id      INTEGER NOT NULL REFERENCES persons(id),
            created_at     TEXT    NOT NULL DEFAULT (datetime('now','utc')),
            expires_at     TEXT    NOT NULL,
            first_used_at  TEXT,
            revoked_at     TEXT
        )
    """)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_self_service_tokens_person "
        "ON self_service_tokens(person_id)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_self_service_tokens_expires "
        "ON self_service_tokens(expires_at)"
    )

    # Audit-Log für Self-Service-Aktionen. Pro Aktion ein Eintrag mit
    # Quelle 'mail-link' bzw. künftigen Varianten.
    bind.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS self_service_audit (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id   INTEGER REFERENCES persons(id),
            file_id     INTEGER REFERENCES idv_files(id),
            aktion      TEXT NOT NULL,   -- 'ignoriert' | 'zur_registrierung'
            quelle      TEXT NOT NULL DEFAULT 'mail-link',
            jti         TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_self_service_audit_person "
        "ON self_service_audit(person_id, created_at)"
    )
    bind.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_self_service_audit_file "
        "ON self_service_audit(file_id)"
    )

    # Default-Settings: Self-Service aus, Digest-Intervall 7 Tage.
    for key, value in (
        ("self_service_enabled",         "0"),
        ("self_service_frequency_days",  "7"),
        ("self_service_last_digest_date", ""),
        ("notify_enabled_owner_digest",  "1"),
    ):
        bind.exec_driver_sql(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    # notification_log nimmt die Digest-Kinds bereits auf (dynamisches
    # kind-Feld); kein Schema-Change nötig.


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_self_service_audit_file")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_self_service_audit_person")
    bind.exec_driver_sql("DROP TABLE IF EXISTS self_service_audit")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_self_service_tokens_expires")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_self_service_tokens_person")
    bind.exec_driver_sql("DROP TABLE IF EXISTS self_service_tokens")
    for key in (
        "self_service_enabled",
        "self_service_frequency_days",
        "self_service_last_digest_date",
        "notify_enabled_owner_digest",
    ):
        bind.exec_driver_sql(
            "DELETE FROM app_settings WHERE key = ?",
            (key,),
        )
