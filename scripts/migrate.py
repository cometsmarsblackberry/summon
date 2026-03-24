#!/usr/bin/env python3
"""Export and import data between Summon installations.

Uses only Python standard library (sqlite3) — no pip dependencies needed.

Usage:
    python3 migrate.py export [--db path/to/reserve.db] [--out output_dir]
    python3 migrate.py import [--db path/to/reserve.db] <input_file>

If --db is omitted, looks for data/reserve.db relative to this script's
parent directory (the project root).
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Data category definitions
# ---------------------------------------------------------------------------

CATEGORIES = {
    "bans": "Banned users (steam_id, display_name, ban_reason)",
    "admins": "Admin users (steam_id, display_name)",
    "locations": "Server locations (code, name, city, country, provider config)",
    "providers": "Cloud providers (code, name, billing, instance plan)",
    "location_providers": "Location-provider mappings (failover priority)",
    "maps": "Game maps (name, display_name, enabled, default)",
    "settings": "Site settings (key-value overrides)",
    "monthly_costs": "Monthly cost history (hours, USD, EUR, reservations)",
    "trivia": "Trivia facts for MOTD pages (scope, key, fact)",
}

CATEGORY_ORDER = list(CATEGORIES.keys())


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _find_db(explicit_path: str | None) -> str:
    """Resolve the SQLite database path."""
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            print(f"Error: Database not found at {p}")
            sys.exit(1)
        return str(p)

    # Auto-detect: look relative to the script's parent dir (project root)
    root = Path(__file__).resolve().parent.parent
    candidates = [
        root / "data" / "reserve.db",
        Path("/data/reserve.db"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    print("Error: Could not find reserve.db. Use --db to specify the path.")
    sys.exit(1)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a table has a specific column."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def _prompt_categories(action: str, restrict_to: list[str] | None = None) -> list[str]:
    """Ask the user which data categories to export/import."""
    items = restrict_to if restrict_to else CATEGORY_ORDER

    print(f"\nAvailable data categories to {action}:\n")
    for i, key in enumerate(items, 1):
        desc = CATEGORIES.get(key, "")
        print(f"  [{i}] {key:20s} - {desc}")
    print(f"  [a] Select all")
    print()

    while True:
        raw = input(f"Enter numbers to {action} (comma-separated, or 'a' for all): ").strip()
        if not raw:
            continue
        if raw.lower() == "a":
            return list(items)

        selected = []
        valid = True
        for part in raw.split(","):
            part = part.strip()
            if not part.isdigit():
                print(f"  Invalid input: '{part}' — enter numbers or 'a'.")
                valid = False
                break
            idx = int(part) - 1
            if idx < 0 or idx >= len(items):
                print(f"  Invalid number: {part} — must be 1-{len(items)}.")
                valid = False
                break
            cat = items[idx]
            if cat not in selected:
                selected.append(cat)
        if valid and selected:
            return selected


def _prompt_conflict_mode() -> str:
    """Ask the user how to handle conflicts during import."""
    print("\nHow should conflicts (existing records) be handled?\n")
    print("  [1] skip    - Keep existing data, skip conflicts")
    print("  [2] update  - Overwrite existing data with imported values")
    print()
    while True:
        choice = input("Enter 1 or 2 [default: 1 skip]: ").strip()
        if choice in ("", "1"):
            return "skip"
        if choice == "2":
            return "update"
        print("  Please enter 1 or 2.")


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def _export_bans(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "users"):
        return []
    cur = conn.execute(
        "SELECT steam_id, display_name, ban_reason "
        "FROM users WHERE is_banned = 1 ORDER BY steam_id"
    )
    return [dict(row) for row in cur.fetchall()]


def _export_admins(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "users"):
        return []
    cur = conn.execute(
        "SELECT steam_id, display_name "
        "FROM users WHERE is_admin = 1 ORDER BY steam_id"
    )
    return [dict(row) for row in cur.fetchall()]


def _export_locations(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "enabled_locations"):
        return []
    cols = [
        "code", "name", "provider", "provider_region",
        "vultr_region", "billing_model",
        "city", "country", "continent", "subdivision",
        "recommended", "enabled", "display_order",
        "instance_plan", "region_instance_limit",
    ]
    # Only select columns that exist (handles older schemas)
    existing = [c for c in cols if _table_has_column(conn, "enabled_locations", c)]
    cur = conn.execute(
        f"SELECT {', '.join(existing)} FROM enabled_locations ORDER BY display_order, code"
    )
    return [dict(row) for row in cur.fetchall()]


def _export_providers(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "providers"):
        return []
    cols = [
        "code", "name", "billing_model", "instance_plan",
        "container_image", "instance_limit", "enabled", "display_order",
    ]
    existing = [c for c in cols if _table_has_column(conn, "providers", c)]
    cur = conn.execute(
        f"SELECT {', '.join(existing)} FROM providers ORDER BY display_order, code"
    )
    return [dict(row) for row in cur.fetchall()]


def _export_location_providers(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "location_providers"):
        return []
    cols = [
        "location_code", "provider_code", "provider_region",
        "priority", "enabled", "instance_plan", "region_instance_limit",
    ]
    existing = [c for c in cols if _table_has_column(conn, "location_providers", c)]
    cur = conn.execute(
        f"SELECT {', '.join(existing)} FROM location_providers "
        "ORDER BY location_code, priority"
    )
    return [dict(row) for row in cur.fetchall()]


def _export_maps(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "game_maps"):
        return []
    cols = ["name", "display_name", "enabled", "is_default", "display_order"]
    existing = [c for c in cols if _table_has_column(conn, "game_maps", c)]
    cur = conn.execute(
        f"SELECT {', '.join(existing)} FROM game_maps ORDER BY display_order, name"
    )
    return [dict(row) for row in cur.fetchall()]


def _export_settings(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "site_settings"):
        return []
    cur = conn.execute("SELECT key, value FROM site_settings ORDER BY key")
    return [dict(row) for row in cur.fetchall()]


def _export_monthly_costs(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "monthly_costs"):
        return []
    cur = conn.execute(
        "SELECT year_month, total_hours, total_cost_usd, total_cost_eur, "
        "reservation_count FROM monthly_costs ORDER BY year_month"
    )
    rows = []
    for row in cur.fetchall():
        d = dict(row)
        # Ensure numeric types serialize cleanly
        d["total_cost_usd"] = str(d.get("total_cost_usd", "0"))
        d["total_cost_eur"] = str(d.get("total_cost_eur", "0"))
        rows.append(d)
    return rows



# ---------------------------------------------------------------------------
# Import functions
# ---------------------------------------------------------------------------

def _import_bans(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT id, is_banned FROM users WHERE steam_id = ?",
            (rec["steam_id"],),
        )
        existing = cur.fetchone()
        if existing:
            if existing["is_banned"]:
                skipped += 1
            elif mode == "update":
                conn.execute(
                    "UPDATE users SET is_banned = 1, ban_reason = ? WHERE id = ?",
                    (rec.get("ban_reason"), existing["id"]),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO users (steam_id, display_name, avatar_url, is_banned, ban_reason, is_admin, reservation_count) "
                "VALUES (?, ?, '', 1, ?, 0, 0)",
                (rec["steam_id"], rec.get("display_name", "Unknown"), rec.get("ban_reason")),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_admins(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT id, is_admin FROM users WHERE steam_id = ?",
            (rec["steam_id"],),
        )
        existing = cur.fetchone()
        if existing:
            if existing["is_admin"]:
                skipped += 1
            elif mode == "update":
                conn.execute(
                    "UPDATE users SET is_admin = 1 WHERE id = ?",
                    (existing["id"],),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO users (steam_id, display_name, avatar_url, is_admin, is_banned, reservation_count) "
                "VALUES (?, ?, '', 1, 0, 0)",
                (rec["steam_id"], rec.get("display_name", "Unknown")),
            )

            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_locations(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT code FROM enabled_locations WHERE code = ?", (rec["code"],)
        )
        if cur.fetchone():
            if mode == "update":
                conn.execute(
                    "UPDATE enabled_locations SET name=?, provider=?, provider_region=?, "
                    "vultr_region=?, billing_model=?, "
                    "city=?, country=?, continent=?, subdivision=?, "
                    "recommended=?, enabled=?, "
                    "display_order=?, instance_plan=?, region_instance_limit=? "
                    "WHERE code=?",
                    (
                        rec["name"], rec.get("provider", "vultr"),
                        rec.get("provider_region", ""),
                        rec.get("vultr_region"), rec.get("billing_model", "hourly"),
                        rec.get("city"), rec.get("country"), rec.get("continent"),
                        rec.get("subdivision"),
                        rec.get("recommended", 0), rec.get("enabled", 1),
                        rec.get("display_order", 0), rec.get("instance_plan"),
                        rec.get("region_instance_limit"), rec["code"],
                    ),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO enabled_locations "
                "(code, name, provider, provider_region, vultr_region, billing_model, "
                "city, country, continent, subdivision, "
                "recommended, enabled, display_order, instance_plan, region_instance_limit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec["code"], rec["name"], rec.get("provider", "vultr"),
                    rec.get("provider_region", ""),
                    rec.get("vultr_region"), rec.get("billing_model", "hourly"),
                    rec.get("city"), rec.get("country"), rec.get("continent"),
                    rec.get("subdivision"),
                    rec.get("recommended", 0), rec.get("enabled", 1),
                    rec.get("display_order", 0), rec.get("instance_plan"),
                    rec.get("region_instance_limit"),
                ),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_providers(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT code FROM providers WHERE code = ?", (rec["code"],)
        )
        if cur.fetchone():
            if mode == "update":
                conn.execute(
                    "UPDATE providers SET name=?, billing_model=?, instance_plan=?, "
                    "container_image=?, instance_limit=?, enabled=?, display_order=? "
                    "WHERE code=?",
                    (
                        rec["name"], rec.get("billing_model", "hourly"),
                        rec.get("instance_plan", "vhf-1c-1gb"),
                        rec.get("container_image", ""),
                        rec.get("instance_limit", 10),
                        rec.get("enabled", 1), rec.get("display_order", 0),
                        rec["code"],
                    ),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO providers "
                "(code, name, billing_model, instance_plan, container_image, "
                "instance_limit, enabled, display_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec["code"], rec["name"],
                    rec.get("billing_model", "hourly"),
                    rec.get("instance_plan", "vhf-1c-1gb"),
                    rec.get("container_image", ""),
                    rec.get("instance_limit", 10),
                    rec.get("enabled", 1), rec.get("display_order", 0),
                ),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_location_providers(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT id FROM location_providers WHERE location_code = ? AND provider_code = ?",
            (rec["location_code"], rec["provider_code"]),
        )
        existing = cur.fetchone()
        if existing:
            if mode == "update":
                conn.execute(
                    "UPDATE location_providers SET provider_region=?, priority=?, "
                    "enabled=?, instance_plan=?, region_instance_limit=? WHERE id=?",
                    (
                        rec["provider_region"], rec.get("priority", 0),
                        rec.get("enabled", 1), rec.get("instance_plan"),
                        rec.get("region_instance_limit"), existing["id"],
                    ),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO location_providers "
                "(location_code, provider_code, provider_region, priority, "
                "enabled, instance_plan, region_instance_limit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rec["location_code"], rec["provider_code"],
                    rec["provider_region"], rec.get("priority", 0),
                    rec.get("enabled", 1), rec.get("instance_plan"),
                    rec.get("region_instance_limit"),
                ),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_maps(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT id FROM game_maps WHERE name = ?", (rec["name"],)
        )
        existing = cur.fetchone()
        if existing:
            if mode == "update":
                conn.execute(
                    "UPDATE game_maps SET display_name=?, enabled=?, is_default=?, "
                    "display_order=? WHERE id=?",
                    (
                        rec.get("display_name", rec["name"]),
                        rec.get("enabled", 1), rec.get("is_default", 0),
                        rec.get("display_order", 0), existing["id"],
                    ),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO game_maps (name, display_name, enabled, is_default, display_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    rec["name"], rec.get("display_name", rec["name"]),
                    rec.get("enabled", 1), rec.get("is_default", 0),
                    rec.get("display_order", 0),
                ),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_settings(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT key FROM site_settings WHERE key = ?", (rec["key"],)
        )
        if cur.fetchone():
            if mode == "update":
                conn.execute(
                    "UPDATE site_settings SET value = ? WHERE key = ?",
                    (rec["value"], rec["key"]),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO site_settings (key, value) VALUES (?, ?)",
                (rec["key"], rec["value"]),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _import_monthly_costs(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        cur = conn.execute(
            "SELECT year_month FROM monthly_costs WHERE year_month = ?",
            (rec["year_month"],),
        )
        if cur.fetchone():
            if mode == "update":
                conn.execute(
                    "UPDATE monthly_costs SET total_hours=?, total_cost_usd=?, "
                    "total_cost_eur=?, reservation_count=? WHERE year_month=?",
                    (
                        rec.get("total_hours", 0), rec.get("total_cost_usd", "0"),
                        rec.get("total_cost_eur", "0"), rec.get("reservation_count", 0),
                        rec["year_month"],
                    ),
                )
                updated += 1
            else:
                skipped += 1
        else:
            conn.execute(
                "INSERT INTO monthly_costs "
                "(year_month, total_hours, total_cost_usd, total_cost_eur, reservation_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    rec["year_month"], rec.get("total_hours", 0),
                    rec.get("total_cost_usd", "0"), rec.get("total_cost_eur", "0"),
                    rec.get("reservation_count", 0),
                ),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


def _export_trivia(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "trivia_facts"):
        return []
    cur = conn.execute(
        "SELECT scope, key, fact FROM trivia_facts ORDER BY scope, key, id"
    )
    return [dict(row) for row in cur.fetchall()]


def _import_trivia(conn: sqlite3.Connection, records: list[dict], mode: str) -> dict:
    created = updated = skipped = 0
    for rec in records:
        # Check for exact duplicate (same scope + key + fact)
        cur = conn.execute(
            "SELECT id FROM trivia_facts WHERE scope = ? AND key = ? AND fact = ?",
            (rec["scope"], rec.get("key", ""), rec["fact"]),
        )
        if cur.fetchone():
            skipped += 1
        else:
            conn.execute(
                "INSERT INTO trivia_facts (scope, key, fact) VALUES (?, ?, ?)",
                (rec["scope"], rec.get("key", ""), rec["fact"]),
            )
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}


EXPORTERS = {
    "bans": _export_bans,
    "admins": _export_admins,
    "locations": _export_locations,
    "providers": _export_providers,
    "location_providers": _export_location_providers,
    "maps": _export_maps,
    "settings": _export_settings,
    "monthly_costs": _export_monthly_costs,
    "trivia": _export_trivia,
}

IMPORTERS = {
    "bans": _import_bans,
    "admins": _import_admins,
    "locations": _import_locations,
    "providers": _import_providers,
    "location_providers": _import_location_providers,
    "maps": _import_maps,
    "settings": _import_settings,
    "monthly_costs": _import_monthly_costs,
    "trivia": _import_trivia,
}


# ---------------------------------------------------------------------------
# Main export / import flows
# ---------------------------------------------------------------------------

def do_export(db_path: str, output_dir: str) -> None:
    conn = _connect(db_path)
    print(f"Database: {db_path}")

    categories = _prompt_categories("export")

    data: dict = {
        "_meta": {
            "exported_at": datetime.now(tz=None).isoformat(),
            "categories": categories,
        }
    }

    for cat in categories:
        print(f"  Exporting {cat}...", end=" ", flush=True)
        records = EXPORTERS[cat](conn)
        data[cat] = records
        print(f"{len(records)} records")

    conn.close()

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=None).strftime("%Y%m%d_%H%M%S")
    filename = output_path / f"summon_export_{timestamp}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\nExport saved to: {filename}")


def do_import(db_path: str, input_file: str) -> None:
    path = Path(input_file)
    if not path.exists():
        print(f"Error: File not found: {input_file}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    meta = data.get("_meta", {})
    available = [cat for cat in CATEGORY_ORDER if cat in data and data[cat]]

    if not available:
        print("No data categories found in this file.")
        sys.exit(1)

    print(f"Database: {db_path}")
    print(f"File:     {path.name}")
    if meta.get("exported_at"):
        print(f"Exported: {meta['exported_at']}")
    print(f"\nCategories in file:")
    for cat in available:
        print(f"  - {cat}: {len(data[cat])} records")

    selected = _prompt_categories("import", restrict_to=available)
    mode = _prompt_conflict_mode()

    conn = _connect(db_path)

    print()
    for cat in selected:
        print(f"  Importing {cat}...", end=" ", flush=True)
        stats = IMPORTERS[cat](conn, data[cat], mode)
        parts = []
        if stats["created"]:
            parts.append(f"{stats['created']} created")
        if stats["updated"]:
            parts.append(f"{stats['updated']} updated")
        if stats["skipped"]:
            parts.append(f"{stats['skipped']} skipped")
        print(", ".join(parts) if parts else "nothing to do")

    conn.commit()
    conn.close()
    print("\nImport complete.")


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> tuple:
    """Parse command-line arguments. Returns (action, db_path, extra_path)."""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _print_usage()
        sys.exit(0)

    action = args[0]
    if action not in ("export", "import"):
        _print_usage()
        sys.exit(1)

    db_path = None
    out_dir = "."
    input_file = None

    i = 1
    while i < len(args):
        if args[i] == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_dir = args[i + 1]
            i += 2
        elif not args[i].startswith("-"):
            if action == "import":
                input_file = args[i]
            elif action == "export":
                out_dir = args[i]
            i += 1
        else:
            print(f"Unknown option: {args[i]}")
            sys.exit(1)

    resolved_db = _find_db(db_path)

    if action == "import" and not input_file:
        print("Error: Please provide the path to the export file.")
        print("Usage: python3 migrate.py import <input_file>")
        sys.exit(1)

    return action, resolved_db, out_dir if action == "export" else input_file


def _print_usage():
    print("Summon Data Migration Tool")
    print()
    print("Usage:")
    print("  python3 migrate.py export [--db reserve.db] [--out output_dir]")
    print("  python3 migrate.py import [--db reserve.db] <input_file>")
    print()
    print("Options:")
    print("  --db PATH   Path to reserve.db (auto-detects data/reserve.db if omitted)")
    print("  --out DIR   Output directory for export (default: current directory)")
    print()
    print("Examples:")
    print("  python3 migrate.py export")
    print("  python3 migrate.py export --db /data/reserve.db --out /tmp")
    print("  python3 migrate.py import summon_export_20260316_120000.json")
    print("  python3 migrate.py import --db /opt/summon/data/reserve.db export.json")


def main():
    action, db_path, extra = _parse_args()

    if action == "export":
        do_export(db_path, extra)
    else:
        do_import(db_path, extra)


if __name__ == "__main__":
    main()
