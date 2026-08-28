#!/usr/bin/env python3
"""Debug what's actually in the embedded surrealkv database."""

from surrealdb import Surreal

DB_PATH = "/data/surreal_db"

db = Surreal(f"surrealkv://{DB_PATH}")

# Try root-level info before selecting namespace
try:
    r = db.query("INFO FOR ROOT")
    print("ROOT INFO:", r)
except Exception as e:  # noqa: BLE001 — throwaway diagnostic probe; the surrealdb SDK raises no common base type and each probe must print its own failure and let the next one run
    print("ROOT INFO error:", e)

# Try selecting namespace and see what databases exist
try:
    db.use("yadgar", "main")
    r = db.query("INFO FOR DB")
    print("DB INFO:", r)
except Exception as e:  # noqa: BLE001 — same probe-and-continue shape as above; a namespace/db that does not exist yet is a normal outcome here
    print("DB INFO error:", e)

# Count records
for table in ["memory", "action_log", "counter", "checkpoint"]:
    try:
        r = db.query(f"SELECT count() AS c FROM {table} GROUP ALL")
        print(f"  {table}: {r}")
    except Exception as e:  # noqa: BLE001 — per-table probe: a missing table is the expected failure and must not stop the remaining tables
        print(f"  {table}: ERROR {e}")
