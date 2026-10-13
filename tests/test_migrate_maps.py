import sqlite3
import unittest

from scripts.migrate import _export_maps, _import_maps


def _create_maps_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE game_maps ("
        "id INTEGER PRIMARY KEY, name VARCHAR(64) UNIQUE NOT NULL, "
        "display_name VARCHAR(64) NOT NULL, enabled BOOLEAN, is_default BOOLEAN, "
        "in_league_rotation BOOLEAN DEFAULT 0, display_order INTEGER)"
    )


class MigrateMapsTests(unittest.TestCase):
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        _create_maps_table(conn)
        return conn

    def test_league_rotation_flag_round_trips_through_export_and_import(self):
        source = self._connect()
        source.execute(
            "INSERT INTO game_maps "
            "(name, display_name, enabled, is_default, in_league_rotation, display_order) "
            "VALUES ('cp_process_f12', 'cp_process_f12', 1, 0, 1, 1)"
        )
        records = _export_maps(source)

        target = self._connect()
        _import_maps(target, records, "skip")

        row = target.execute(
            "SELECT in_league_rotation FROM game_maps WHERE name = 'cp_process_f12'"
        ).fetchone()
        self.assertEqual(1, records[0]["in_league_rotation"])
        self.assertEqual(1, row["in_league_rotation"])


if __name__ == "__main__":
    unittest.main()
