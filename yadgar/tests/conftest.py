"""Pytest configuration and shared fixtures."""

import hashlib
import os

import pytest

if os.environ.get("YADGAR_DB_URL"):

    @pytest.fixture(autouse=True)
    def _isolate_surrealdb(monkeypatch):
        """Give each test its own SurrealDB namespace to prevent state leakage.

        In server mode all StorageEngine instances connect to the same SurrealDB
        process. Without isolation, data inserted by one test leaks into the next.

        Strategy: derive a deterministic namespace from the storage path so that:
        - two engines opened on the same path share one namespace (intended sharing)
        - engines opened on different tmp_path values get separate namespaces
        """
        from yadgar import storage as _sm

        def _make_isolated(self):
            from surrealdb import Surreal

            db = Surreal(self._db_url)
            db.signin({"username": "root", "password": "root"})
            path_hash = hashlib.md5(str(self._db_path).encode()).hexdigest()[:12]
            db.use("yadgar", f"t{path_hash}")
            return db

        monkeypatch.setattr(_sm.StorageEngine, "_make_connection", _make_isolated)
