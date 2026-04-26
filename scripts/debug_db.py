#!/usr/bin/env python3
"""Debug what's actually in the embedded surrealkv database."""

from surrealdb import Surreal

DB_PATH = "/data/surreal_db"

db = Surreal(f"surrealkv://{DB_PATH}")

# Try root-level info before selecting namespace
try:
    r = db.query("INFO FOR ROOT")
    print("ROOT INFO:", r)
except Exception as e:
    print("ROOT INFO error:", e)

# Try selecting namespace and see what databases exist
try:
    db.use("yadgar", "main")
    r = db.query("INFO FOR DB")
    print("DB INFO:", r)
except Exception as e:
    print("DB INFO error:", e)

# Count records
for table in ["memory", "action_log", "counter", "checkpoint"]:
    try:
        r = db.query(f"SELECT count() AS c FROM {table} GROUP ALL")
        print(f"  {table}: {r}")
    except Exception as e:
        print(f"  {table}: ERROR {e}")
