#!/usr/bin/env python3
"""Test if surrealdb-python 2.0.0 can write to a SurrealDB server binary at /surreal_test."""

import subprocess
import sys
import time
import urllib.request

BINARY = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/bin/surreal"
DB_PATH = "/tmp/compat_test_db"
PORT = 19001

proc = subprocess.Popen(
    [
        BINARY,
        "start",
        "--no-banner",
        "--bind",
        f"127.0.0.1:{PORT}",
        "--user",
        "root",
        "--pass",
        "root",
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
        except Exception:
            time.sleep(0.2)
    else:
        print("FAIL: server did not start")
        sys.exit(1)

    from surrealdb import Surreal

    db = Surreal(f"ws://127.0.0.1:{PORT}")
    db.signin({"username": "root", "password": "root"})
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
