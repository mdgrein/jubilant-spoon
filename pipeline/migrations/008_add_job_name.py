"""
Migration 008: Add 'name' and 'chain_id' columns to the jobs table.

Unlike migration 007, this migration only adds new columns (no constraint changes),
so simple ALTER TABLE ADD COLUMN statements are sufficient.
"""

import sqlite3
from pathlib import Path


def run(conn: sqlite3.Connection) -> None:
    # Check if columns already exist (idempotent)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    if "name" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN name TEXT NOT NULL DEFAULT ''")

    if "chain_id" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN chain_id TEXT")

    conn.commit()
    print("Migration 008 applied: 'name' and 'chain_id' columns added to jobs.")


if __name__ == "__main__":
    db_path = Path(__file__).parent.parent.parent / "clowder.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    run(conn)
    conn.close()
