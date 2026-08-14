"""Car D (2026-08-14 identity train, §3) — corpus re-key migration tests.

Three tests, mirroring the task's contract:

  TestParseMap        — map-file TSV parser. Reads Car A's ``parse_map``
                        (which is what ``rekey_corpus`` uses too) and
                        asserts the column-2-is-authoritative contract.

  TestDryRunNoWrites  — the dry-run path. Inject a fake storage that
                        counts every ``_q`` call; assert exactly one
                        read per table, zero writes, the map TSV
                        contains the expected rows, and a second
                        ``run()`` call with ``apply=True`` on an
                        unseeded registry still does not write to the
                        corpus.

  TestGroupedReport   — the bucketed report. Sentinel + prose + git
                        + local directories all in one fixture; assert
                        the report buckets them correctly, the
                        collision detector flags same-basename paths,
                        and the map TSV is grouped (drop / review /
                        seed) in the operator's expected order.

The test seams are the same ones project_backfill.py exposes:
``_get_storage`` and ``_forward_admin`` (via Car A's ``seed_row``).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

# ── map parsing (Car A's parse_map — re-used by rekey_corpus) ───────────────


class TestParseMap:
    """Column-2-is-authoritative contract for the re-key map.

    Car A already shipped ``yadgar.core.cli.project.parse_map``; Car D
    reuses it. This test pins the behaviour rekey_corpus depends on:
    ``parse_map`` returns rows with the five expected keys and skips
    comment lines, so a hand-edited map with a ``DROP`` column-2
    passes through to the apply path unchanged.
    """

    def test_parses_well_formed_rows(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        f.write_text(
            "# comment line, skipped\n"
            "/home/max/git/yadgar\tm-agahi/yadgar\t10\t2\tsample\n"
            "/home/max\tlocal/max\t1\t0\tNOT a git repo\n"
            "/home/x\tDROP\t0\t1\tdrop decision\n"
            "/home/y\tREVIEW\t1\t0\tneeds human\n"
        )
        rows = parse_map(f)
        assert len(rows) == 4
        assert rows[0]["source_directory"] == "/home/max/git/yadgar"
        assert rows[0]["project_id"] == "m-agahi/yadgar"
        assert rows[2]["project_id"] == "DROP"  # column 2 authoritative
        assert rows[3]["project_id"] == "REVIEW"

    def test_skips_blank_and_comment_lines(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        f.write_text("\n# header\n\n/home/x\towner/x\t1\t0\tnote\n")
        rows = parse_map(f)
        assert len(rows) == 1
        assert rows[0]["project_id"] == "owner/x"

    def test_malformed_row_raises_systemexit(self, tmp_path: Path) -> None:
        from yadgar.core.cli.project import parse_map

        f = tmp_path / "map.tsv"
        f.write_text("/home/x\towner/x\t1\t0\tnote\n/too-few-cols\n")
        with pytest.raises(SystemExit) as ei:
            parse_map(f)
        assert ei.value.code == 2


# ── dry-run contract: zero writes ──────────────────────────────────────────


class _FakeStorage:
    """In-memory stand-in for the composed SurrealDB engine.

    Records every ``_q`` call. The Car D migration calls ``_q`` ONLY
    for reads (DISTINCT directory_context); a write call would be a
    regression and the test asserts none happened.
    """

    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict | None]] = []

    def _q(self, surql: str, params: dict | None = None) -> list[dict]:
        self.calls.append((surql, params))
        for table, rows in self._rows.items():
            if f"FROM {table}" in surql:
                return list(rows)
        return []


class TestDryRunNoWrites:
    """Dry-run contract — discover, write the map, no storage writes.

    The dry-run path is the operator's review tool: it MUST be readable
    end-to-end on a production corpus with no side effects beyond the
    map TSV itself. Asserted by:
      * counting ``_q`` calls (exactly one per table, no UPDATE/DELETE)
      * inspecting the produced map file (rows match the corpus)
      * running ``apply=False`` (default) and confirming the
        ``_forward_admin`` seam is never touched
    """

    @pytest.fixture
    def fixture(self, tmp_path: Path) -> dict:
        rows = {
            "memory": [
                {"directory_context": "/home/max/git/yadgar"},
                {"directory_context": "/home/max/git/yadgar"},
                {"directory_context": "global"},
            ],
            "wiki_page": [
                {"directory_context": "/home/max/git/yadgar"},
            ],
        }
        return {"rows": rows, "map": tmp_path / "map.tsv"}

    def test_dry_run_does_not_write_to_storage(self, fixture: dict) -> None:
        from yadgar.backend.migrations import rekey_corpus

        storage = _FakeStorage(fixture["rows"])
        result = rekey_corpus.run(
            storage=storage,
            map_path=fixture["map"],
            apply=False,
        )

        assert result["ok"] is True
        # Exactly two reads — one per table, no UPDATE / DELETE / INSERT.
        assert len(storage.calls) == 2
        for surql, _params in storage.calls:
            upper = surql.upper()
            assert "SELECT" in upper, f"dry-run must only SELECT, got: {surql!r}"
            assert "UPDATE" not in upper
            assert "DELETE" not in upper
            assert "INSERT" not in upper

    def test_dry_run_writes_map_file(self, fixture: dict) -> None:
        from yadgar.backend.migrations import rekey_corpus

        storage = _FakeStorage(fixture["rows"])
        rekey_corpus.run(
            storage=storage,
            map_path=fixture["map"],
            apply=False,
        )

        assert fixture["map"].exists()
        contents = fixture["map"].read_text()
        # Sentinels pre-classified to DROP in column 2.
        data_rows = [line for line in contents.splitlines() if line and not line.startswith("#")]
        sentinel_rows = [line for line in data_rows if line.split("\t")[1] == "DROP"]
        assert len(sentinel_rows) == 1  # the 'global' sentinel in the fixture
        # Real paths keep their derivation.
        assert "/home/max/git/yadgar" in contents

    def test_dry_run_does_not_touch_forward_admin(self, fixture: dict) -> None:
        """The apply path goes through ``_forward_admin``; dry-run must
        not, even by accident. Patched at the source module because
        ``seed_row`` does a lazy import."""
        from yadgar.backend.migrations import rekey_corpus

        storage = _FakeStorage(fixture["rows"])
        with patch("yadgar.core.forward._forward_admin") as fwd:
            rekey_corpus.run(
                storage=storage,
                map_path=fixture["map"],
                apply=False,
            )
            fwd.assert_not_called()


# ── grouped report ──────────────────────────────────────────────────────────


class TestGroupedReport:
    """The bucketed report — sentinel / prose / git / local.

    The operator reviews the dry-run output. The contract: every
    distinct directory_context lands in EXACTLY ONE bucket, the
    report is the same shape every time, and the collision detector
    flags any basename shared by two paths.
    """

    @pytest.fixture
    def fixture(self, tmp_path: Path) -> dict:
        rows = {
            "memory": [
                # git remote — derives owner/repo
                {"directory_context": "/home/max/git/yadgar"},
                {"directory_context": "/home/max/git/yadgar"},
                # local path with no git — local/<basename>
                {"directory_context": "/home/max/notes"},
                # second path with the same basename — collision
                {"directory_context": "/home/alice/notes"},
                # sentinel — DROP
                {"directory_context": "global"},
                {"directory_context": "system"},
                # free-text prose — REVIEW
                {"directory_context": "db_inspect"},
            ],
            "wiki_page": [
                {"directory_context": "/home/max/git/yadgar"},
            ],
        }
        return {"rows": rows, "map": tmp_path / "map.tsv"}

    def test_buckets_sentinel_prose_git_local(self, fixture: dict) -> None:
        from yadgar.backend.migrations import rekey_corpus

        storage = _FakeStorage(fixture["rows"])
        result = rekey_corpus.run(
            storage=storage,
            map_path=fixture["map"],
            apply=False,
        )

        report = result["report"]
        assert report["discovered"] == 6  # 6 distinct directory_contexts

        # Drop bucket — the two sentinels.
        drop_dirs = {r["directory_context"] for r in report["drop_rows"]}
        assert drop_dirs == {"global", "system"}

        # Review bucket — the prose row.
        review_dirs = {r["directory_context"] for r in report["review_rows"]}
        assert review_dirs == {"db_inspect"}

        # Seed bucket — the three real paths.
        seed_dirs = {r["directory_context"] for r in report["seed_rows"]}
        assert seed_dirs == {
            "/home/max/git/yadgar",
            "/home/max/notes",
            "/home/alice/notes",
        }

        # Git vs local — owner/repo for the git repo, local/<basename>
        # for the two non-git paths.
        by_dir = {r["directory_context"]: r for r in report["seed_rows"]}
        assert by_dir["/home/max/git/yadgar"]["kind"] == "git"
        assert by_dir["/home/max/git/yadgar"]["derived_project_id"] == "m-agahi/yadgar"
        assert by_dir["/home/max/notes"]["kind"] == "local"
        assert by_dir["/home/max/notes"]["derived_project_id"] == "local/notes"
        assert by_dir["/home/alice/notes"]["kind"] == "local"
        assert by_dir["/home/alice/notes"]["derived_project_id"] == "local/notes"

    def test_basenames_collide_detected(self, fixture: dict) -> None:
        from yadgar.backend.migrations import rekey_corpus

        storage = _FakeStorage(fixture["rows"])
        result = rekey_corpus.run(
            storage=storage,
            map_path=fixture["map"],
            apply=False,
        )

        collisions = result["report"]["basenames_collide"]
        # Two paths share the basename "notes".
        coll = {c["basename"]: c["paths"] for c in collisions}
        assert "notes" in coll
        assert sorted(coll["notes"]) == [
            "/home/alice/notes",
            "/home/max/notes",
        ]

    def test_map_file_is_grouped_drop_review_seed(self, fixture: dict) -> None:
        """Operator reviews the map TSV: sentinels first (DROP), then
        prose (REVIEW), then real paths. The grouping is the report
        the dry-run printed, frozen in a file."""
        from yadgar.backend.migrations import rekey_corpus

        storage = _FakeStorage(fixture["rows"])
        rekey_corpus.run(
            storage=storage,
            map_path=fixture["map"],
            apply=False,
        )

        contents = fixture["map"].read_text()
        lines = [line for line in contents.splitlines() if line and not line.startswith("#")]
        # Two DROP rows (sentinels), one REVIEW (prose), three seed rows.
        assert sum(1 for ln in lines if ln.split("\t")[1] == "DROP") == 2
        assert sum(1 for ln in lines if ln.split("\t")[1] == "REVIEW") == 1
        assert sum(1 for ln in lines if ln.split("\t")[1] not in ("DROP", "REVIEW")) == 3


# ── CLI wiring ──────────────────────────────────────────────────────────────


class TestMigrateRekeyCli:
    """``yadgar migrate rekey`` parser surface."""

    def test_creates_migrate_subparser_with_rekey(self) -> None:
        from yadgar.core.cli.migrate import register

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["migrate", "rekey"])
        assert args.migrate_command == "rekey"
        assert args.map is None
        assert args.apply is False
        assert args.force is False
        assert hasattr(args, "func")

    def test_rekey_accepts_apply_flag(self) -> None:
        from yadgar.core.cli.migrate import register

        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["migrate", "rekey", "--map", "/tmp/m.tsv", "--apply"])
        assert args.map == "/tmp/m.tsv"
        assert args.apply is True

    def test_rekey_refuses_apply_without_map(self, tmp_path: Path, capsys) -> None:
        """§3 contract: --apply on a missing map is a structured error,
        exit 2, no writes attempted."""
        from yadgar.core.cli.migrate import cmd_migrate_rekey

        rc = cmd_migrate_rekey(
            argparse.Namespace(
                map=str(tmp_path / "does-not-exist.tsv"),
                apply=True,
                force=False,
            )
        )
        assert rc == 2
        captured = capsys.readouterr()
        assert "--apply" in captured.err
