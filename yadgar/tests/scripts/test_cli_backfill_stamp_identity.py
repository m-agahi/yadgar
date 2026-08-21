"""Car 1 — ``yadgar backfill --stamp-identity``, the operator surface.

``stamp_project_id`` is a registered backend admin op reachable ONLY via
``POST /admin``. Without a CLI arm an operator cannot run it at all — the
same gap ``--adr-rows`` and ``--reslug-adr-pages`` were filed for.

The exit-code contract mirrors the ``--adr-rows`` branch exactly, and the
mirroring is the point: a preflight failure exits NON-ZERO even on a dry run,
because the whole reason the preview runs the write path's guards (Car 19 /
task 176) is so an operator learns "this cannot be applied" from the preview
rather than from a half-finished apply.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from yadgar.core.cli.backfill import cmd_backfill, register


def _args(**overrides):
    base = {
        "directory": ".",
        "project": "owner/repo",
        "reslug_adr_pages": False,
        "adr_rows": False,
        "stamp_identity": True,
        "mapping_file": None,
        "apply": False,
        "skip_adr": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _manifest(**overrides):
    base = {
        "ok": True,
        "error": None,
        "dry_run": True,
        "applied": False,
        "directory_map": {"/home/max/git/yadgar": "m-agahi/yadgar"},
        "map_conflicts": [],
        "reach_markers": ["global"],
        "guards": {"names": ["assert_project_registered"], "checked_project_ids": ["a/b"]},
        "plan": {},
        "tables": {
            "entity": {
                "rows_seen": 3,
                "rows_stamped": 2,
                "rows_cross_project": 0,
                "rows_undecidable": 1,
                "undecidable_by_reason": {"shared_by_construction": 1},
                "undecidable_sample": [],
                "undecidable_sample_truncated": False,
                "cross_project": [],
            }
        },
        "dangling_relationships": {
            "count": 2,
            "by_type": {"derived_from": 2},
            "rows": [],
            "rows_truncated": False,
        },
        "totals": {
            "rows_seen": 3,
            "rows_stamped": 2,
            "rows_cross_project": 0,
            "rows_undecidable": 1,
        },
    }
    base.update(overrides)
    return base


@contextmanager
def _patched(forward_return):
    with (
        patch("yadgar.core.cli._shared.resolve_cli_project", return_value="owner/repo"),
        patch("yadgar.core.forward._forward_admin", return_value=forward_return) as fwd,
    ):
        yield fwd


class TestRegister:
    def test_flags_are_registered(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill", "--stamp-identity", "--mapping-file", "/tmp/m.json"])
        assert args.stamp_identity is True
        assert args.mapping_file == "/tmp/m.json"

    def test_stamp_identity_defaults_off(self):
        root = argparse.ArgumentParser()
        subs = root.add_subparsers()
        register(subs)
        args = root.parse_args(["backfill"])
        assert args.stamp_identity is False
        assert args.mapping_file is None


class TestForwarding:
    def test_forwards_the_registered_op_name(self):
        with _patched(_manifest()) as fwd:
            assert cmd_backfill(_args()) == 0
        op, payload = fwd.call_args[0]
        assert op == "stamp_project_id"

    def test_dry_run_is_the_default(self):
        with _patched(_manifest()) as fwd:
            cmd_backfill(_args())
        assert fwd.call_args[0][1]["dry_run"] is True

    def test_apply_flag_turns_off_dry_run(self):
        with _patched(_manifest(dry_run=False, applied=True)) as fwd:
            cmd_backfill(_args(apply=True))
        assert fwd.call_args[0][1]["dry_run"] is False

    def test_mapping_file_is_read_and_forwarded(self, tmp_path):
        path = tmp_path / "map.json"
        path.write_text(json.dumps({"/a": "o/r"}))
        with _patched(_manifest()) as fwd:
            assert cmd_backfill(_args(mapping_file=str(path))) == 0
        assert fwd.call_args[0][1]["mapping"] == {"/a": "o/r"}

    def test_absent_mapping_key_when_no_file(self):
        with _patched(_manifest()) as fwd:
            cmd_backfill(_args())
        assert "mapping" not in fwd.call_args[0][1]


class TestExitCodes:
    def test_clean_dry_run_exits_zero(self):
        with _patched(_manifest()):
            assert cmd_backfill(_args()) == 0

    def test_preflight_failure_exits_nonzero_on_a_dry_run(self):
        """The preview is where a guard failure must become an exit code."""
        rejected = _manifest(ok=False, error="write-path guard rejected project_id 'x/y'")
        with _patched(rejected):
            assert cmd_backfill(_args()) == 1

    def test_failed_apply_exits_nonzero(self):
        rejected = _manifest(ok=False, dry_run=False, error="storage_unavailable")
        with _patched(rejected):
            assert cmd_backfill(_args(apply=True)) == 1

    def test_successful_apply_exits_zero(self):
        with _patched(_manifest(dry_run=False, applied=True)):
            assert cmd_backfill(_args(apply=True)) == 0

    def test_unreadable_mapping_file_exits_two_without_forwarding(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        with _patched(_manifest()) as fwd:
            assert cmd_backfill(_args(mapping_file=str(path))) == 2
        fwd.assert_not_called()


class TestReport:
    def test_report_names_the_buckets_an_operator_decides_on(self, capsys):
        with _patched(_manifest()):
            cmd_backfill(_args())
        err = capsys.readouterr().err
        assert "rows_stamped=2" in err
        assert "rows_undecidable=1" in err
        assert "shared_by_construction" in err
        assert "dangling_relationships=2" in err

    def test_report_names_conflicts_and_reach_markers_separately(self, capsys):
        manifest = _manifest(
            map_conflicts=[{"directory": "/shared", "project_ids": ["a/b", "c/d"]}]
        )
        with _patched(manifest):
            cmd_backfill(_args())
        err = capsys.readouterr().err
        assert "CONFLICT: /shared" in err
        assert "reach_markers" in err

    def test_machine_readable_manifest_goes_to_stdout(self, capsys):
        with _patched(_manifest()):
            cmd_backfill(_args())
        assert json.loads(capsys.readouterr().out)["ok"] is True
