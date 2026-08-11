"""Database setup and session management."""

from pathlib import Path
from sqlalchemy import MetaData, event, text
from sqlalchemy.schema import CreateIndex, CreateTable
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


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        """Tune every pooled SQLite connection for concurrent web traffic."""
        cursor = dbapi_connection.cursor()
        try:
            # WAL lets readers continue while player/status updates are written.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA cache_size=-20000")  # 20 MiB per connection
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def create_tables():
    """Create all database tables and run lightweight migrations.

    SQLite cannot relax a NOT NULL constraint in place.  Existing databases
    therefore need one table rebuild for Instant-only locations. Foreign-key
    checks are disabled only around that single transaction and are verified
    immediately afterwards.
    """
    # Database helpers and migration scripts may call this without importing
    # the web application first. Import the model package here so every
    # Declarative model is registered on Base.metadata before create_all.
    import app.models  # noqa: F401

    if settings.database_url.startswith("sqlite"):
        async with engine.connect() as conn:
            await conn.execute(text("PRAGMA foreign_keys=OFF"))
            await conn.commit()
            try:
                async with conn.begin():
                    await _create_and_migrate(conn)
            finally:
                await conn.execute(text("PRAGMA foreign_keys=ON"))
                await conn.commit()

            violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
            if violations:
                raise RuntimeError(f"Database migration left foreign-key violations: {violations}")
        return

    async with engine.begin() as conn:
        await _create_and_migrate(conn)


async def _create_and_migrate(conn) -> None:
    """Run create_all and all additive/rebuild migrations in one transaction."""
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
    await _migrate_add_column(conn, "reservations", "config_file", "VARCHAR(64)")
    await _migrate_add_column(conn, "reservations", "enable_logs_tf_upload", "BOOLEAN DEFAULT 1")
    await _migrate_add_column(conn, "reservations", "enable_demos_tf_upload", "BOOLEAN DEFAULT 1")
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
    # Runtime-neutral reservation connection fields. All historical
    # reservations used the cloud runtime.
    await _migrate_add_column(
        conn, "reservations", "runtime_kind", "VARCHAR(16) NOT NULL DEFAULT 'cloud'"
    )
    await _migrate_add_column(conn, "reservations", "direct_ip", "VARCHAR(15)")
    await _migrate_add_column(conn, "reservations", "direct_port", "INTEGER")
    await _migrate_add_column(conn, "reservations", "direct_tv_port", "INTEGER")
    await conn.execute(text(
        "UPDATE reservations SET runtime_kind = 'cloud' "
        "WHERE runtime_kind IS NULL OR runtime_kind = ''"
    ))

    await _migrate_add_column(conn, "enabled_locations", "ping_url", "VARCHAR(512)")
    await _migrate_add_column(
        conn, "instant_hosts", "update_auto_drained", "BOOLEAN NOT NULL DEFAULT 0"
    )
    await _migrate_enabled_locations_nullable(conn)
    await _migrate_instant_hosts_public_ipv4_nullable(conn)
    await _backfill_motd_tokens(conn)
    await _migrate_create_indexes(conn)


# Tables and columns that are allowed in migrations (prevents SQL injection)
_ALLOWED_TABLES = frozenset({
    "game_maps", "users", "reservations", "cloud_instances",
    "enabled_locations", "providers", "site_settings", "ping_submissions",
    "steam_trust_snapshots", "upload_links", "location_providers",
    "trivia_facts",
    "instant_hosts", "instant_slots", "instant_assignments",
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


async def _migrate_create_indexes(conn) -> None:
    """Create indexes used by rate-limit and historical-stat queries."""
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_reservations_created_at ON reservations (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_user_created_at ON reservations (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_status_created_at ON reservations (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reservations_runtime_kind ON reservations (runtime_kind)",
    ):
        await conn.execute(text(statement))


async def _migrate_enabled_locations_nullable(conn) -> None:
    """Relax legacy cloud mapping columns for Instant-only locations.

    This is intentionally a data-preserving SQLite table rebuild. IDs (the
    location codes) and every existing value are copied verbatim. It runs in
    the surrounding startup transaction, so an interrupted migration leaves
    the original schema intact.
    """
    if not settings.database_url.startswith("sqlite"):
        return

    info = (await conn.execute(text("PRAGMA table_info(enabled_locations)"))).all()
    by_name = {row[1]: row for row in info}
    if not by_name:
        return
    if not by_name.get("provider", (None,) * 4)[3] and not by_name.get(
        "provider_region", (None,) * 4
    )[3]:
        return

    await conn.execute(text("DROP TABLE IF EXISTS enabled_locations__instant_migration"))
    await conn.execute(text("""
        CREATE TABLE enabled_locations__instant_migration (
            code VARCHAR(32) NOT NULL PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            provider VARCHAR(32) REFERENCES providers (code),
            provider_region VARCHAR(32),
            vultr_region VARCHAR(32),
            billing_model VARCHAR(32),
            city VARCHAR(64),
            country VARCHAR(64),
            continent VARCHAR(32),
            subdivision VARCHAR(64),
            recommended BOOLEAN,
            enabled BOOLEAN,
            display_order INTEGER,
            instance_plan VARCHAR(32),
            region_instance_limit INTEGER,
            ping_url VARCHAR(512)
        )
    """))

    ordered_columns = [
        "code", "name", "provider", "provider_region", "vultr_region",
        "billing_model", "city", "country", "continent", "subdivision",
        "recommended", "enabled", "display_order", "instance_plan",
        "region_instance_limit", "ping_url",
    ]
    columns = [column for column in ordered_columns if column in by_name]
    joined = ", ".join(columns)
    await conn.execute(text(
        f"INSERT INTO enabled_locations__instant_migration ({joined}) "
        f"SELECT {joined} FROM enabled_locations"
    ))
    await conn.execute(text("DROP TABLE enabled_locations"))
    await conn.execute(text(
        "ALTER TABLE enabled_locations__instant_migration RENAME TO enabled_locations"
    ))


async def _migrate_instant_hosts_public_ipv4_nullable(conn) -> None:
    """Allow pending Instant hosts to learn their IPv4 during enrollment."""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        await conn.execute(text(
            "ALTER TABLE instant_hosts ALTER COLUMN public_ipv4 DROP NOT NULL"
        ))
        return
    if dialect != "sqlite":
        return

    info = (await conn.execute(text("PRAGMA table_info(instant_hosts)"))).all()
    by_name = {row[1]: row for row in info}
    if not by_name or not by_name.get("public_ipv4", (None,) * 4)[3]:
        return

    from app.models.instance import EnabledLocation
    from app.models.instant import InstantHost

    temporary_name = "instant_hosts__ipv4_migration"
    await conn.execute(text(f"DROP TABLE IF EXISTS {temporary_name}"))
    migration_metadata = MetaData()
    # Include the referenced table in the temporary metadata so SQLAlchemy can
    # compile the foreign key without creating or modifying that table.
    EnabledLocation.__table__.to_metadata(migration_metadata)
    migration_table = InstantHost.__table__.to_metadata(
        migration_metadata, name=temporary_name
    )
    create_sql = str(CreateTable(migration_table).compile(dialect=conn.dialect))
    await conn.execute(text(create_sql))

    target_columns = [column.name for column in InstantHost.__table__.columns]
    copied_columns = [column for column in target_columns if column in by_name]
    joined = ", ".join(copied_columns)
    await conn.execute(text(
        f"INSERT INTO {temporary_name} ({joined}) SELECT {joined} FROM instant_hosts"
    ))
    await conn.execute(text("DROP TABLE instant_hosts"))
    await conn.execute(text(
        f"ALTER TABLE {temporary_name} RENAME TO instant_hosts"
    ))
    for index in InstantHost.__table__.indexes:
        create_index_sql = str(CreateIndex(index).compile(dialect=conn.dialect))
        await conn.execute(text(create_index_sql))


async def get_db() -> AsyncSession:
    """Dependency for getting database sessions."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()
