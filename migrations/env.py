"""Alembic-Umgebung für idvscope.

Die Anwendung arbeitet mit raw ``sqlite3`` und pflegt keine SQLAlchemy-
Modelle; Alembic wird ausschließlich für die Schema-Verwaltung (Revisions,
Versionstabelle) eingesetzt. Migrationen führen ihre Änderungen über
``op.execute(...)`` oder direkt auf der darunterliegenden sqlite3-Connection
aus (siehe z. B. Initial-Revision).

Der DB-Pfad wird vom Aufrufer (``db._alembic_cfg``) per
``cfg.set_main_option("sqlalchemy.url", …)`` injiziert, sodass dieselbe
idvscope.db migriert wird, die auch die App anschließend öffnet.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# Keine SA-Modelle – autogenerate wird nicht genutzt.
target_metadata = None


def run_migrations_offline() -> None:
    """Offline-Modus: SQL-Skript statt Verbindung.

    Wird von ``alembic upgrade --sql`` verwendet; die App ruft stets den
    Online-Pfad (Verbindung) auf.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online-Modus: direkte Verbindung zur idvscope.db."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # ``engine.begin()`` (statt ``connect()``) commitet die Verbindung beim
    # Verlassen des Blocks – andernfalls würde SA 2.x die von Alembic
    # implizit begonnene Transaktion am Ende rollbacken und alle
    # Migrationsschritte gingen verloren.
    with connectable.begin() as connection:
        # PRAGMAs analog zu db_pragmas.apply_pragmas – bewusst minimal, damit
        # Migrationen nicht auf vom Writer gesetzte Extras angewiesen sind.
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.exec_driver_sql("PRAGMA busy_timeout = 60000")

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,          # SQLite: ALTER via Batch-Mode
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
