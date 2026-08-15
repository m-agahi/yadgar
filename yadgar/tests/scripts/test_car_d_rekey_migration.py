"""Car D (2026-08-14 identity train, §3) — corpus re-key migration tests.

Three tests, mirroring the task's contract:

  TestParseMap        — map-file TSV parser. Reads Car A's ``parse_map``
                        (which is what ``rekey_corpus`` uses too) and
                        asserts the column-2-is-authoritative contract.

  TestDryRunNoWrites  — the dry-run path. Inject a fixture ``counts``
                        dict so the discovery seam is bypassed; assert
                        ``_forward_admin`` is NOT called on a dry run
                        with counts injected, and the map TSV contains
                        the expected rows.

  TestGroupedReport   — the bucketed report. Sentinel + prose + git
                        + local directories all in one fixture; assert
                        the report buckets them correctly, the
                        collision detector flags same-basename paths,
                        and the map TSV is grouped (drop / review /
                        seed) in the operator's expected order.

The test seams are ``counts=`` (the discover_directories fixture) and
``yadgar.core.forward._forward_admin`` (patched at source so the
migration's thin indirection does not need to be re-patched).
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


class TestDryRunNoWrites:
    """Dry-run contract — discover via counts fixture, write the map.

    The dry-run path is the operator's review tool: it MUST be readable
    end-to-end on a production corpus with no side effects beyond the
    map TSV itself. Asserted by:
      * passing a fixture counts dict (no network round-trip)
      * inspecting the produced map file (rows match the corpus)
      * patching ``_forward_admin`` at the source module and asserting
        it is NOT called on a dry-run (apply=False)
    """

    @pytest.fixture
    def fixture(self, tmp_path: Path) -> dict:
        counts = {
            "/home/max/git/yadgar": {"memory_rows": 2, "wiki_rows": 1},
            "global": {"memory_rows": 1, "wiki_rows": 0},
        }
        return {"counts": counts, "map": tmp_path / "map.tsv"}

    def test_dry_run_does_not_call_forward_admin(self, fixture: dict) -> None:
        """The fixture ``counts`` arg bypasses the discovery seam — so a
        dry run with counts injected MUST NOT call ``_forward_admin``
        at all (no discovery, no apply). Patched at the source module
        because ``rekey_corpus._forward_admin`` does a lazy import."""
        from yadgar.core.migrations import rekey_corpus

        with patch("yadgar.core.forward._forward_admin") as fwd:
            result = rekey_corpus.run(
                counts=fixture["counts"],
                map_path=fixture["map"],
                apply=False,
            )

        assert result["ok"] is True
        fwd.assert_not_called()

    def test_dry_run_writes_map_file(self, fixture: dict) -> None:
        from yadgar.core.migrations import rekey_corpus

        rekey_corpus.run(
            counts=fixture["counts"],
            map_path=fixture["map"],
            apply=False,
        )

        assert fixture["map"].exists()
        contents = fixture["map"].read_text()
        data_rows = [line for line in contents.splitlines() if line and not line.startswith("#")]
        by_source = {line.split("\t")[0]: line.split("\t")[1] for line in data_rows}
        # The 'global' sentinel SPLITS: D4's producer cohort drops, the
        # remainder carries Decision G's owner. It is NOT one DROP row.
        assert by_source["global::memify"] == "DROP"
        assert by_source["global::rest"] == "local/aws-work"
        # Real paths keep their derivation.
        assert "/home/max/git/yadgar" in contents


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
        counts = {
            # git remote — derives owner/repo
            "/home/max/git/yadgar": {"memory_rows": 2, "wiki_rows": 1},
            # local path with no git — local/<basename>
            "/home/max/notes": {"memory_rows": 1, "wiki_rows": 0},
            # second path with the same basename — collision
            "/home/alice/notes": {"memory_rows": 1, "wiki_rows": 0},
            # sentinel — 'global' SPLITS (D4 memify drops, Decision G keeps
            # the remainder); 'system' is a whole-class DROP (D3).
            "global": {"memory_rows": 1, "wiki_rows": 0},
            "system": {"memory_rows": 1, "wiki_rows": 0},
            # free-text prose — REVIEW
            "db_inspect": {"memory_rows": 1, "wiki_rows": 0},
        }
        return {"counts": counts, "map": tmp_path / "map.tsv"}

    def test_buckets_sentinel_prose_git_local(self, fixture: dict) -> None:
        from yadgar.core.migrations import rekey_corpus

        # Patch the filesystem-probing helpers so the bucketed-report
        # contract is tested on a deterministic mapping, not on whatever
        # path happens to exist on the host running the test. CI does not
        # have /home/max/git/yadgar, /home/max/notes, or /home/alice/notes
        # on its filesystem — the live probes would all fall through to
        # ``local/<basename>`` and the ``git`` assertion would fail.
        # The fixture supplies the answers; the test verifies the bucket
        # arithmetic on top of them.
        def fake_walk(directory: str) -> str | None:
            return None  # no .yadgar/project-id for any fixture path

        def fake_origin(directory: str) -> str:
            if directory == "/home/max/git/yadgar":
                return "https://github.com/m-agahi/yadgar.git"
            return ""  # the two notes paths are not git checkouts

        with (
            patch("yadgar.core.identity._walk_project_id_file", side_effect=fake_walk),
            patch("yadgar.core.identity._origin_remote", side_effect=fake_origin),
        ):
            result = rekey_corpus.run(
                counts=fixture["counts"],
                map_path=fixture["map"],
                apply=False,
            )

        report = result["report"]
        assert report["discovered"] == 6  # 6 distinct directory_contexts
        # 'global' emits two cohort rows, so rows > distinct directories.
        assert report["rows_emitted"] == 7
        # This fixture injects no ``cohorts``, so the DESTRUCTIVE cohort's
        # size is unknown. That must be visible: a memify row reading 0 rows
        # would otherwise be read as "nothing dies".
        assert report["cohort_counts_available"] is False

        # Drop bucket — 'system' whole-class (D3) + 'global's memify cohort (D4).
        drop_cohorts = {(r["directory_context"], r["cohort"]) for r in report["drop_rows"]}
        assert drop_cohorts == {("system", ""), ("global", "memify")}

        # Review bucket — the prose row.
        review_dirs = {r["directory_context"] for r in report["review_rows"]}
        assert review_dirs == {"db_inspect"}

        # Seed bucket — the three real paths PLUS Decision G's 'global' remainder.
        seed_dirs = {r["directory_context"] for r in report["seed_rows"]}
        assert seed_dirs == {
            "/home/max/git/yadgar",
            "/home/max/notes",
            "/home/alice/notes",
            "global",
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
        from yadgar.core.migrations import rekey_corpus

        result = rekey_corpus.run(
            counts=fixture["counts"],
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
        from yadgar.core.migrations import rekey_corpus

        rekey_corpus.run(
            counts=fixture["counts"],
            map_path=fixture["map"],
            apply=False,
        )

        contents = fixture["map"].read_text()
        lines = [line for line in contents.splitlines() if line and not line.startswith("#")]
        # Two DROP rows ('system' + 'global::memify'), one REVIEW (prose),
        # four seed rows (three real paths + 'global::rest').
        assert sum(1 for ln in lines if ln.split("\t")[1] == "DROP") == 2
        assert sum(1 for ln in lines if ln.split("\t")[1] == "REVIEW") == 1
        assert sum(1 for ln in lines if ln.split("\t")[1] not in ("DROP", "REVIEW")) == 4


# ── Decision G / D4: the `global` cohort split ──────────────────────────────


class TestGlobalCohortSplit:
    """``global`` is TWO cohorts with TWO decisions — not one DROP.

    The plan (``docs/plans/archive/0047-pr40-remediation-2026-08-10.md``):

      * **D4** (:858) — DELETE only the ``_memify_derive`` sub-cohort, scoped
        by PRODUCER SIGNATURE (tags ``derived`` + ``auto-generated`` AND
        content matching *"are frequently modified together"*), never by
        ``directory_context`` alone.
      * **Decision G** (:207-209) — the REMAINDER gets
        ``project_id = local/aws-work`` PLUS the ``global`` tag. Owner and
        reach are separate axes (§1.4), hence BOTH.

    The backend already implements both correctly
    (``project_backfill._is_memify_global`` is the D4 four-way signature;
    ``_plan_updates`` sets ``add_global_tag`` when the key is the sentinel).
    What was missing is the CORE side handing the backend a mapping that
    contains ``global`` at all — ``_map_target`` classified the whole
    sentinel to ``DROP`` and ``apply_map`` skips ``DROP`` rows, so the
    ``global`` key never reached ``project_id_backfill``.
    """

    @pytest.fixture
    def fixture(self, tmp_path: Path) -> dict:
        return {
            "counts": {
                "global": {"memory_rows": 350, "wiki_rows": 83},
                "system": {"memory_rows": 604, "wiki_rows": 0},
            },
            # Measured live 2026-08-14 (the plan recorded 238 on 2026-08-10 —
            # counts drift, which is exactly why this is injected rather than
            # hardcoded in the module).
            "cohorts": {"memify_global": {"memory_rows": 237, "wiki_rows": 0}},
            "map": tmp_path / "map.tsv",
        }

    def _run(self, fixture: dict):
        from yadgar.core.migrations import rekey_corpus

        return rekey_corpus.run(
            counts=fixture["counts"],
            cohorts=fixture["cohorts"],
            map_path=fixture["map"],
            apply=False,
        )

    # ── THE seam: what apply_map forwards to the backend ────────────────────

    def test_apply_forwards_global_to_local_aws_work(self, fixture: dict) -> None:
        """Decision G, asserted where it bites.

        A test that only greps the TSV would go green on a ``write_map``-only
        fix while ``apply_map`` still skipped the row: the DROP-skip branch is
        in ``apply_map``, not in the generator. So this asserts on the
        ``mapping`` dict actually handed to ``project_id_backfill``."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            rekey_corpus.apply_map(fixture["map"], confirm=True)

        backfill_calls = [c for c in fwd.call_args_list if c.args[0] == "project_id_backfill"]
        assert len(backfill_calls) == 1
        mapping = backfill_calls[0].args[1]["mapping"]
        # The literal sentinel is the backend's key — the ``::cohort`` suffix
        # is a map-file encoding and must never reach the backend.
        assert mapping["global"] == "local/aws-work"
        assert not any("::" in key for key in mapping)

    def test_registry_is_seeded_with_local_aws_work(self, fixture: dict) -> None:
        """Decision G's target is a real registered project (ADR-0223 is
        fail-loud on unknown targets), so the apply must seed it."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            rekey_corpus.apply_map(fixture["map"], confirm=True)

        seeded = {c.args[1]["key"] for c in fwd.call_args_list if c.args[0] == "create_project_row"}
        assert seeded == {"local/aws-work"}

    def test_memify_cohort_never_enters_the_mapping(self, fixture: dict) -> None:
        """D4's cohort is a DELETE, driven by the backend's producer-signature
        predicate. It must contribute no mapping entry of its own."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            rekey_corpus.apply_map(fixture["map"], confirm=True)

        backfill = next(c for c in fwd.call_args_list if c.args[0] == "project_id_backfill")
        assert "system" not in backfill.args[1]["mapping"]
        assert len(backfill.args[1]["mapping"]) == 1

    # ── the map file the operator reviews ───────────────────────────────────

    def test_map_splits_global_into_two_cohort_rows(self, fixture: dict) -> None:
        """The destructive D4 decision gets its OWN reviewable line.

        One ``global -> local/aws-work`` row plus a note would also work (the
        backend predicate is authoritative either way); two rows is chosen so
        an operator reading the map sees that a subset dies."""
        self._run(fixture)

        rows = {
            line.split("\t")[0]: line.split("\t")[1]
            for line in fixture["map"].read_text().splitlines()
            if line and not line.startswith("#")
        }
        assert rows["global::memify"] == "DROP"
        assert rows["global::rest"] == "local/aws-work"
        # The undifferentiated sentinel row is GONE — re-emitting it is the
        # regression that would silently overwrite the user's decision.
        assert "global" not in rows

    def test_system_is_still_dropped(self, fixture: dict) -> None:
        """D3, verbatim: "d3. delete". Guard against the fix over-reaching."""
        self._run(fixture)

        rows = {
            line.split("\t")[0]: line.split("\t")[1]
            for line in fixture["map"].read_text().splitlines()
            if line and not line.startswith("#")
        }
        assert rows["system"] == "DROP"

    def test_cohort_counts_come_from_the_backend(self, fixture: dict) -> None:
        """237 is measured, never hardcoded: the memify count arrives from the
        discovery op and the remainder is arithmetic over it."""
        result = self._run(fixture)

        by_dir = {
            (r["directory_context"], r["cohort"]): r
            for r in result["report"]["drop_rows"] + result["report"]["seed_rows"]
        }
        memify = by_dir[("global", "memify")]
        rest = by_dir[("global", "rest")]
        assert memify["memory_rows"] == 237
        assert memify["wiki_rows"] == 0  # the D4 signature is memory-only
        assert rest["memory_rows"] == 350 - 237
        assert rest["wiki_rows"] == 83

    def test_report_buckets_memify_drop_and_rest_seed(self, fixture: dict) -> None:
        """The two cohorts land in DIFFERENT buckets — that IS the split."""
        result = self._run(fixture)

        drop = {(r["directory_context"], r["cohort"]) for r in result["report"]["drop_rows"]}
        seed = {(r["directory_context"], r["cohort"]) for r in result["report"]["seed_rows"]}
        assert ("global", "memify") in drop
        assert ("system", "") in drop
        assert ("global", "rest") in seed

    def test_discovered_counts_distinct_directories_not_emitted_rows(self, fixture: dict) -> None:
        """``discovered`` keeps meaning "distinct directory_context values";
        the cohort split is reported separately as ``rows_emitted``."""
        result = self._run(fixture)

        assert result["report"]["discovered"] == 2  # 'global' + 'system'
        assert result["report"]["rows_emitted"] == 3  # global x2 + system

    # ── column 2 stays authoritative for the delete cohorts ─────────────────

    def test_retargeting_a_delete_cohort_refuses_to_apply(self, fixture: dict) -> None:
        """The footgun this split creates, closed.

        The D4/D3 deletes are driven by BACKEND predicates, so an operator who
        retargets ``global::memify`` to a project_id would see the rows deleted
        anyway — a column that reads authoritative but is not. So ``apply_map``
        withholds ``confirm_deletes`` and refuses, rather than proceeding with
        a map whose delete rows it cannot honour."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)
        text = (
            fixture["map"]
            .read_text()
            .replace("global::memify\tDROP", "global::memify\tlocal/keepme")
        )
        fixture["map"].write_text(text)

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            result = rekey_corpus.apply_map(fixture["map"], confirm=True)

        assert result["ok"] is False
        assert result["reason"] == "delete_cohort_retargeted"
        assert "global::memify" in result["retargeted"]
        fwd.assert_not_called()  # refuses BEFORE any registry write

    def test_confirm_deletes_is_derived_from_the_map(self, fixture: dict) -> None:
        """A DROP-marked delete cohort in the reviewed map IS the operator's
        confirmation — it is not hardcoded True at the call site."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            rekey_corpus.apply_map(fixture["map"], confirm=True)

        backfill = next(c for c in fwd.call_args_list if c.args[0] == "project_id_backfill")
        assert backfill.args[1]["confirm_deletes"] is True

    def test_run_apply_propagates_the_refusal_to_the_top_level_ok(self, fixture: dict) -> None:
        """The refusal must reach the CLI's exit code.

        ``cmd_migrate_rekey`` returns 0/1 off ``result["ok"]``. A refusal
        nested under ``result["apply"]["ok"]`` and nothing else means
        ``yadgar migrate rekey --apply`` prints "ok": false and exits 0 —
        a gate nothing scripting this command can detect."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)
        fixture["map"].write_text(
            fixture["map"]
            .read_text()
            .replace("global::memify\tDROP", "global::memify\tlocal/keepme")
        )

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}):
            result = rekey_corpus.run(
                counts=fixture["counts"],
                cohorts=fixture["cohorts"],
                map_path=fixture["map"],
                apply=True,
            )

        assert result["ok"] is False
        assert result["reason"] == "delete_cohort_retargeted"
        assert result["apply"]["reason"] == "delete_cohort_retargeted"

    def test_unknown_cohort_suffix_is_not_split(self, tmp_path: Path) -> None:
        """``::`` is only a cohort separator for the cohorts we define.

        ``apply_map`` reads a possibly hand-edited file, and REVIEW rows exist
        so an operator CAN retarget them — including the free-text-prose
        values the plan counts 18 of. Splitting on any ``::`` would silently
        truncate such a key and stamp nothing. (Measured 2026-08-14: zero live
        rows contain ``::`` — this keeps it that way by construction.)"""
        from yadgar.core.migrations import rekey_corpus

        map_path = tmp_path / "map.tsv"
        map_path.write_text("weird::value\tlocal/weird\t3\t0\thand-retargeted\n")

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            rekey_corpus.apply_map(map_path, confirm=True)

        backfill = next(c for c in fwd.call_args_list if c.args[0] == "project_id_backfill")
        assert backfill.args[1]["mapping"] == {"weird::value": "local/weird"}

    def test_tag_suffix_in_column_two_is_tolerated(self, fixture: dict) -> None:
        """Tolerant read, strict write: the generator emits a clean registry
        key, but a hand-edited map annotating the reach axis inline still
        resolves to the same project_id rather than seeding a bogus one."""
        from yadgar.core.migrations import rekey_corpus

        self._run(fixture)
        text = (
            fixture["map"]
            .read_text()
            .replace("global::rest\tlocal/aws-work", "global::rest\tlocal/aws-work +TAG:global")
        )
        fixture["map"].write_text(text)

        with patch("yadgar.core.forward._forward_admin", return_value={"ok": True}) as fwd:
            rekey_corpus.apply_map(fixture["map"], confirm=True)

        backfill = next(c for c in fwd.call_args_list if c.args[0] == "project_id_backfill")
        assert backfill.args[1]["mapping"]["global"] == "local/aws-work"


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


# ── duplicate-key classification (live-envelope regression) ─────────────────


class TestDuplicateRegistryRowIsSkipped:
    """The registry seed must treat an ALREADY-REGISTERED key as ``skipped``.

    Found on the sandbox VM 2026-08-15 running the shipped 5.183.0 wheel
    against backend 5.74.0: ``--apply`` aborted with
    ``registry_seed_failed`` on ``local/argo-workflows`` — one of the
    same-basename collisions the dry-run report itself lists under
    ``basenames_collide``. Two paths deriving one key is a NORMAL,
    reported condition, and the ``skipped`` bucket exists for it.

    The classifier matched ``"DuplicateProject"`` / ``"duplicate"``, but
    ``create_project_row`` returns ``{"ok": False, "error": str(exc)}``
    (ledger.py) and ``DuplicateProjectError``'s message carries no class
    name — so the match could never fire and every duplicate became a
    hard abort. The second consequence is worse than the first: after a
    partial run the already-created rows make EVERY retry fail on its
    first row, so the migration is unresumable.

    These tests build the envelope from the real exception rather than a
    hardcoded string — the fixtures that hardcoded the class name are
    what let this ship.
    """

    def test_real_duplicate_message_classifies_as_skipped(self) -> None:
        from yadgar._shared.storage.sql.errors import DuplicateProjectError
        from yadgar.core.migrations.rekey_corpus import _classify_registry_result

        envelope = {"ok": False, "error": str(DuplicateProjectError("local/argo-workflows"))}
        assert _classify_registry_result(envelope) == "skipped"

    def test_unknown_project_message_still_classifies_as_failed(self) -> None:
        """Guard against over-matching — only duplicates are benign."""
        from yadgar._shared.storage.sql.errors import UnknownProjectError
        from yadgar.core.migrations.rekey_corpus import _classify_registry_result

        envelope = {"ok": False, "error": str(UnknownProjectError("local/nope"))}
        assert _classify_registry_result(envelope) == "failed"

    def test_legacy_class_name_envelope_still_skipped(self) -> None:
        """Back-compat: a backend that DOES name the class stays supported."""
        from yadgar.core.migrations.rekey_corpus import _classify_registry_result

        envelope = {"ok": False, "error": "DuplicateProjectError: local/x"}
        assert _classify_registry_result(envelope) == "skipped"

    def test_project_seed_cli_classifier_agrees(self) -> None:
        """``yadgar project seed`` carries the same match — same live bug."""
        from yadgar._shared.storage.sql.errors import DuplicateProjectError

        dup = {"ok": False, "error": str(DuplicateProjectError("local/argo-workflows"))}
        with patch("yadgar.core.forward._forward_admin", return_value=dup):
            from yadgar.core.cli.project import seed_row

            outcome = seed_row(
                {"project_id": "local/argo-workflows", "note": ""},
                auth_token="t",
            )
        assert outcome == "skipped"

    def test_apply_continues_past_a_duplicate_and_reaches_backfill(self, tmp_path: Path) -> None:
        """End-to-end seam test: a duplicate mid-loop must NOT abort the run.

        Two map rows deriving the same key (the collision shape observed
        on the VM). The second ``create_project_row`` returns the real
        duplicate envelope; the run must still reach
        ``project_id_backfill`` with BOTH directories in the mapping.
        """
        from yadgar._shared.storage.sql.errors import DuplicateProjectError
        from yadgar.core.migrations import rekey_corpus

        map_path = tmp_path / "map.tsv"
        map_path.write_text(
            "# project-id map\n"
            "/home/max/quinyx/argo-workflows\tlocal/argo-workflows\t3\t0\t\n"
            "/home/max/quinyx/infrastructure-services/argo-workflows\t"
            "local/argo-workflows\t2\t0\t\n"
        )

        calls: list[tuple[str, dict]] = []

        def fake_forward(op: str, payload: dict, *, timeout_s: float = 30.0) -> dict:
            calls.append((op, payload))
            if op == "create_project_row":
                if len([c for c in calls if c[0] == "create_project_row"]) == 1:
                    return {"ok": True, "row": {"key": payload["key"]}}
                return {
                    "ok": False,
                    "error": str(DuplicateProjectError(payload["key"])),
                }
            return {"ok": True, "updated": 5, "quarantined": 0}

        with patch.object(rekey_corpus, "_forward_admin", side_effect=fake_forward):
            result = rekey_corpus.apply_map(map_path, confirm=True)

        assert result.get("ok") is not False, result
        ops = [op for op, _ in calls]
        assert "project_id_backfill" in ops, "duplicate aborted the run before backfill"

        backfill_payload = next(p for op, p in calls if op == "project_id_backfill")
        assert backfill_payload["mapping"] == {
            "/home/max/quinyx/argo-workflows": "local/argo-workflows",
            "/home/max/quinyx/infrastructure-services/argo-workflows": "local/argo-workflows",
        }
