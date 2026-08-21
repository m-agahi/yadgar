"""Tests for yadgar/core/cli/backfill.py — operator ADR backfill subcommand.

task-adr-backfill-prompts (fix 2): ``seed_adr_rows`` and ``reslug`` are
registered backend admin ops (``yadgar.backend.admin_exec._ADMIN_OPS``)
reachable ONLY via POST /admin — there was no MCP tool and no CLI surface,
so an operator could not run either. ``cmd_backfill`` is a thin forwarder,
same pattern as ``drain``/``restore``: it reaches the backend directly via
``yadgar.core.forward._forward_admin`` (no new core-daemon HTTP route —
the same op-name + payload dispatch already serves any registered admin op).

Strategy: patch ``_forward_admin`` and ``resolve_cli_project`` at their
source modules (``cmd_backfill`` lazy-imports them by name, same as
``cmd_drain``).
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from yadgar.core.cli.backfill import cmd_backfill, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(
    directory=".",
    project="owner/repo",
    reslug_adr_pages=False,
    adr_rows=False,
    apply=False,
    skip_adr=None,
):
    return SimpleNamespace(
        directory=directory,
        project=project,
        reslug_adr_pages=reslug_adr_pages,
        adr_rows=adr_rows,
        apply=apply,
        skip_adr=skip_adr,
    )


@contextmanager
def _patched(forward_return=None, forward_side_effect=None, resolved_project="owner/repo"):
    with (
        patch(
            "yadgar.core.cli._shared.resolve_cli_project",
            return_value=resolved_project,
        ) as resolve_mock,
        patch(
            "yadgar.core.forward._forward_admin",
            return_value=forward_return,
            side_effect=forward_side_effect,
        ) as fwd,
    ):
        yield fwd, resolve_mock


# ---------------------------------------------------------------------------
# register() — parser wiring
# ---------------------------------------------------------------------------


class TestRegister:
    def test_creates_backfill_subparser(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill"])
        assert hasattr(args, "func")
        assert args.func is cmd_backfill

    def test_directory_defaults_to_cwd(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill"])
        assert args.directory == "."

    def test_directory_positional_accepted(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill", "/some/dir"])
        assert args.directory == "/some/dir"

    def test_reslug_adr_pages_flag(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill", "--reslug-adr-pages"])
        assert args.reslug_adr_pages is True
        assert args.adr_rows is False
        assert args.apply is False

    def test_adr_rows_flag(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill", "--adr-rows"])
        assert args.adr_rows is True

    def test_apply_flag(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill", "--reslug-adr-pages", "--apply"])
        assert args.apply is True

    def test_project_argument_accepted(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill", "--project", "acme/widget"])
        assert args.project == "acme/widget"


# ---------------------------------------------------------------------------
# cmd_backfill — --reslug-adr-pages
# ---------------------------------------------------------------------------


class TestCmdBackfillReslug:
    def test_dry_run_is_the_default(self):
        """No --apply -> dry_run=True is forwarded. Never apply by accident."""
        with _patched(forward_return={"rewrites": [], "dry_run": True, "collisions": []}) as (
            fwd,
            _resolve,
        ):
            rc = cmd_backfill(_make_args(reslug_adr_pages=True))
        fwd.assert_called_once_with("reslug", {"project_id": "owner/repo", "dry_run": True})
        assert rc == 0

    def test_apply_flag_forwards_dry_run_false(self):
        with _patched(forward_return={"rewrites": [], "dry_run": False, "collisions": []}) as (
            fwd,
            _resolve,
        ):
            cmd_backfill(_make_args(reslug_adr_pages=True, apply=True))
        fwd.assert_called_once_with("reslug", {"project_id": "owner/repo", "dry_run": False})

    def test_prints_result_as_json(self, capsys):
        result = {
            "rewrites": [{"old": "a", "new": "b", "id": 1}],
            "dry_run": True,
            "collisions": [],
        }
        with _patched(forward_return=result):
            cmd_backfill(_make_args(reslug_adr_pages=True))
        out = capsys.readouterr().out
        assert json.loads(out.strip().splitlines()[-1]) == result

    def test_reports_collisions_readably(self, capsys):
        result = {
            "rewrites": [{"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1}],
            "dry_run": True,
            "collisions": [
                {"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1, "occupant_id": 2}
            ],
        }
        with _patched(forward_return=result):
            cmd_backfill(_make_args(reslug_adr_pages=True))
        err = capsys.readouterr().err
        assert "collision" in err.lower()
        assert "yadgar-adr-0001" in err
        assert "owner_repo_adr-0001" in err

    def test_forwards_resolved_project_id(self):
        with _patched(
            forward_return={"rewrites": [], "dry_run": True, "collisions": []},
            resolved_project="acme/widget",
        ) as (
            fwd,
            resolve_mock,
        ):
            cmd_backfill(_make_args(reslug_adr_pages=True, project="acme/widget"))
        resolve_mock.assert_called_once()
        fwd.assert_called_once_with("reslug", {"project_id": "acme/widget", "dry_run": True})

    def test_forward_error_propagates(self):
        """Forward-only: backend-unreachable errors surface, no local fallback."""
        with _patched(forward_side_effect=RuntimeError("YADGAR_EMBED_URL is not set")):
            with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
                cmd_backfill(_make_args(reslug_adr_pages=True))

    def test_apply_with_collisions_exits_nonzero(self):
        """Ledger task 13 defect 2: ``reslug`` has no ``ok`` key at all —
        ``_print_reslug_report`` only PRINTS collisions to stderr, and the
        branch returned 0 unconditionally. A collision on ``--apply`` means
        a page was deliberately left un-reslugged (data is safe, but the
        operator got no signal at all). This is the unread path: an
        operator running ``--apply`` in a script has nobody reading
        stderr, which is exactly the harm."""
        result = {
            "rewrites": [{"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1}],
            "dry_run": False,
            "collisions": [
                {"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1, "occupant_id": 2}
            ],
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(reslug_adr_pages=True, apply=True))
        assert rc != 0

    def test_apply_without_collisions_exits_zero(self):
        """Pin: a clean apply (no collisions) must stay exit 0."""
        result = {
            "rewrites": [{"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1}],
            "dry_run": False,
            "collisions": [],
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(reslug_adr_pages=True, apply=True))
        assert rc == 0

    def test_dry_run_with_collisions_still_exits_zero(self):
        """Pin, not an oversight: a dry run that REPORTS collisions is doing
        its job — the operator is reading the report by definition (that is
        why they ran a dry run instead of --apply). The exit code is the
        only channel on --apply (nobody scripts a dry run and then ignores
        its stdout the way an unattended --apply run can); gating dry-run
        too would make every preview with a pre-existing collision fail the
        same way a real apply-time failure does, which teaches an operator
        to stop trusting the exit code on the run that matters. Mirrors
        ``TestAdrRowsDryRunByDefault.test_dry_run_exits_zero_despite_a_mismatched_gate``."""
        result = {
            "rewrites": [{"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1}],
            "dry_run": True,
            "collisions": [
                {"old": "yadgar-adr-0001", "new": "owner_repo_adr-0001", "id": 1, "occupant_id": 2}
            ],
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(reslug_adr_pages=True))
        assert rc == 0


# ---------------------------------------------------------------------------
# cmd_backfill — --adr-rows
# ---------------------------------------------------------------------------


class TestCmdBackfillAdrRows:
    def test_forwards_project_id_and_directory(self, tmp_path):
        result = {
            "pages_seen": 2,
            "rows_inserted": 2,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "gate": {
                "index_rows": 2,
                "pages_seen": 2,
                "page_type_adr_rows": 2,
                "exact_match": True,
            },
        }
        with _patched(forward_return=result) as (fwd, _resolve):
            rc = cmd_backfill(_make_args(directory=str(tmp_path), adr_rows=True))
        fwd.assert_called_once_with(
            "seed_adr_rows",
            {
                "project_id": "owner/repo",
                "directory": str(tmp_path.resolve()),
                "dry_run": True,
            },
        )
        assert rc == 0

    def test_prints_result_as_json(self, capsys):
        result = {
            "pages_seen": 1,
            "rows_inserted": 1,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "gate": {
                "index_rows": 1,
                "pages_seen": 1,
                "page_type_adr_rows": 1,
                "exact_match": True,
            },
        }
        with _patched(forward_return=result):
            cmd_backfill(_make_args(adr_rows=True))
        out = capsys.readouterr().out
        assert json.loads(out.strip().splitlines()[-1]) == result

    def test_readable_summary_on_stderr(self, capsys):
        result = {
            "pages_seen": 3,
            "rows_inserted": 3,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "gate": {
                "index_rows": 3,
                "pages_seen": 3,
                "page_type_adr_rows": 3,
                "exact_match": True,
            },
        }
        with _patched(forward_return=result):
            cmd_backfill(_make_args(adr_rows=True))
        err = capsys.readouterr().err
        assert "pages_seen" in err
        assert "rows_inserted" in err
        assert "rows_already_present" in err
        assert "rows_failed" in err
        assert "flagged" in err
        assert "gate" in err

    def test_exit_zero_when_gate_exact_match_and_no_flags(self):
        result = {
            "pages_seen": 1,
            "rows_inserted": 1,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "gate": {
                "index_rows": 1,
                "pages_seen": 1,
                "page_type_adr_rows": 1,
                "exact_match": True,
            },
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(adr_rows=True, apply=True))
        assert rc == 0

    def test_exit_nonzero_when_gate_exact_match_false(self):
        result = {
            "pages_seen": 2,
            "rows_inserted": 1,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "gate": {
                "index_rows": 1,
                "pages_seen": 2,
                "page_type_adr_rows": 1,
                "exact_match": False,
            },
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(adr_rows=True, apply=True))
        assert rc != 0

    def test_exit_nonzero_when_flagged_nonempty_even_if_gate_matches(self):
        """The op flags page-only rows as informational (D35b) even on a
        clean run — the CLI is where that becomes an operator-visible
        non-zero exit rather than a silently-swallowed dict key."""
        result = {
            "pages_seen": 1,
            "rows_inserted": 1,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [{"slug": "yadgar-adr-0124", "reason": "no index row provenance"}],
            "supersedes_links": 0,
            "gate": {
                "index_rows": 1,
                "pages_seen": 1,
                "page_type_adr_rows": 1,
                "exact_match": True,
            },
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(adr_rows=True, apply=True))
        assert rc != 0

    def test_apply_exits_zero_on_a_clean_no_op_with_the_legacy_index_gone(self, capsys):
        """Ledger task 311: a 0-insert ``--apply`` no-op must exit 0.

        The gate block is built by the REAL ``_exact_equality_gate`` rather than
        hand-stubbed, because a stubbed ``exact_match: True`` would pass before
        the fix and prove only that the CLI reads a key. The counts are the live
        corpus's (ADR-0429): the legacy ``<project>-adr-index`` page is deleted
        everywhere, so ``index_rows`` is 0 forever, and every project's page
        count already equals its ledger row count — there is no remaining work
        for this op anywhere. Before the fix that exact state produced
        ``exact_match: False`` and ``backfill.py``'s ``return 1``: a no-op the
        operator was told had failed, on the irreversible ``--apply`` path.

        Also asserts the predicate is NAMED on stderr. ``exact_match: true``
        alone cannot distinguish "reconciled the two live counts" from
        "compared three zeros", and the operator reading the report is the
        person who has to tell them apart.
        """
        from yadgar.backend.admin_exec.adr_seed import _exact_equality_gate

        index_rows, pages_seen, page_type_adr_rows = 0, 230, 230
        result = {
            "pages_seen": pages_seen,
            "rows_inserted": 0,
            "rows_already_present": pages_seen,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "gate": {
                "index_rows": index_rows,
                "pages_seen": pages_seen,
                "page_type_adr_rows": page_type_adr_rows,
                "index_absent": index_rows == 0,
                "exact_match": _exact_equality_gate(
                    index_rows=index_rows,
                    pages_seen=pages_seen,
                    page_type_adr_rows=page_type_adr_rows,
                ),
            },
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(adr_rows=True, apply=True))
        err = capsys.readouterr().err
        assert rc == 0, (
            "a converged corpus is not a failed run; the D35c gate's third side "
            "no longer exists, so demanding it made every apply exit non-zero"
        )
        assert "index_absent" in err, "the report must name which predicate produced the verdict"

    def test_forward_error_propagates(self):
        with _patched(forward_side_effect=RuntimeError("YADGAR_EMBED_URL is not set")):
            with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
                cmd_backfill(_make_args(adr_rows=True))

    def test_dry_run_exits_nonzero_when_the_preflight_rejected_the_project(self):
        """Task 176: the preview's exit code is the whole point of the preview.

        ``_preflight_write_guards`` runs the write path's registry guard on the
        dry-run path too, and reports a rejection as ``ok: False``. The dry-run
        ``return 0`` below sits AFTER the ``ok is False`` branch, so a rejected
        preview exits 1 — this pins that ordering, because reversing the two
        lines would restore exactly the defect (a dry run that cannot fail).

        Distinct from ``test_dry_run_exits_zero_despite_a_mismatched_gate``: the
        GATE is a post-mortem that necessarily disagrees on a dry run, so it must
        NOT gate the preview; the PREFLIGHT is a pre-write check that means the
        apply cannot succeed, so it must.
        """
        result = {
            "ok": False,
            "error": (
                "write-path guard 'assert_project_registered' rejected project_id "
                "'owner/repo' (UnknownProjectError: unknown project_id: 'owner/repo')"
            ),
            "resume_after_adr": None,
            "pages_seen": 230,
            "rows_inserted": 0,
            "rows_already_present": 0,
            "rows_failed": 0,
            "rows_skipped_by_request": 0,
            "flagged": [],
            "supersedes_links": 0,
            "plan": [],
            "gate": {
                "index_rows": 230,
                "pages_seen": 230,
                "page_type_adr_rows": 0,
                "exact_match": False,
            },
        }
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(adr_rows=True))
        assert rc == 1, "a dry run that could not validate must not read as a green light"


# ---------------------------------------------------------------------------
# cmd_backfill — no mode flag given
# ---------------------------------------------------------------------------


class TestCmdBackfillNoMode:
    def test_no_flag_returns_nonzero_and_makes_no_forward_call(self):
        with _patched() as (fwd, _resolve):
            rc = cmd_backfill(_make_args())
        fwd.assert_not_called()
        assert rc != 0


# ---------------------------------------------------------------------------
# --skip-adr (Car 2) — ADR-0006's mandated skip, which had no mechanism
# ---------------------------------------------------------------------------


def _seed_result(**over) -> dict:
    base = {
        "pages_seen": 10,
        "rows_inserted": 1,
        "rows_already_present": 0,
        "rows_failed": 0,
        "rows_skipped_by_request": 9,
        "next_id": 10,
        "next_id_basis": "information_schema",
        "flagged": [],
        "supersedes_links": 0,
        "gate": {
            "index_rows": 10,
            "pages_seen": 10,
            "page_type_adr_rows": 10,
            "exact_match": True,
        },
    }
    base.update(over)
    return base


class TestParseSkipAdr:
    """A typo'd skip silently shifts every later ADR onto the wrong ledger id,
    and ``adr.id`` has no undo — so parsing is strict and loud."""

    def test_repeated_flags_accumulate(self):
        from yadgar.core.cli.backfill import _parse_skip_adr

        assert _parse_skip_adr(["1", "5", "6"]) == [1, 5, 6]

    def test_comma_separated_single_flag(self):
        from yadgar.core.cli.backfill import _parse_skip_adr

        assert _parse_skip_adr(["1,5,6,7,8,9"]) == [1, 5, 6, 7, 8, 9]

    def test_mixed_forms_and_adr_prefix(self):
        from yadgar.core.cli.backfill import _parse_skip_adr

        assert _parse_skip_adr(["ADR-0001,0005", "6", "adr-0007"]) == [1, 5, 6, 7]

    def test_none_and_empty_yield_no_skips(self):
        from yadgar.core.cli.backfill import _parse_skip_adr

        assert _parse_skip_adr(None) == []
        assert _parse_skip_adr([""]) == []

    def test_duplicates_collapse(self):
        from yadgar.core.cli.backfill import _parse_skip_adr

        assert _parse_skip_adr(["5", "0005", "ADR-0005"]) == [5]

    def test_garbage_raises(self):
        from yadgar.core.cli.backfill import _parse_skip_adr

        with pytest.raises(ValueError, match="not an ADR number"):
            _parse_skip_adr(["seven"])


class TestSkipAdrWiring:
    def test_register_accepts_repeated_and_comma_forms(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(
            ["backfill", "--adr-rows", "--skip-adr", "1,5", "--skip-adr", "ADR-0006"]
        )
        assert args.skip_adr == ["1,5", "ADR-0006"]

    def test_skip_numbers_reach_the_payload(self):
        with _patched(forward_return=_seed_result()) as (fwd, _resolve):
            cmd_backfill(_make_args(adr_rows=True, skip_adr=["1,5,6", "7", "8", "9"]))
        payload = fwd.call_args[0][1]
        assert payload["skip_adr_numbers"] == [1, 5, 6, 7, 8, 9]

    def test_absent_skip_omits_the_key_entirely(self):
        """An explicit empty list would read as "the operator stated no skips";
        omitting the key leaves the op's own default in charge."""
        with _patched(forward_return=_seed_result(rows_skipped_by_request=0)) as (fwd, _r):
            cmd_backfill(_make_args(adr_rows=True))
        assert "skip_adr_numbers" not in fwd.call_args[0][1]


class TestAdrRowsDryRunByDefault:
    """``--apply`` used to be wired ONLY to the reslug branch, so the
    unrepairable half of this CLI was the half with no preview."""

    def test_default_is_dry_run(self):
        with _patched(forward_return=_seed_result()) as (fwd, _resolve):
            cmd_backfill(_make_args(adr_rows=True))
        assert fwd.call_args[0][1]["dry_run"] is True

    def test_apply_turns_the_dry_run_off(self):
        with _patched(forward_return=_seed_result()) as (fwd, _resolve):
            cmd_backfill(_make_args(adr_rows=True, apply=True))
        assert fwd.call_args[0][1]["dry_run"] is False

    def test_dry_run_exits_zero_despite_a_mismatched_gate(self):
        """Nothing was written, so the gate necessarily disagrees. Exiting
        non-zero on every dry run teaches the operator to ignore the exit code
        on the run that matters."""
        result = _seed_result(
            dry_run=True,
            rows_inserted=0,
            gate={
                "index_rows": 230,
                "pages_seen": 236,
                "page_type_adr_rows": 6,
                "exact_match": False,
            },
        )
        with _patched(forward_return=result):
            assert cmd_backfill(_make_args(adr_rows=True)) == 0

    def test_dry_run_plan_is_printed(self, capsys):
        result = _seed_result(
            dry_run=True,
            rows_inserted=0,
            plan=[
                {"adr": "ADR-0010", "slug": "yadgar-adr-0010", "planned_id": 10},
                {"adr": "ADR-0011", "slug": "yadgar-adr-0011", "planned_id": 11},
            ],
        )
        with _patched(forward_return=result):
            cmd_backfill(_make_args(adr_rows=True))
        err = capsys.readouterr().err
        assert "[DRY RUN]" in err
        assert "PLAN: ADR-0010 -> id 10" in err
        assert "PLAN: ADR-0011 -> id 11" in err


class TestAdrRowsFailureIsVisible:
    def test_structural_abort_exits_nonzero_and_names_the_resume_point(self, capsys):
        result = _seed_result(
            ok=False,
            error="the ledger surface cannot take an ADR insert (AttributeError: ...)",
            resume_after_adr=57,
            rows_inserted=47,
        )
        with _patched(forward_return=result):
            rc = cmd_backfill(_make_args(adr_rows=True, apply=True))
        assert rc != 0
        err = capsys.readouterr().err
        assert "ABORTED" in err
        assert "resume after ADR-57" in err

    def test_rows_failed_exits_nonzero_even_with_a_matching_gate(self):
        """A per-row failure leaves a permanent hole in the numbering; it must
        not be absorbed by an otherwise-clean report."""
        with _patched(forward_return=_seed_result(rows_failed=3)):
            assert cmd_backfill(_make_args(adr_rows=True, apply=True)) != 0
