#!/usr/bin/env python3
"""
Migrate yadgar data from Python-client embedded surrealkv to SurrealDB server format.

Reads from /data/surreal_db (embedded mode) and writes to /data/surreal_db_new
(SurrealDB v2.3.5 server format).  Run inside the looseking/yadgar container:

  docker run --rm \
    -v /home/max/.yadgar:/data \
    -v /path/to/scripts:/scripts \
    -e YADGAR_DB_URL= \
    looseking/yadgar:latest \
    python3 /scripts/migrate_to_server.py
"""

import subprocess
import time
import urllib.request

OLD_PATH = "/data/surreal_db"
NEW_PATH = "/data/surreal_db_new"
SERVER_URL = "ws://127.0.0.1:18000"
SERVER_HTTP = "http://127.0.0.1:18000/health"

TABLES = [
    "memory",
    "action_log",
    "checkpoint",
    "counter",
    "file_hash",
    "memory_transition",
    "relationship",
    "engram_slot",
    "user_profile",
    "derived_belief",
    "wiki_page",
    "wiki_crossref",
]


def wait_for_server(url: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"SurrealDB server did not start within {timeout}s")


def main() -> None:
    from surrealdb import Surreal

    # ── Step 1: Export from embedded mode ────────────────────────────────────
    print("==> Opening embedded database (read-only export)…")
    src = Surreal(f"surrealkv://{OLD_PATH}")
    src.use("yadgar", "main")

    all_data: dict[str, list] = {}
    for table in TABLES:
        # Paginate in batches of 500 to avoid memory issues with large tables
        records: list = []
        offset = 0
        batch_size = 500
        while True:
            batch = src.query(f"SELECT * FROM {table} LIMIT {batch_size} START {offset}")
            if not batch:
                break
            records.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size
        all_data[table] = records
        print(f"   {table}: {len(records)} records")

    src.close()
    print(f"==> Export complete: {sum(len(v) for v in all_data.values())} total records\n")

    # ── Step 2: Start a fresh SurrealDB server on NEW_PATH ───────────────────
    import os

    _surreal_user = os.environ.get("SURREAL_USER") or os.environ.get("YADGAR_DB_USER")
    _surreal_pass = os.environ.get("SURREAL_PASS") or os.environ.get("YADGAR_DB_PASS")
    if not _surreal_user or not _surreal_pass:
        raise RuntimeError(
            "SURREAL_USER/SURREAL_PASS (or YADGAR_DB_USER/YADGAR_DB_PASS) must be set. "
            "Example: SURREAL_USER=root SURREAL_PASS=<secret> python3 migrate_to_server.py"
        )

    print(f"==> Starting SurrealDB server on {NEW_PATH}…")
    proc = subprocess.Popen(
        [
            "/usr/local/bin/surreal",
            "start",
            "--no-banner",
            "--bind",
            "127.0.0.1:18000",
            "--user",
            _surreal_user,
            "--pass",
            _surreal_pass,
            f"surrealkv://{NEW_PATH}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_server(SERVER_HTTP)
        print("   Server ready.\n")

        # ── Step 3: Import into server ────────────────────────────────────────
        print("==> Importing into SurrealDB server…")
        dst = Surreal(SERVER_URL)
        dst.signin({"username": _surreal_user, "password": _surreal_pass})
        dst.use("yadgar", "main")

        for table, records in all_data.items():
            if not records:
                continue
            ok = 0
            for record in records:
                rid = record.get("id")
                if rid is None:
                    continue
                # Strip 'id' from content — the record ID is supplied separately
                content = {k: v for k, v in record.items() if k != "id"}
                try:
                    dst.query(
                        "UPSERT type::thing($table, $rid) CONTENT $content",
                        {"table": table, "rid": str(rid).split(":")[-1], "content": content},
                    )
                    ok += 1
                except Exception as exc:  # noqa: BLE001 — per-record isolation in a one-shot migration: the surrealdb SDK raises no common base, and one bad row must not abandon the remaining records
                    print(f"   WARNING: {table}:{rid} — {exc}")
            print(f"   {table}: {ok}/{len(records)} records imported")

        dst.close()
        print("\n==> Migration complete.")
        print(f"    New database: {NEW_PATH}")
        print(
            "    Next: stop container, rename surreal_db → surreal_db_old, "
            "rename surreal_db_new → surreal_db, restart service."
        )

    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
