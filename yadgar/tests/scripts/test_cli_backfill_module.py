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
):
    return SimpleNamespace(
        directory=directory,
        project=project,
        reslug_adr_pages=reslug_adr_pages,
        adr_rows=adr_rows,
        apply=apply,
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


# ---------------------------------------------------------------------------
# cmd_backfill — --adr-rows
# ---------------------------------------------------------------------------


class TestCmdBackfillAdrRows:
    def test_forwards_project_id_and_directory(self, tmp_path):
        result = {
            "pages_seen": 2,
            "rows_inserted": 2,
            "rows_skipped": 0,
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
            {"project_id": "owner/repo", "directory": str(tmp_path.resolve())},
        )
        assert rc == 0

    def test_prints_result_as_json(self, capsys):
        result = {
            "pages_seen": 1,
            "rows_inserted": 1,
            "rows_skipped": 0,
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
            "rows_skipped": 0,
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
        assert "rows_skipped" in err
        assert "flagged" in err
        assert "gate" in err

    def test_exit_zero_when_gate_exact_match_and_no_flags(self):
        result = {
            "pages_seen": 1,
            "rows_inserted": 1,
            "rows_skipped": 0,
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
            rc = cmd_backfill(_make_args(adr_rows=True))
        assert rc == 0

    def test_exit_nonzero_when_gate_exact_match_false(self):
        result = {
            "pages_seen": 2,
            "rows_inserted": 1,
            "rows_skipped": 0,
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
            rc = cmd_backfill(_make_args(adr_rows=True))
        assert rc != 0

    def test_exit_nonzero_when_flagged_nonempty_even_if_gate_matches(self):
        """The op flags page-only rows as informational (D35b) even on a
        clean run — the CLI is where that becomes an operator-visible
        non-zero exit rather than a silently-swallowed dict key."""
        result = {
            "pages_seen": 1,
            "rows_inserted": 1,
            "rows_skipped": 0,
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
            rc = cmd_backfill(_make_args(adr_rows=True))
        assert rc != 0

    def test_forward_error_propagates(self):
        with _patched(forward_side_effect=RuntimeError("YADGAR_EMBED_URL is not set")):
            with pytest.raises(RuntimeError, match="YADGAR_EMBED_URL"):
                cmd_backfill(_make_args(adr_rows=True))


# ---------------------------------------------------------------------------
# cmd_backfill — no mode flag given
# ---------------------------------------------------------------------------


class TestCmdBackfillNoMode:
    def test_no_flag_returns_nonzero_and_makes_no_forward_call(self):
        with _patched() as (fwd, _resolve):
            rc = cmd_backfill(_make_args())
        fwd.assert_not_called()
        assert rc != 0
