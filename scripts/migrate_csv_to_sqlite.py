"""
One-time migration: copy existing CSV data files into the SQLite database.

Usage (from project root):
    python scripts/migrate_csv_to_sqlite.py

Safe to run on a fresh install with no CSVs — it skips missing files.
Safe to re-run — INSERT OR IGNORE prevents duplicates.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import coll_store


def main():
    db = coll_store._db_path()
    print(f"Database: {db}")

    existed = db.exists()
    coll_store.init_db()
    if not existed:
        print("Created new database.")

    print("Migrating CSV data...")
    coll_store._migrate_csv_to_db()

    conn = coll_store.get_db()
    try:
        for table in ("users", "beats", "vouchers", "installments",
                      "completed_vouchers", "completed_installments"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<28} {count} rows")
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
