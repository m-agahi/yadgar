"""Lint guard + unit tests for C1: column projection in bulk embedding/heat scans.

C1 adds projected helpers to storage/memory.py:
  - iter_embeddings_minimal()  → list[tuple[int, bytes]] — (id, embedding) only
  - get_embeddings_by_ids(ids) → list[tuple[int, bytes]] — for C3 two-phase fetch
  - get_ids_with_heat()        → list[tuple[int, float]] — (id, heat) only

The existing full-row methods are kept as shims; all current callers need
extra fields (content/metadata/datetime fields) and cannot be switched yet:

  get_all_memories_with_embeddings callers — deferred (need content):
    dream.py:23             — _create_dream_insight accesses content; C3 two-phase
    community.py:179        — _build_cluster_summary + _compute_centroid need content+cluster_id

  get_all_memories_for_decay callers — deferred (need extra fields):
    heat_decay.py:94        — needs last_accessed, last_decay_at, is_protected (C2: server-side)
    community.py:162        — _find_memories_for_entities needs content
    community.py:231        — _create_root_clusters needs cluster_id, directory_context
    embed_compress.py:56    — needs created_at, content, compressed
    gap_detection.py:20     — needs heat, tags, confidence, content, id

This guard fails (RED) if the projected helpers do not exist in memory.py,
ensuring they are added before this test turns GREEN.
"""

import ast
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest  # noqa: F401 (used via pytest.approx, pytest.skip, pytest.fail)

# ---------------------------------------------------------------------------
# Helper: locate memory.py
# ---------------------------------------------------------------------------


def _memory_py() -> Path:
    root = Path(__file__).parent.parent  # yadgar/ package root
    p = root / "storage" / "memory.py"
    assert p.exists(), f"storage/memory.py not found at {p}"
    return p


# ---------------------------------------------------------------------------
# Lint guard: projected helpers must exist in memory.py
# ---------------------------------------------------------------------------


class TestProjectedHelpersExist:
    """Fail if the projected helper methods are missing from memory.py.

    These helpers are required by C1. The test is RED before helpers are added,
    GREEN after.
    """

    def _defined_methods(self) -> set[str]:
        source = _memory_py().read_text()
        tree = ast.parse(source)
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_iter_embeddings_minimal_exists(self):
        """iter_embeddings_minimal() must be defined in memory.py (C1 helper)."""
        methods = self._defined_methods()
        assert "iter_embeddings_minimal" in methods, (
            "iter_embeddings_minimal() not found in storage/memory.py.\n"
            "Add it: SELECT meta::id(id) AS id, embedding FROM memory "
            "WHERE embedding IS NOT NONE AND heat > 0"
        )

    def test_get_embeddings_by_ids_exists(self):
        """get_embeddings_by_ids() must be defined in memory.py (C1 helper, used by C3)."""
        methods = self._defined_methods()
        assert "get_embeddings_by_ids" in methods, (
            "get_embeddings_by_ids() not found in storage/memory.py.\n"
            "Add it: SELECT meta::id(id) AS id, embedding FROM memory WHERE id IN $ids"
        )

    def test_get_ids_with_heat_exists(self):
        """get_ids_with_heat() must be defined in memory.py (C1 helper for heat-only scans)."""
        methods = self._defined_methods()
        assert "get_ids_with_heat" in methods, (
            "get_ids_with_heat() not found in storage/memory.py.\n"
            "Add it: SELECT meta::id(id) AS id, heat FROM memory WHERE heat > 0"
        )


# ---------------------------------------------------------------------------
# Lint guard: new projected helpers must NOT use SELECT *
# ---------------------------------------------------------------------------


class TestProjectedHelpersDoNotUseSelectStar:
    """The new projected helpers must use column-projected SQL, not SELECT *.

    This ensures C1 actually reduces data transfer and doesn't just rename the
    same SELECT * call.
    """

    def _get_method_body_source(self, method_name: str) -> str:
        """Extract source lines of a named method from memory.py."""
        source = _memory_py().read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ):
                # ast gives us line numbers (1-indexed)
                start = node.lineno - 1
                end = node.end_lineno
                return "\n".join(lines[start:end])
        return ""

    def test_iter_embeddings_minimal_no_select_star(self):
        body = self._get_method_body_source("iter_embeddings_minimal")
        if not body:
            pytest.skip("iter_embeddings_minimal not yet defined")
        assert "SELECT *" not in body, (
            "iter_embeddings_minimal() uses SELECT * — must project only id + embedding"
        )

    def test_get_embeddings_by_ids_no_select_star(self):
        body = self._get_method_body_source("get_embeddings_by_ids")
        if not body:
            pytest.skip("get_embeddings_by_ids not yet defined")
        assert "SELECT *" not in body, (
            "get_embeddings_by_ids() uses SELECT * — must project only id + embedding"
        )

    def test_get_ids_with_heat_no_select_star(self):
        body = self._get_method_body_source("get_ids_with_heat")
        if not body:
            pytest.skip("get_ids_with_heat not yet defined")
        assert "SELECT *" not in body, (
            "get_ids_with_heat() uses SELECT * — must project only id + heat"
        )


# ---------------------------------------------------------------------------
# Unit tests: projected helpers return correct (id, embedding) / (id, heat)
# ---------------------------------------------------------------------------


def _floats_to_bytes(floats: list[float]) -> bytes:
    """Pack a list of floats to bytes (little-endian float32) — mirrors storage encode."""
    return struct.pack(f"<{len(floats)}f", *floats)


def _bytes_to_floats(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"<{n}f", b))


class TestIterEmbeddingsMinimal:
    """Unit tests for iter_embeddings_minimal() — (id, embedding) tuples."""

    def _make_storage(self, rows):
        """Build a minimal storage mock with _q returning raw rows and _extract_id/decode."""
        from yadgar.storage.client import _MemoryClient  # noqa: F401

        storage = MagicMock()
        storage._q.return_value = rows
        # _extract_id: strip "memory:N" → N
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )
        # _floats_to_bytes: encode float list to bytes
        storage._floats_to_bytes.side_effect = _floats_to_bytes
        return storage

    def test_returns_list_of_id_embedding_tuples(self):
        """iter_embeddings_minimal returns list[(int, bytes)] — correct ids and embeddings."""
        from yadgar.storage.memory import _MemoryMixin

        emb1 = [0.1, 0.2, 0.3]
        emb2 = [0.4, 0.5, 0.6]
        # SurrealDB raw rows: id as string, embedding as float list
        raw_rows = [
            {"id": "memory:1", "embedding": emb1},
            {"id": "memory:2", "embedding": emb2},
        ]

        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )
        storage._floats_to_bytes.side_effect = _floats_to_bytes

        result = _MemoryMixin.iter_embeddings_minimal(storage)

        assert isinstance(result, list)
        assert len(result) == 2
        ids = [r[0] for r in result]
        embs = [r[1] for r in result]
        assert ids == [1, 2]
        assert all(isinstance(e, bytes) for e in embs)
        assert _bytes_to_floats(embs[0]) == pytest.approx(emb1, rel=1e-5)
        assert _bytes_to_floats(embs[1]) == pytest.approx(emb2, rel=1e-5)

    def test_skips_rows_without_embedding(self):
        """Rows with None embedding are excluded."""
        from yadgar.storage.memory import _MemoryMixin

        raw_rows = [
            {"id": "memory:1", "embedding": [0.1, 0.2]},
            {"id": "memory:2", "embedding": None},
        ]
        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )
        storage._floats_to_bytes.side_effect = _floats_to_bytes

        result = _MemoryMixin.iter_embeddings_minimal(storage)
        assert len(result) == 1
        assert result[0][0] == 1

    def test_empty_store_returns_empty_list(self):
        """Empty DB returns empty list."""
        from yadgar.storage.memory import _MemoryMixin

        storage = MagicMock()
        storage._q.return_value = []
        result = _MemoryMixin.iter_embeddings_minimal(storage)
        assert result == []

    def test_embedding_bytes_round_trip(self):
        """Embedding bytes decode back to original float values (within float32 precision)."""
        from yadgar.storage.memory import _MemoryMixin

        floats = [float(i) * 0.01 for i in range(384)]
        raw_rows = [{"id": "memory:42", "embedding": floats}]
        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )
        storage._floats_to_bytes.side_effect = _floats_to_bytes

        result = _MemoryMixin.iter_embeddings_minimal(storage)
        assert len(result) == 1
        assert result[0][0] == 42
        decoded = _bytes_to_floats(result[0][1])
        assert decoded == pytest.approx(floats, rel=1e-5)


class TestGetEmbeddingsByIds:
    """Unit tests for get_embeddings_by_ids(ids) — fetch projected rows for id list."""

    def test_returns_matching_ids_only(self):
        from yadgar.storage.memory import _MemoryMixin

        raw_rows = [
            {"id": "memory:5", "embedding": [0.1, 0.2]},
            {"id": "memory:7", "embedding": [0.3, 0.4]},
        ]
        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )
        storage._floats_to_bytes.side_effect = _floats_to_bytes

        result = _MemoryMixin.get_embeddings_by_ids(storage, [5, 7])
        assert len(result) == 2
        assert result[0][0] == 5
        assert result[1][0] == 7

    def test_empty_id_list_returns_empty(self):
        from yadgar.storage.memory import _MemoryMixin

        storage = MagicMock()
        storage._q.return_value = []
        result = _MemoryMixin.get_embeddings_by_ids(storage, [])
        assert result == []
        # Should not call _q with an empty id list (avoids needless DB round-trip)
        # Allow either: early return or _q returning [] — both correct.

    def test_embedding_bytes_round_trip(self):
        from yadgar.storage.memory import _MemoryMixin

        floats = [1.0, 2.0, 3.0]
        storage = MagicMock()
        storage._q.return_value = [{"id": "memory:99", "embedding": floats}]
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )
        storage._floats_to_bytes.side_effect = _floats_to_bytes

        result = _MemoryMixin.get_embeddings_by_ids(storage, [99])
        assert len(result) == 1
        assert result[0][0] == 99
        assert _bytes_to_floats(result[0][1]) == pytest.approx(floats, rel=1e-5)


class TestDecayScalarProjection:
    """C2: get_all_memories_for_decay_scalar() must exist and must not pull content/embedding."""

    def _defined_methods(self) -> set[str]:
        source = _memory_py().read_text()
        tree = ast.parse(source)
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def _get_method_body_source(self, method_name: str) -> str:
        source = _memory_py().read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ):
                start = node.lineno - 1
                end = node.end_lineno
                return "\n".join(lines[start:end])
        return ""

    def test_scalar_method_exists(self):
        """get_all_memories_for_decay_scalar() must be defined in memory.py (C2 helper)."""
        methods = self._defined_methods()
        assert "get_all_memories_for_decay_scalar" in methods, (
            "get_all_memories_for_decay_scalar() not found in storage/memory.py. "
            "Add projected query that drops content+embedding."
        )

    def _extract_sql_strings(self, method_name: str) -> list[str]:
        """Extract all string literals from a method body using AST — avoids comment/docstring false positives."""
        source = _memory_py().read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ):
                strings = []
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        strings.append(child.value)
                return strings
        return []

    def test_scalar_method_excludes_content(self):
        """get_all_memories_for_decay_scalar SQL must not include content column."""
        body = self._get_method_body_source("get_all_memories_for_decay_scalar")
        if not body:
            pytest.skip("get_all_memories_for_decay_scalar not yet defined")
        assert "SELECT *" not in body, (
            "get_all_memories_for_decay_scalar() uses SELECT * — must project scalar columns only"
        )
        # Check SQL strings only, not comments/docstrings
        sql_strings = self._extract_sql_strings("get_all_memories_for_decay_scalar")
        for s in sql_strings:
            if "SELECT" in s.upper():
                assert "content" not in s, (
                    "get_all_memories_for_decay_scalar() SQL selects 'content' — must exclude it"
                )

    def test_scalar_method_excludes_embedding(self):
        """get_all_memories_for_decay_scalar SQL must not include embedding column."""
        body = self._get_method_body_source("get_all_memories_for_decay_scalar")
        if not body:
            pytest.skip("get_all_memories_for_decay_scalar not yet defined")
        assert "SELECT *" not in body, (
            "get_all_memories_for_decay_scalar() uses SELECT * — must project scalar columns only"
        )
        # Check SQL strings only, not comments/docstrings
        sql_strings = self._extract_sql_strings("get_all_memories_for_decay_scalar")
        for s in sql_strings:
            if "SELECT" in s.upper():
                assert "embedding" not in s, (
                    "get_all_memories_for_decay_scalar() SQL selects 'embedding' — must exclude it"
                )

    def test_scalar_method_includes_required_fields(self):
        """get_all_memories_for_decay_scalar must include all fields the decay math reads."""
        body = self._get_method_body_source("get_all_memories_for_decay_scalar")
        if not body:
            pytest.skip("get_all_memories_for_decay_scalar not yet defined")
        required = [
            "heat",
            "is_protected",
            "last_accessed",
            "last_decay_at",
            "access_count_since_decay",
            "tags",
            "importance",
            "emotional_valence",
            "confidence",
        ]
        for field in required:
            assert field in body, (
                f"get_all_memories_for_decay_scalar() is missing required field '{field}'"
            )

    def test_heat_decay_calls_scalar_method(self):
        """heat_decay.py must call get_all_memories_for_decay_scalar, not get_all_memories_for_decay."""
        root = Path(__file__).parent.parent
        decay_py = root / "consolidation" / "heat_decay.py"
        assert decay_py.exists(), f"heat_decay.py not found at {decay_py}"
        source = decay_py.read_text()
        assert "get_all_memories_for_decay_scalar" in source, (
            "heat_decay.py does not call get_all_memories_for_decay_scalar() — "
            "update _decay_memories to use the scalar projection (C2)"
        )
        # Guard: original full-row call must NOT remain in heat_decay.py
        # (allows the name as a substring in a comment referencing the old method,
        # but must not appear as a method call)
        import re

        calls = re.findall(r"self\._storage\.get_all_memories_for_decay\b", source)
        # Should be zero calls to the non-scalar variant
        assert len(calls) == 0, (
            "heat_decay.py still calls get_all_memories_for_decay() (full SELECT *) — "
            "must be replaced with get_all_memories_for_decay_scalar()"
        )

    def test_scalar_method_returns_dicts_with_required_keys(self):
        """Unit test: scalar method returns list[dict] with all required decay fields."""
        from yadgar.storage.memory import _MemoryMixin

        required_keys = {
            "id",
            "heat",
            "is_protected",
            "last_accessed",
            "last_decay_at",
            "access_count_since_decay",
            "tags",
            "importance",
            "emotional_valence",
            "confidence",
        }
        now_iso = "2026-06-25T00:00:00+00:00"
        raw_rows = [
            {
                "id": "memory:1",
                "heat": 0.8,
                "is_protected": False,
                "last_accessed": now_iso,
                "last_decay_at": now_iso,
                "access_count_since_decay": 2,
                "tags": ["work"],
                "importance": 0.6,
                "emotional_valence": 0.1,
                "confidence": 0.9,
            }
        ]
        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._rows_to_dicts.return_value = raw_rows  # passthrough

        result = _MemoryMixin.get_all_memories_for_decay_scalar(storage)
        assert len(result) == 1
        row = result[0]
        for key in required_keys:
            assert key in row, f"Key '{key}' missing from scalar result row"
        # Confirm content and embedding absent from the row
        assert "content" not in row, "content must not be in scalar decay result"
        assert "embedding" not in row, "embedding must not be in scalar decay result"


class TestGetIdsWithHeat:
    """Unit tests for get_ids_with_heat() — (id, heat) tuples for heat-decay scans."""

    def test_returns_id_heat_tuples(self):
        from yadgar.storage.memory import _MemoryMixin

        raw_rows = [
            {"id": "memory:10", "heat": 0.8},
            {"id": "memory:20", "heat": 0.3},
        ]
        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )

        result = _MemoryMixin.get_ids_with_heat(storage)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == (10, pytest.approx(0.8))
        assert result[1] == (20, pytest.approx(0.3))

    def test_empty_store_returns_empty(self):
        from yadgar.storage.memory import _MemoryMixin

        storage = MagicMock()
        storage._q.return_value = []
        result = _MemoryMixin.get_ids_with_heat(storage)
        assert result == []

    def test_heat_is_float(self):
        from yadgar.storage.memory import _MemoryMixin

        raw_rows = [{"id": "memory:3", "heat": 1}]  # int from DB
        storage = MagicMock()
        storage._q.return_value = raw_rows
        storage._extract_id.side_effect = lambda rid: (
            int(rid.split(":")[1]) if isinstance(rid, str) and ":" in rid else int(rid)
        )

        result = _MemoryMixin.get_ids_with_heat(storage)
        assert isinstance(result[0][1], float)
