#!/usr/bin/env python3
"""
Migrate from SurrealDB v2.3.5 server format to v2.0.4 server format.

READ side:  v2.3.5 server on /data/surreal_db  (SELECT works with Python 2.0.0)
WRITE side: v2.0.4 server on /data/surreal_db_v204

Usage (inside container):
  docker run --rm \
    -v ~/.yadgar:/data \
    -v /tmp/surreal_v2.0.4:/surreal_v204:ro \
    -v /path/to/scripts:/scripts \
    looseking/yadgar:3.1.0 \
    python3 /scripts/migrate_v235_to_v204.py
"""

import os as _os
import subprocess
import time
import urllib.request

SURREAL_235 = "/usr/local/bin/surreal"  # v2.3.5 — in the image
SURREAL_204 = "/surreal_v204"  # v2.0.4 — mounted from host
SRC_PATH = "/data/surreal_db"
DST_PATH = "/data/surreal_db_v204"
SRC_PORT = 19001
DST_PORT = 19002

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

_SURREAL_USER = _os.environ.get("SURREAL_USER") or _os.environ.get("YADGAR_DB_USER")
_SURREAL_PASS = _os.environ.get("SURREAL_PASS") or _os.environ.get("YADGAR_DB_PASS")
if not _SURREAL_USER or not _SURREAL_PASS:
    raise RuntimeError(
        "SURREAL_USER/SURREAL_PASS (or YADGAR_DB_USER/YADGAR_DB_PASS) must be set. "
        "Example: SURREAL_USER=root SURREAL_PASS=<secret> python3 migrate_v235_to_v204.py"
    )


def start_server(binary, path, port):
    proc = subprocess.Popen(
        [
            binary,
            "start",
            "--no-banner",
            "--bind",
            f"127.0.0.1:{port}",
            "--user",
            _SURREAL_USER,
            "--pass",
            _SURREAL_PASS,
            f"surrealkv://{path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return proc
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    err = proc.stderr.read().decode()
    raise RuntimeError(f"Server on port {port} did not start. stderr: {err[-400:]}")


def connect(port):
    from surrealdb import Surreal

    db = Surreal(f"ws://127.0.0.1:{port}")
    db.signin({"username": _SURREAL_USER, "password": _SURREAL_PASS})
    db.use("yadgar", "main")
    return db


def main():
    from surrealdb import Surreal  # noqa: F401 — ensure importable before starting servers

    print("==> Starting v2.3.5 source server…")
    src_proc = start_server(SURREAL_235, SRC_PATH, SRC_PORT)
    print("   Ready.")

    print("==> Starting v2.0.4 destination server…")
    dst_proc = start_server(SURREAL_204, DST_PATH, DST_PORT)
    print("   Ready.\n")

    try:
        src = connect(SRC_PORT)
        dst = connect(DST_PORT)

        # ── Export ──────────────────────────────────────────────────────────
        print("==> Exporting from v2.3.5…")
        all_data: dict[str, list] = {}
        for table in TABLES:
            records: list = []
            offset, batch = 0, 500
            while True:
                batch_records = src.query(f"SELECT * FROM {table} LIMIT {batch} START {offset}")
                if not batch_records:
                    break
                records.extend(batch_records)
                if len(batch_records) < batch:
                    break
                offset += batch
            all_data[table] = records
            print(f"   {table}: {len(records)} records")

        src.close()
        total = sum(len(v) for v in all_data.values())
        print(f"==> Export complete: {total} total records\n")

        # ── Import ──────────────────────────────────────────────────────────
        print("==> Importing into v2.0.4…")
        for table, records in all_data.items():
            if not records:
                continue
            ok, warn = 0, 0
            for record in records:
                rid = record.get("id")
                if rid is None:
                    continue
                content = {k: v for k, v in record.items() if k != "id"}
                rid_str = str(rid)
                rid_part = rid_str.split(":")[-1] if ":" in rid_str else rid_str
                try:
                    rid_val = int(rid_part) if rid_part.isdigit() else rid_part
                except Exception:
                    rid_val = rid_part
                try:
                    dst.query(
                        "UPSERT type::thing($table, $rid) CONTENT $content",
                        {"table": table, "rid": rid_val, "content": content},
                    )
                    ok += 1
                except Exception as exc:
                    print(f"   WARNING {table}:{rid} — {exc}")
                    warn += 1
            print(f"   {table}: {ok} ok, {warn} warn")

        dst.close()
        print("\n==> Migration complete.")
        print(f"    New DB (v2.0.4 format): {DST_PATH}")
        print(
            "    Next: stop service, mv surreal_db→surreal_db_v235, "
            "mv surreal_db_v204→surreal_db, update Dockerfile to v2.0.4, rebuild, restart."
        )

    finally:
        src_proc.terminate()
        src_proc.wait()
        dst_proc.terminate()
        dst_proc.wait()


if __name__ == "__main__":
    main()
