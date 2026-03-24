"""Database setup and session management."""

import os
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


settings = get_settings()

# Ensure data directory exists for SQLite
if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///")[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    echo=False,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def create_tables():
    """Create all database tables and run lightweight migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns that create_all won't add to existing tables
        await _migrate_add_column(conn, "game_maps", "is_default", "BOOLEAN DEFAULT 0")
        await _migrate_add_column(conn, "enabled_locations", "region_instance_limit", "INTEGER")
        await _migrate_add_column(conn, "enabled_locations", "subdivision", "VARCHAR(16)")
        # Steam trust fields on users
        await _migrate_add_column(conn, "users", "steam_account_created_at", "DATETIME")
        await _migrate_add_column(conn, "users", "tf2_playtime_hours", "INTEGER")
        await _migrate_add_column(conn, "users", "owns_tf2", "BOOLEAN")
        await _migrate_add_column(conn, "users", "has_vac_ban", "BOOLEAN")
        await _migrate_add_column(conn, "users", "profile_public", "BOOLEAN")
        await _migrate_add_column(conn, "users", "steam_data_updated_at", "DATETIME")
        await _migrate_add_column(conn, "reservations", "provision_attempts", "INTEGER DEFAULT 0")
        await _migrate_add_column(conn, "reservations", "enable_direct_connect", "BOOLEAN DEFAULT 0")
        await _migrate_add_column(conn, "users", "ban_reason", "VARCHAR(255)")
        await _migrate_add_column(conn, "reservations", "plugin_api_key", "VARCHAR(64) DEFAULT ''")
        await _migrate_add_column(conn, "users", "deleted_at", "DATETIME")
        # Provider priority: track which provider created each instance
        await _migrate_add_column(conn, "cloud_instances", "provider_code", "VARCHAR(32)")
        await _migrate_add_column(conn, "cloud_instances", "provider_region", "VARCHAR(32)")
        # Track actual end time for accurate daily hours calculation
        await _migrate_add_column(conn, "reservations", "ended_at", "DATETIME")
        # MOTD access token (unguessable URL)
        await _migrate_add_column(conn, "reservations", "motd_token", "VARCHAR(64) DEFAULT ''")
        await _backfill_motd_tokens(conn)


# Tables and columns that are allowed in migrations (prevents SQL injection)
_ALLOWED_TABLES = frozenset({
    "game_maps", "users", "reservations", "cloud_instances",
    "enabled_locations", "providers", "site_settings", "ping_submissions",
    "steam_trust_snapshots", "upload_links", "location_providers",
    "trivia_facts",
})


async def _migrate_add_column(conn, table: str, column: str, column_def: str):
    """Add a column to an existing table if it doesn't exist (SQLite-safe)."""
    import re
    from sqlalchemy import text

    # Validate inputs against allowlist to prevent SQL injection
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Table '{table}' not in migration allowlist")
    if not re.match(r'^[a-z_][a-z0-9_]*$', column):
        raise ValueError(f"Invalid column name: '{column}'")
    if not re.match(r"^[A-Za-z0-9_ ()']+$", column_def):
        raise ValueError(f"Invalid column definition: '{column_def}'")

    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    columns = [row[1] for row in result]
    if column not in columns:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}"))



async def _backfill_motd_tokens(conn):
    """Generate motd_token for any reservations that don't have one."""
    import secrets
    from sqlalchemy import text

    rows = await conn.execute(
        text("SELECT id FROM reservations WHERE motd_token = '' OR motd_token IS NULL")
    )
    for (row_id,) in rows:
        token = secrets.token_urlsafe(32)
        await conn.execute(
            text("UPDATE reservations SET motd_token = :token WHERE id = :id"),
            {"token": token, "id": row_id},
        )


async def get_db() -> AsyncSession:
    """Dependency for getting database sessions."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
