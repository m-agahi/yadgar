"""Car A (2026-08-14 identity train, §2) — project-registry seed tests.

Closes the gap where ``backend.admin_exec.ledger_project.create_project_row``
existed and was registered in ``backend/admin_exec/__init__.py:152`` but
had no CLI / MCP path. Mirrors the style of
``test_car_m_project_param.py`` (the car that pinned the registry
refusal behaviour at the read side) — that test asserts the guard at
``MariaStorageEngine.assert_project_registered`` REJECTS unknown ids; this one asserts
the SEED path that lets the guard ever succeed.

RED → GREEN: written before the implementation; the
``yadgar.core.cli.project`` module and the ``project_seed`` MCP tool
in ``yadgar.core.server.tools.misc`` were added to make these pass.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

# The duplicate envelope is built from the REAL exception, never a
# hardcoded string: these fixtures previously asserted
# ``"DuplicateProjectError: <key>"``, a shape the backend does not
# produce (it returns ``str(exc)``, which carries no class name). That
# mismatch kept the suite green while the live seed path classified every
# duplicate as a hard failure — found on the sandbox VM 2026-08-15.
from yadgar._shared.storage.sql.errors import DuplicateProjectError

# ── parse_map / classify_row / seed_row / read_auth_token (CLI helpers) ──────


class TestParseMap:
    """Map-file TSV parser — structural contract, no backend call."""

    def test_parses_well_formed_rows(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        f.write_text(
            "# comment line, skipped\n"
            "/home/max/git/yadgar\tm-agahi/yadgar\t10\t2\tsample note\n"
            "/home/max\tlocal/max\t1\t0\tNOT a git repo\n"
        )
        rows = parse_map(f)
        assert len(rows) == 2
        assert rows[0]["source_directory"] == "/home/max/git/yadgar"
        assert rows[0]["project_id"] == "m-agahi/yadgar"
        assert rows[0]["memory_rows"] == "10"
        assert rows[0]["wiki_rows"] == "2"
        assert rows[0]["note"] == "sample note"
        assert rows[1]["project_id"] == "local/max"

    def test_skips_blank_and_comment_lines(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        f.write_text("\n# header\n\n/home/x\towner/x\t1\t0\tnote\n")
        rows = parse_map(f)
        assert len(rows) == 1
        assert rows[0]["project_id"] == "owner/x"

    def test_malformed_row_raises_systemexit(self, tmp_path: Path) -> None:
        """§2: CLI exits non-zero with structured stderr on a malformed row."""
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        f.write_text("/home/x\towner/x\t1\t0\tnote\n/too-few-cols\n")
        with pytest.raises(SystemExit) as ei:
            parse_map(f)
        assert ei.value.code == 2

    def test_missing_file_raises_systemexit(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        with pytest.raises(SystemExit) as ei:
            parse_map(tmp_path / "does-not-exist.tsv")
        assert ei.value.code == 2

    def test_empty_directory_or_project_id_raises(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        # Tab-separated: source_directory is empty (col 1 starts the line)
        f.write_text("\towner/x\t1\t0\tnote\n")
        with pytest.raises(SystemExit) as ei:
            parse_map(f)
        assert ei.value.code == 2


class TestClassifyRow:
    """Map row classification — DROP / REVIEW / seed."""

    def test_drop_classified_as_drop(self) -> None:
        from yadgar.core.cli.project import classify_row

        assert classify_row({"project_id": "DROP"}) == "drop"

    def test_review_classified_as_review(self) -> None:
        from yadgar.core.cli.project import classify_row

        assert classify_row({"project_id": "REVIEW"}) == "review"

    def test_normal_row_classified_as_seed(self) -> None:
        from yadgar.core.cli.project import classify_row

        assert classify_row({"project_id": "m-agahi/yadgar"}) == "seed"
        assert classify_row({"project_id": "local/aws-work"}) == "seed"


class TestInferKind:
    """``create_project_row`` payload ``kind`` field — git vs local."""

    def test_slash_key_is_git(self) -> None:
        from yadgar.core.cli.project import infer_kind

        assert infer_kind("m-agahi/yadgar") == "git"

    def test_local_prefix_is_local(self) -> None:
        from yadgar.core.cli.project import infer_kind

        assert infer_kind("local/max") == "local"

    def test_prose_key_falls_back_to_local(self) -> None:
        """§5.5: ``memory:<id>`` prose keys are not registry rows by themselves
        (they are explicit classifications in the map) — but if one slips
        through, the best-effort fallback is ``local``."""
        from yadgar.core.cli.project import infer_kind

        assert infer_kind("memory:486752") == "local"


class TestSeedRow:
    """Per-row ``create_project_row`` call — success / dup / fail."""

    def _row(self, project_id: str = "m-agahi/yadgar", note: str = "test") -> dict:
        return {
            "source_directory": "/home/max/git/yadgar",
            "project_id": project_id,
            "memory_rows": "1",
            "wiki_rows": "0",
            "note": note,
        }

    def test_create_returns_created(self) -> None:
        from yadgar.core.cli.project import seed_row

        with patch(
            "yadgar.core.forward._forward_admin",
            return_value={"ok": True, "row": {"key": "m-agahi/yadgar"}},
        ):
            out = seed_row(self._row(), auth_token="x")
        assert out == "created"

    def test_duplicate_returns_skipped(self) -> None:
        """Idempotency: second call for an already-present key is a no-op."""
        from yadgar.core.cli.project import seed_row

        with patch(
            "yadgar.core.forward._forward_admin",
            return_value={"ok": False, "error": str(DuplicateProjectError("m-agahi/yadgar"))},
        ):
            out = seed_row(self._row(), auth_token="x")
        assert out == "skipped"

    def test_backend_error_returns_failed(self) -> None:
        from yadgar.core.cli.project import seed_row

        with patch(
            "yadgar.core.forward._forward_admin",
            return_value={"ok": False, "error": "engine #2 not composed"},
        ):
            out = seed_row(self._row(), auth_token="x")
        assert out == "failed"

    def test_backend_exception_returns_failed(self) -> None:
        """Fail-soft: a backend exception must NOT raise out of ``seed_row``
        (the loop must keep going for the rest of the map)."""
        from yadgar.core.cli.project import seed_row

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=RuntimeError("backend down"),
        ):
            out = seed_row(self._row(), auth_token="x")
        assert out == "failed"


# ── cmd_project_seed — full CLI handler (idempotency + exit code) ────────────


class TestCmdProjectSeed:
    """§2: idempotency on re-run, exit 0 on a known-good map, non-zero on
    a malformed map. Mirrors test_cli_drain_module's pattern (patch the
    forward helper at its source module)."""

    def _good_map(self, tmp_path: Path) -> Path:
        f = tmp_path / "map.tsv"
        f.write_text(
            "# header\n"
            "/home/max/git/yadgar\tm-agahi/yadgar\t10\t2\tsample\n"
            "/home/max\tlocal/max\t1\t0\tNOT a git repo\n"
            "/home/x\tDROP\t0\t1\tdrop decision\n"
            "/home/y\tREVIEW\t1\t0\tneeds human\n"
        )
        return f

    def _make_args(self, map_path: Path) -> argparse.Namespace:
        return argparse.Namespace(map=str(map_path))

    @contextmanager
    def _patched_backend(self, return_values: list[dict]):
        """Patch ``_forward_admin`` at its source module — the CLI helper
        does a lazy import inside ``seed_row``, so the seam lives at
        ``yadgar.core.forward._forward_admin``."""
        if not return_values:
            return_values = [{"ok": True, "row": {}}] * 100
        it = iter(return_values)

        def side_effect(*_args, **_kwargs):
            return next(it)

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=side_effect,
        ) as fwd:
            yield fwd

    def test_seeds_two_rows_and_skips_drop_and_review(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import cmd_project_seed

        map_path = self._good_map(tmp_path)
        with self._patched_backend([{"ok": True, "row": {}}, {"ok": True, "row": {}}]):
            rc = cmd_project_seed(self._make_args(map_path))
        assert rc == 0

    def test_idempotency_second_run_is_noop(self, tmp_path: Path) -> None:
        """§2 test bullet: seed the same map twice; second call is a no-op
        for already-present rows. The backend returns ``DuplicateProjectError``
        on the second run, which the wrapper converts to
        ``{"ok": False, "error": "..."}`` and we classify as ``skipped``."""
        from yadgar.core.cli.project import cmd_project_seed

        map_path = self._good_map(tmp_path)
        args = self._make_args(map_path)

        # First run: both rows succeed.
        with self._patched_backend([{"ok": True, "row": {}}, {"ok": True, "row": {}}]):
            rc = cmd_project_seed(args)
        assert rc == 0

        # Second run: both rows are already registered → backend says dup.
        dup = {"ok": False, "error": str(DuplicateProjectError("m-agahi/yadgar"))}
        with self._patched_backend(
            [dup, {"ok": False, "error": str(DuplicateProjectError("local/max"))}]
        ):
            rc = cmd_project_seed(args)
        assert rc == 0

    def test_known_good_map_exits_zero(self, tmp_path: Path) -> None:
        """§2 test bullet: CLI exits 0 on a known-good map."""
        from yadgar.core.cli.project import cmd_project_seed

        map_path = self._good_map(tmp_path)
        with self._patched_backend([{"ok": True, "row": {}}, {"ok": True, "row": {}}]):
            rc = cmd_project_seed(self._make_args(map_path))
        assert rc == 0

    def test_malformed_map_exits_nonzero(self, tmp_path: Path) -> None:
        """§2 test bullet: CLI exits non-zero with structured stderr on a
        malformed map row."""
        from yadgar.core.cli.project import cmd_project_seed

        f = tmp_path / "bad.tsv"
        f.write_text("/home/x\towner/x\t1\t0\tnote\n/too-few-cols\n")
        with pytest.raises(SystemExit) as ei:
            cmd_project_seed(argparse.Namespace(map=str(f)))
        assert ei.value.code == 2

    def test_missing_map_exits_nonzero(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import cmd_project_seed

        with pytest.raises(SystemExit) as ei:
            cmd_project_seed(argparse.Namespace(map=str(tmp_path / "nope.tsv")))
        assert ei.value.code == 2

    def test_genuine_row_failure_exits_nonzero(self, tmp_path: Path) -> None:
        """Ledger task 13 defect 1: a genuine (non-duplicate) per-row backend
        failure must fail the whole run's exit code — not just print a FAIL
        line to stderr and return 0. Before the fix this returned 0 even
        though ``counts["failed"] == 1``, which is exactly the shape that
        let a real DataError-1406 truncation bug get reported as
        "transient" on 2026-08-20."""
        from yadgar.core.cli.project import cmd_project_seed

        map_path = self._good_map(tmp_path)
        with self._patched_backend(
            [
                {"ok": True, "row": {}},
                {"ok": False, "error": "engine #2 not composed"},
            ]
        ):
            rc = cmd_project_seed(self._make_args(map_path))
        assert rc == 1

    def test_all_duplicate_rows_still_exit_zero(self, tmp_path: Path) -> None:
        """Pin: duplicates (idempotent re-run) are ``skipped``, not
        ``failed`` — they must NOT trip the new failure gate."""
        from yadgar.core.cli.project import cmd_project_seed

        map_path = self._good_map(tmp_path)
        dup = {"ok": False, "error": str(DuplicateProjectError("m-agahi/yadgar"))}
        with self._patched_backend(
            [dup, {"ok": False, "error": str(DuplicateProjectError("local/max"))}]
        ):
            rc = cmd_project_seed(self._make_args(map_path))
        assert rc == 0


# ── register() — parser wiring ───────────────────────────────────────────────


class TestRegister:
    def test_creates_project_subparser_with_seed(self) -> None:
        from yadgar.core.cli.project import register

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["project", "seed", "--map", "/tmp/map.tsv"])
        assert args.project_command == "seed"
        assert args.map == "/tmp/map.tsv"
        assert hasattr(args, "func")

    def test_seed_default_map_is_none(self) -> None:
        """When --map is omitted, the default (``cwd/.yadgar/...``) is
        resolved at handler time, not parser time."""
        from yadgar.core.cli.project import register

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["project", "seed"])
        assert args.map is None
        assert hasattr(args, "func")


# ── MCP tool: project_seed — wire shape + idempotency + guard-stays-on ──────


class TestProjectSeedMcpTool:
    """MCP surface — mirrors the CLI's behavior at the tool boundary, plus
    the §2 test bullet that the existing guard at
    ``MariaStorageEngine.assert_project_registered`` is NOT relaxed (the new
    tool only SEEDS — it does not weaken the refusal)."""

    def test_tool_call_seeds_a_known_good_map(self, tmp_path: Path) -> None:
        """§2: idempotency + seed behaviour at the MCP boundary."""
        from yadgar.core.server.tools.misc import project_seed

        f = tmp_path / "map.tsv"
        f.write_text("/home/x\towner/x\t1\t0\tnote\n/home/y\tlocal/y\t1\t0\tnote\n")

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=[
                {"ok": True, "row": {"key": "owner/x"}},
                {"ok": True, "row": {"key": "local/y"}},
            ],
        ):
            result = project_seed(map_path=str(f))

        assert result["ok"] is True
        assert result["counts"]["seed"] == 2
        assert result["counts"]["created"] == 2
        assert result["counts"]["skipped"] == 0
        assert result["counts"]["drop"] == 0
        assert result["counts"]["review"] == 0

    def test_tool_call_idempotent_second_run(self, tmp_path: Path) -> None:
        """Second call is a no-op for already-present rows."""
        from yadgar.core.server.tools.misc import project_seed

        f = tmp_path / "map.tsv"
        f.write_text("/home/x\towner/x\t1\t0\tnote\n")

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=[
                {"ok": True, "row": {"key": "owner/x"}},
                {"ok": False, "error": str(DuplicateProjectError("owner/x"))},
            ],
        ):
            first = project_seed(map_path=str(f))
            second = project_seed(map_path=str(f))

        assert first["counts"]["created"] == 1
        assert first["counts"]["skipped"] == 0
        assert second["counts"]["created"] == 0
        assert second["counts"]["skipped"] == 1
        # Pin: an all-duplicate re-run is a clean idempotent no-op — ``ok``
        # must stay True. Only a GENUINE failure should flip it.
        assert second["ok"] is True

    def test_tool_ok_false_on_genuine_row_failure(self, tmp_path: Path) -> None:
        """Ledger task 13 defect 1: ``ok`` was a LITERAL ``True`` regardless
        of ``counts["failed"]`` — a caller reading ``ok`` first (the field a
        model checks first) saw success while a row silently failed. This
        pins the fix: ``ok`` reflects ``counts["failed"] == 0``, and the
        counts payload stays intact either way."""
        from yadgar.core.server.tools.misc import project_seed

        f = tmp_path / "map.tsv"
        f.write_text("/home/x\towner/x\t1\t0\tnote\n/home/y\towner/y\t1\t0\tnote\n")

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=[
                {"ok": True, "row": {"key": "owner/x"}},
                {"ok": False, "error": "engine #2 not composed"},
            ],
        ):
            result = project_seed(map_path=str(f))

        assert result["ok"] is False
        assert result["counts"]["failed"] == 1
        assert result["counts"]["created"] == 1
        assert result["counts"]["seed"] == 2
        assert result["map_path"] == str(f)

    def test_tool_rejects_unknown_id_via_existing_guard(self, tmp_path: Path) -> None:
        """§2 test bullet: the registry guard STILL refuses unknown
        project_ids on the WRITE path after the seed is added. The new tool
        only seeds; the guard stays in force (ADR-0078).

        Task 384 re-pointed this at ``MariaStorageEngine.assert_project_registered``.
        It used to name ``_ensure_project_exists_sync`` — a function with zero
        call sites, so the "guard on the WRITE path" this test claimed to pin
        was not the one any write reached."""
        import inspect

        from yadgar._shared.storage.sql.errors import UnknownProjectError
        from yadgar._shared.storage.sql.mariadb import MariaStorageEngine

        sig = inspect.signature(MariaStorageEngine.assert_project_registered)
        assert "project_id" in sig.parameters
        # The class identity is what every ``except`` binds on; if the
        # seed refactor accidentally re-raised something else the
        # downstream catches would all silently miss.
        assert UnknownProjectError is not None

    def test_tool_returns_error_envelope_on_missing_map(self, tmp_path: Path) -> None:
        """Map file structural errors come back as ``{"ok": False, ...}`` —
        the MCP boundary does not raise SystemExit."""
        from yadgar.core.server.tools.misc import project_seed

        result = project_seed(map_path=str(tmp_path / "does-not-exist.tsv"))
        assert result["ok"] is False
        assert "map" in result["error"].lower()

    def test_tool_skips_drop_and_review_rows(self, tmp_path: Path) -> None:
        """DROP / REVIEW rows are operator decisions, not registry rows."""
        from yadgar.core.server.tools.misc import project_seed

        f = tmp_path / "map.tsv"
        f.write_text(
            "/home/x\tDROP\t0\t1\tdrop decision\n"
            "/home/y\tREVIEW\t1\t0\tneeds human\n"
            "/home/z\towner/z\t1\t0\tnote\n"
        )

        with patch(
            "yadgar.core.forward._forward_admin",
            return_value={"ok": True, "row": {"key": "owner/z"}},
        ) as fwd:
            result = project_seed(map_path=str(f))

        assert result["counts"]["drop"] == 1
        assert result["counts"]["review"] == 1
        assert result["counts"]["seed"] == 1
        assert result["counts"]["created"] == 1
        # Backend was called exactly once (only the seed-eligible row).
        assert fwd.call_count == 1
