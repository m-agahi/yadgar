"""v5.25.0 — benchmark Phase 1 retrieval infra + reproducibility metadata.

TDD: written before implementation. Tests cover:
1. compute_dataset_sha256 — deterministic hash on known bytes
2. build_reproducibility_dict — correct field names, no API calls
3. get_yadgar_commit — subprocess call pattern + fail-soft
4. get_claude_version — subprocess call pattern + fail-soft on missing binary
5. --retrieval-only flag path — verify NO claude -p calls when flag set
6. output dict reproducibility fields present in run_benchmark() result
7. dataset hash pin constant exists and is a valid sha256 format OR empty (pin-after-first-download)

No ML pipeline is invoked. Heavy fixtures are mocked.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

# ── Target imports (will fail red until implementation) ───────────────
from benchmarks.run_longmemeval import (
    LONGMEMEVAL_S_SHA256,
    build_reproducibility_dict,
    compute_dataset_sha256,
    get_claude_version,
    get_yadgar_commit,
)

# ── 1. compute_dataset_sha256 ─────────────────────────────────────────


def test_sha256_known_bytes(tmp_path):
    """SHA256 of deterministic bytes must match expected hash."""
    data = b"yadgar benchmark test fixture"
    expected = hashlib.sha256(data).hexdigest()
    p = tmp_path / "fixture.json"
    p.write_bytes(data)
    assert compute_dataset_sha256(p) == expected


def test_sha256_returns_64_hex_chars(tmp_path):
    """Output is always 64 lowercase hex characters."""
    p = tmp_path / "any.json"
    p.write_bytes(b"hello")
    result = compute_dataset_sha256(p)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


# ── 2. get_yadgar_commit ──────────────────────────────────────────────


def test_get_yadgar_commit_returns_40_hex(monkeypatch):
    """Returns 40-char hex string when git available."""
    fake_sha = "a" * 40
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *a, **kw: (fake_sha + "\n").encode(),
    )
    result = get_yadgar_commit()
    assert result == fake_sha


def test_get_yadgar_commit_fail_soft(monkeypatch):
    """Returns None (not exception) when git unavailable."""

    def _raise(*a, **kw):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr("subprocess.check_output", _raise)
    result = get_yadgar_commit()
    assert result is None


# ── 3. get_claude_version ─────────────────────────────────────────────


def test_get_claude_version_parses_output(monkeypatch):
    """Returns version string when claude binary present."""
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *a, **kw: b"Claude Code 1.2.3\n",
    )
    result = get_claude_version()
    assert result == "Claude Code 1.2.3"


def test_get_claude_version_fail_soft(monkeypatch):
    """Returns None when claude binary not found."""

    def _raise(*a, **kw):
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr("subprocess.check_output", _raise)
    result = get_claude_version()
    assert result is None


# ── 4. build_reproducibility_dict ────────────────────────────────────


def test_build_reproducibility_dict_required_fields(tmp_path, monkeypatch):
    """Output dict must contain all required reproducibility fields."""
    # Minimal mock settings
    settings = MagicMock()
    settings.EMBEDDING_MODEL = "all-MiniLM-L6-v2"

    data = b'[{"question_id": "q1"}]'
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_bytes(data)

    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *a, **kw: b"deadbeef" * 5 + b"\n",
    )

    result = build_reproducibility_dict(dataset_path, settings)

    required_keys = {
        "yadgar_commit",
        "dataset_sha256",
        "embedding_model",
        "reader_llm",
        "judge_llm",
        "python_version",
        "run_date_utc",
    }
    assert required_keys.issubset(result.keys()), f"Missing keys: {required_keys - result.keys()}"


def test_build_reproducibility_dict_placeholders_in_retrieval_only(tmp_path, monkeypatch):
    """reader_llm and judge_llm are None for retrieval-only (Phase 1)."""
    settings = MagicMock()
    settings.EMBEDDING_MODEL = "test-model"

    dataset_path = tmp_path / "data.json"
    dataset_path.write_bytes(b"[]")

    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *a, **kw: b"abc123\n",
    )

    result = build_reproducibility_dict(dataset_path, settings)
    assert result["reader_llm"] is None
    assert result["judge_llm"] is None


def test_build_reproducibility_dict_embedding_model_captured(tmp_path, monkeypatch):
    """embedding_model field reflects actual settings value."""
    settings = MagicMock()
    settings.EMBEDDING_MODEL = "sentence-transformers/all-mpnet-base-v2"

    dataset_path = tmp_path / "data.json"
    dataset_path.write_bytes(b"[]")

    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: b"sha\n")

    result = build_reproducibility_dict(dataset_path, settings)
    assert result["embedding_model"] == "sentence-transformers/all-mpnet-base-v2"


# ── 5. LONGMEMEVAL_S_SHA256 constant ─────────────────────────────────


def test_sha256_pin_constant_is_valid_format():
    """Pin constant must be 64-char hex string OR empty string (pin-after-first-download)."""
    assert isinstance(LONGMEMEVAL_S_SHA256, str), "LONGMEMEVAL_S_SHA256 must be a str"
    if LONGMEMEVAL_S_SHA256:
        assert len(LONGMEMEVAL_S_SHA256) == 64, "Non-empty pin must be 64 hex chars"
        assert all(c in "0123456789abcdef" for c in LONGMEMEVAL_S_SHA256), (
            "Pin must be lowercase hex"
        )


# ── 6. retrieval-only flag suppresses claude -p calls ─────────────────


def test_retrieval_only_no_claude_calls(monkeypatch, tmp_path):
    """--retrieval-only must never invoke call_claude_pipe."""
    # We don't run the full pipeline; just verify that retrieval_only=True
    # means generate_answer / judge_answer are never reached.
    # This is a contract test against the flag in run_benchmark().
    import benchmarks.run_longmemeval as bm

    original_call_claude = bm.call_claude_pipe
    call_log = []

    def _spy_claude(*a, **kw):
        call_log.append(a)
        return original_call_claude(*a, **kw)

    monkeypatch.setattr(bm, "call_claude_pipe", _spy_claude)

    # Load a minimal dataset and run with retrieval_only=True
    # Mock everything heavy so this doesn't spin up ML
    minimal_question = {
        "question_id": "test_q1",
        "question_type": "single-session-user",
        "question": "What is the user's name?",
        "question_date": "2024-01-01",
        "answer": "Alice",
        "answer_session_ids": ["sess_1"],
        "haystack_sessions": [[{"role": "user", "content": "My name is Alice."}]],
        "haystack_session_ids": ["sess_1"],
        "haystack_dates": ["2024-01-01"],
    }

    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_text(json.dumps([minimal_question]))

    # Mock the heavy components
    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = 1
    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = b"\x00" * 384
    mock_embeddings.get_model_name.return_value = "test-model"
    mock_retriever = MagicMock()
    mock_retriever.recall.return_value = []
    mock_curator = MagicMock()
    mock_thermo = MagicMock()
    mock_thermo.compute_importance.return_value = 0.5

    with (
        patch("benchmarks.run_longmemeval.StorageEngine", return_value=mock_storage),
        patch("benchmarks.run_longmemeval.EmbeddingEngine", return_value=mock_embeddings),
        patch("benchmarks.run_longmemeval.KnowledgeGraph", return_value=MagicMock()),
        patch("benchmarks.run_longmemeval.MemoryThermodynamics", return_value=mock_thermo),
        patch("benchmarks.run_longmemeval.Retriever", return_value=mock_retriever),
        patch("benchmarks.run_longmemeval.MemoryCurator", return_value=mock_curator),
        patch("benchmarks.run_longmemeval.build_reproducibility_dict", return_value={}),
        patch("subprocess.check_output", return_value=b"abc123\n"),
        tempfile.TemporaryDirectory() as _tmpdir,
    ):
        result = bm.run_benchmark(
            dataset_path=dataset_path,
            retrieval_only=True,
            max_questions=1,
            output_path=str(tmp_path / "out.json"),
        )

    assert call_log == [], (
        f"call_claude_pipe was called {len(call_log)} time(s) with retrieval_only=True"
    )
    assert result["retrieval_only"] is True


# ── 7. run_benchmark output includes reproducibility dict ─────────────


def test_run_benchmark_output_has_reproducibility_key(tmp_path, monkeypatch):
    """run_benchmark result dict must include a 'reproducibility' key."""
    import benchmarks.run_longmemeval as bm

    minimal_question = {
        "question_id": "test_q2",
        "question_type": "single-session-user",
        "question": "What is the pet's name?",
        "question_date": "2024-01-01",
        "answer": "Rex",
        "answer_session_ids": ["sess_2"],
        "haystack_sessions": [[{"role": "user", "content": "My dog is Rex."}]],
        "haystack_session_ids": ["sess_2"],
        "haystack_dates": ["2024-01-01"],
    }

    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_text(json.dumps([minimal_question]))

    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = 1
    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = b"\x00" * 384
    mock_embeddings.get_model_name.return_value = "test-model"
    mock_retriever = MagicMock()
    mock_retriever.recall.return_value = []
    mock_curator = MagicMock()
    mock_thermo = MagicMock()
    mock_thermo.compute_importance.return_value = 0.5

    fake_repro = {
        "yadgar_commit": "a" * 40,
        "dataset_sha256": "b" * 64,
        "embedding_model": "test-model",
        "reader_llm": None,
        "judge_llm": None,
        "python_version": "3.14.0",
        "run_date_utc": "2026-05-31T00:00:00+00:00",
    }

    with (
        patch("benchmarks.run_longmemeval.StorageEngine", return_value=mock_storage),
        patch("benchmarks.run_longmemeval.EmbeddingEngine", return_value=mock_embeddings),
        patch("benchmarks.run_longmemeval.KnowledgeGraph", return_value=MagicMock()),
        patch("benchmarks.run_longmemeval.MemoryThermodynamics", return_value=mock_thermo),
        patch("benchmarks.run_longmemeval.Retriever", return_value=mock_retriever),
        patch("benchmarks.run_longmemeval.MemoryCurator", return_value=mock_curator),
        patch("benchmarks.run_longmemeval.build_reproducibility_dict", return_value=fake_repro),
        patch("subprocess.check_output", return_value=b"abc\n"),
    ):
        result = bm.run_benchmark(
            dataset_path=dataset_path,
            retrieval_only=True,
            max_questions=1,
            output_path=str(tmp_path / "out2.json"),
        )

    assert "reproducibility" in result, "run_benchmark() result must include 'reproducibility' key"
    assert result["reproducibility"] == fake_repro


# ── v5.25.1 — shared surreal runner + YADGAR_DB_URL override ─────────


def test_shared_runner_module_importable():
    """yadgar._surreal_runner must exist and export spawn_surreal."""
    from yadgar._surreal_runner import (  # noqa: F401
        allocate_port_with_retry,
        spawn_surreal,
        teardown_surreal_proc,
    )

    assert callable(spawn_surreal)
    assert callable(teardown_surreal_proc)
    assert callable(allocate_port_with_retry)


def test_helpers_shim_re_exports():
    """yadgar.tests._surreal_helpers still exports spawn_surreal (shim)."""
    from yadgar._surreal_runner import spawn_surreal as r_spawn
    from yadgar.tests._surreal_helpers import spawn_surreal as h_spawn  # noqa: F401

    assert h_spawn is r_spawn


def test_benchmark_skips_spawn_when_db_url_set(monkeypatch, tmp_path):
    """If YADGAR_DB_URL is already set, benchmark must NOT spawn a server."""
    import benchmarks.run_longmemeval as bm

    spawn_calls = []

    def _fake_spawn(*a, **kw):
        spawn_calls.append(a)
        raise AssertionError(
            "spawn_surreal_for_benchmark must not be called when YADGAR_DB_URL is set"
        )

    monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:19999")
    monkeypatch.setattr(bm, "spawn_surreal_for_benchmark", _fake_spawn)

    # build a minimal dataset
    minimal_question = {
        "question_id": "test_override_q",
        "question_type": "single-session-user",
        "question": "Test question?",
        "question_date": "2024-01-01",
        "answer": "Yes",
        "answer_session_ids": ["s1"],
        "haystack_sessions": [[{"role": "user", "content": "Yes."}]],
        "haystack_session_ids": ["s1"],
        "haystack_dates": ["2024-01-01"],
    }
    dataset_path = tmp_path / "longmemeval_s_cleaned.json"
    dataset_path.write_text(json.dumps([minimal_question]))

    mock_storage = MagicMock()
    mock_storage.insert_memory.return_value = 1
    mock_storage.close = MagicMock()
    mock_embeddings = MagicMock()
    mock_embeddings.encode.return_value = b"\x00" * 384
    mock_embeddings.get_model_name.return_value = "test-model"
    mock_retriever = MagicMock()
    mock_retriever.recall.return_value = []
    mock_curator = MagicMock()
    mock_thermo = MagicMock()
    mock_thermo.compute_importance.return_value = 0.5

    with (
        patch("benchmarks.run_longmemeval.StorageEngine", return_value=mock_storage),
        patch("benchmarks.run_longmemeval.EmbeddingEngine", return_value=mock_embeddings),
        patch("benchmarks.run_longmemeval.KnowledgeGraph", return_value=MagicMock()),
        patch("benchmarks.run_longmemeval.MemoryThermodynamics", return_value=mock_thermo),
        patch("benchmarks.run_longmemeval.Retriever", return_value=mock_retriever),
        patch("benchmarks.run_longmemeval.MemoryCurator", return_value=mock_curator),
        patch("benchmarks.run_longmemeval.build_reproducibility_dict", return_value={}),
        patch("subprocess.check_output", return_value=b"abc\n"),
    ):
        bm.run_benchmark(
            dataset_path=dataset_path,
            retrieval_only=True,
            max_questions=1,
            output_path=str(tmp_path / "out3.json"),
        )

    assert spawn_calls == [], (
        "spawn_surreal_for_benchmark was called despite YADGAR_DB_URL being set"
    )


def test_wipe_benchmark_tables_callable():
    """run_longmemeval must export a wipe_benchmark_tables function."""
    from benchmarks.run_longmemeval import wipe_benchmark_tables  # noqa: F401

    assert callable(wipe_benchmark_tables)
