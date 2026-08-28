#!/usr/bin/env python3
"""Test if surrealdb-python 2.0.0 can write to a SurrealDB server binary at /surreal_test."""

import os
import subprocess
import sys
import time
import urllib.request

BINARY = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/bin/surreal"
DB_PATH = "/tmp/compat_test_db"
PORT = 19001

# Credentials from environment — no hardcoded defaults.
_USER = os.environ.get("SURREAL_USER") or os.environ.get("YADGAR_DB_USER")
_PASS = os.environ.get("SURREAL_PASS") or os.environ.get("YADGAR_DB_PASS")
if not _USER or not _PASS:
    print(
        "ERROR: set SURREAL_USER + SURREAL_PASS (or YADGAR_DB_USER + YADGAR_DB_PASS)",
        file=sys.stderr,
    )
    sys.exit(1)

proc = subprocess.Popen(
    [
        BINARY,
        "start",
        "--no-banner",
        "--bind",
        f"127.0.0.1:{PORT}",
        "--user",
        _USER,
        "--pass",
        _PASS,
        f"surrealkv://{DB_PATH}",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
)

try:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        print("FAIL: server did not start")
        sys.exit(1)

    from surrealdb import Surreal

    db = Surreal(f"ws://127.0.0.1:{PORT}")
    db.signin({"username": _USER, "password": _PASS})
    db.use("test", "test")
    r = db.query("CREATE memory:1 SET content = 'hello'")
    print("write:", r)
    r2 = db.query("SELECT * FROM memory")
    print("read:", r2)
    db.close()
    print("PASS")
finally:
    proc.terminate()
    proc.wait()
