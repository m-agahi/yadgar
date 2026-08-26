"""Car C8-3 / Task #241 — TSV note→display_name truncation (VARCHAR(64)) + log reason.

Defect (ledger task #241):
  ``project_seed`` reads the TSV ``note`` column and writes it into the
  ``project.display_name`` column (VARCHAR(64)). The current slice is
  ``[:255]`` (cli/project.py:202-203), which lets a 200-char note through
  and trips a MariaDB ``DataError`` (1406, "Data too long for column
  'display_name'"). The exception is then swallowed in the ledger wrapper
  (ledger.py:799-801) into a bare ``{"ok": False, "error": str(exc)}`` —
  the operator path counts it as ``failed: N`` with no per-row reason,
  so a "failed: 12" counter is unactionable until someone digs into the
  daemon log.

Fix scope:
  1. CLI truncates ``note`` to 64 chars BEFORE writing display_name, and
     logs the truncation reason (so 65 / 200 char notes land as "created",
     not "failed").
  2. The ledger wrapper's DataError swallow now also surfaces the
     underlying reason into the ``error`` envelope — the operator path
     already prints ``FAIL: <key>: <err>`` (cli/project.py:227), so a
     reason-bearing error envelope makes the counter actionable.

Scope 1 shipped in C8-3. SCOPE 2 DID NOT (ledger task #381): that commit
touched only ``cli/project.py`` and this file, and the test written for it
asserted the round-trip of a message the test itself supplied — which the
unchanged ``str(exc)`` wrapper satisfied. Car Q wrote the production half
(``driver_errors.driver_error_detail``) and re-pinned it against a real
``sqlalchemy.exc.DataError``; see ``TestLedgerCreateProjectRowErrorEnrichment``.

This file pins both behaviours. RED → GREEN: written before the fix.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Display_name column is VARCHAR(64). The boundary is 64 chars (no
# truncation), the off-by-one is 65 chars (one over, must be truncated),
# and a "very long" fixture is 200 chars (the original defect size).
DISPLAY_NAME_MAX = 64


class TestSeedRowNoteTruncation:
    """``seed_row`` must NOT silently fail on long notes — it must
    truncate to 64 chars and log the reason."""

    def _row(self, note: str | None) -> dict:
        return {
            "source_directory": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "memory_rows": "1",
            "wiki_rows": "0",
            "note": note,
        }

    def _capture(self, note: str | None, capture_display_name: bool = True) -> tuple[str, object]:
        """Run ``seed_row`` with a mocked backend; return ``(status,
        captured_display_name_or_calls)``."""
        from yadgar.core.cli.project import seed_row

        captured: dict = {}

        def fake_forward(_op: str, payload: dict, **_kw):
            if capture_display_name:
                captured["display_name"] = payload.get("display_name")
            return {"ok": True, "row": {"key": payload.get("key")}}

        with patch("yadgar.core.forward._forward_admin", side_effect=fake_forward):
            status = seed_row(self._row(note), auth_token="x")

        return status, captured.get("display_name")

    def test_note_at_boundary_64_chars_succeeds_untruncated(self) -> None:
        """64-char note (== VARCHAR(64) width) lands as ``created`` and is
        forwarded verbatim — boundary, not over."""
        note = "a" * 64
        status, sent = self._capture(note)
        assert status == "created"
        assert sent == note
        assert len(sent) == 64  # type: ignore[arg-type]

    def test_note_over_boundary_65_chars_truncated_to_64(self) -> None:
        """65-char note (== VARCHAR(64) + 1) lands as ``created`` and is
        truncated to 64 chars. Before the fix this turned into ``failed:
        1`` with no reason — that is the regression."""
        note = "a" * 65
        status, sent = self._capture(note)
        assert status == "created", "65-char note must NOT silently fail"
        assert sent == "a" * 64
        assert len(sent) == 64  # type: ignore[arg-type]

    def test_note_200_chars_truncated_to_64(self) -> None:
        """The defect size from task #241: a 200-char note (the kind that
        landed in production on 2026-08-20) must truncate cleanly."""
        note = "x" * 200
        status, sent = self._capture(note)
        assert status == "created"
        assert sent == "x" * 64
        assert len(sent) == 64  # type: ignore[arg-type]

    def test_note_none_not_forwarded_as_display_name(self) -> None:
        """An empty / missing note must NOT produce a ``display_name`` key
        at all — keeps the schema's NULL-on-omit contract intact."""
        status, sent = self._capture(None)
        assert status == "created"
        assert sent is None, "missing note must not surface as a display_name key"

    def test_note_empty_string_treated_as_no_note(self) -> None:
        """Empty string note (vs None) — same contract: no display_name."""
        status, sent = self._capture("")
        assert status == "created"
        # Either None or absent — the contract is "don't set the field".
        assert sent in (None, "")


class TestSeedRowTruncationLogsReason:
    """Truncation is observable: it logs a WARNING so the operator knows
    WHY a long note came through as 64 chars (and which row it was)."""

    def test_truncation_emits_log_record(self, caplog) -> None:
        """65+ char note logs a WARNING naming the key + the truncation
        length, so the daemon log has the reason for the slice."""
        import logging

        from yadgar.core.cli.project import seed_row

        row = {
            "source_directory": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "memory_rows": "1",
            "wiki_rows": "0",
            "note": "a" * 200,
        }

        with caplog.at_level(logging.WARNING, logger="yadgar"):
            with patch(
                "yadgar.core.forward._forward_admin",
                return_value={"ok": True, "row": {"key": "m-agahi/yadgar"}},
            ):
                status = seed_row(row, auth_token="x")

        assert status == "created"
        # The log must name the key AND the truncation reason — that's
        # the whole point of this car. Surface the reason so it isn't
        # only visible to a daemon-log reader.
        log_text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "m-agahi/yadgar" in log_text, f"truncation log must name the key; got: {log_text!r}"
        assert "truncat" in log_text.lower(), (
            f"truncation log must say it was truncated; got: {log_text!r}"
        )

    def test_no_truncation_log_on_short_note(self, caplog) -> None:
        """A 64-char note (= boundary, no truncation) emits no truncation
        log — only over-length notes do."""
        import logging

        from yadgar.core.cli.project import seed_row

        row = {
            "source_directory": "/home/max/git/yadgar",
            "project_id": "m-agahi/yadgar",
            "memory_rows": "1",
            "wiki_rows": "0",
            "note": "a" * 64,
        }

        with caplog.at_level(logging.WARNING, logger="yadgar"):
            with patch(
                "yadgar.core.forward._forward_admin",
                return_value={"ok": True, "row": {"key": "m-agahi/yadgar"}},
            ):
                seed_row(row, auth_token="x")

        log_text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "truncat" not in log_text.lower(), (
            f"64-char boundary note must NOT log truncation; got: {log_text!r}"
        )


class TestSeedRowFailedCounterSemantics:
    """``failed: N`` must count ONLY genuinely bad rows — a long note is
    no longer bad (it truncates). The 2026-08-20 incident had a real
    DataError counted alongside transient noise; the fix separates them."""

    def _row(self, project_id: str, note: str) -> dict:
        return {
            "source_directory": "/home/max/git/yadgar",
            "project_id": project_id,
            "memory_rows": "1",
            "wiki_rows": "0",
            "note": note,
        }

    def test_long_note_does_not_count_as_failed(self, tmp_path) -> None:
        """A 200-char note + a genuinely-bad row in the same map: only
        the genuinely-bad one increments ``failed``. Before the fix the
        200-char note also incremented ``failed`` (silent DataError)."""
        from yadgar.core.cli.project import cmd_project_seed

        f = tmp_path / "map.tsv"
        f.write_text(
            "/home/x\towner/longnote\t1\t0\t" + ("a" * 200) + "\n"
            "/home/y\towner/genuinefail\t1\t0\tnormal\n"
        )

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=[
                # longnote: 200-char note now truncates, so this is the
                # "ok" path the fix produces.
                {"ok": True, "row": {"key": "owner/longnote"}},
                # genuinefail: backend says "engine #2 not composed".
                {"ok": False, "error": "engine #2 not composed"},
            ],
        ):
            rc = cmd_project_seed(_argparse_namespace := _ns(str(f)))

        assert rc == 1, "genuine failure must exit non-zero"
        # We can't see counts here without capturing stdout, but the rc
        # is the operator-facing signal that a row genuinely failed. A
        # silent-DataError regression would flip rc back to 0 (because
        # both rows would have been "failed" but the bug also returned
        # 0 in pre-C11-#88 builds — pinned by test_car_a_project_seed).

    def test_only_long_note_in_map_exits_zero(self, tmp_path) -> None:
        """Pure long-note map: zero failures, exit 0. Before the fix this
        exited 1 (DataError counted as failure)."""
        from yadgar.core.cli.project import cmd_project_seed

        f = tmp_path / "map.tsv"
        f.write_text("/home/x\towner/longnote\t1\t0\t" + ("a" * 200) + "\n")

        with patch(
            "yadgar.core.forward._forward_admin",
            return_value={"ok": True, "row": {"key": "owner/longnote"}},
        ):
            rc = cmd_project_seed(_ns(str(f)))

        assert rc == 0, "long note now truncates and succeeds — exit must be 0"


def _ns(map_path: str):
    """Tiny argparse.Namespace factory for the cmd_project_seed tests."""
    import argparse

    return argparse.Namespace(map=map_path)


# ── Ledger wrapper — DataError reason surfaces into the error envelope ───────


class TestLedgerCreateProjectRowErrorEnrichment:
    """``ledger.create_project_row``'s error envelope IS the operator's only
    signal (``cli/project.py``'s ``FAIL: <key>: <err>`` print), so it must
    carry the driver's own reason — the MySQL errno and the column the
    server named — not SQLAlchemy's full ``str(exc)``.

    CAR Q NOTE — this class shipped in C8-3 pinning nothing. Its original
    test raised a duck-typed exception whose ``str()`` was a message the test
    itself supplied, then asserted that same message came back out. Since the
    wrapper did ``str(exc)``, it passed at the merge base while
    ``ledger.create_project_row`` was byte-identical to master; the
    production half named in this module's "Fix scope 2" was never written
    (``git show 7419084b --stat`` touches only ``cli/project.py`` and this
    file). The tests below construct a REAL ``sqlalchemy.exc.DataError``
    wrapping a driver exception with ``args == (1406, "Data too long ...")``,
    which is the shape the seed path actually produces, and assert on what
    the wrapper EXTRACTS from it rather than on what the test handed in.
    """

    @staticmethod
    def _real_data_error(note_value: str):
        """A genuine ``sqlalchemy.exc.DataError`` around a driver exception.

        The driver half is duck-typed (pymysql is not a test dependency) but
        its ``args`` tuple is the real ``(errno, message)`` shape every
        MySQL/MariaDB DBAPI raises, and the SQLAlchemy half is the real
        class — which is what makes ``str(exc)`` carry the ``[SQL: ...]`` /
        ``[parameters: ...]`` tail this test distinguishes against.
        """
        sqlalchemy_exc = pytest.importorskip(
            "sqlalchemy.exc",
            reason="ledger driver-error extraction needs the `sql` extra installed",
        )

        class _PyMySQLDataError(Exception):
            pass

        orig = _PyMySQLDataError(1406, "Data too long for column 'display_name' at row 1")
        return sqlalchemy_exc.DataError(
            "INSERT INTO project (`key`, display_name) VALUES (%s, %s)",
            ("m-agahi/yadgar", note_value),
            orig,
        )

    async def _envelope(self, exc: BaseException) -> dict:
        from yadgar.backend.admin_exec import ledger

        class _FakeStorage:
            async def create_project_row(self, **_kw):
                raise exc

        with patch.object(ledger, "_get_sql_storage", return_value=_FakeStorage()):
            return await ledger.create_project_row(
                {"key": "m-agahi/yadgar", "kind": "git", "display_name": "x" * 200}
            )

    @pytest.mark.asyncio
    async def test_dataerror_envelope_carries_driver_errno_and_column(self) -> None:
        """The envelope names the MySQL errno and the offending column.

        ``db_errno`` is the half that cannot pass by accident: nothing in the
        exception's ``str()`` produces that key, so a wrapper that still does
        ``str(exc)`` fails here regardless of what the message happens to say.
        """
        note_value = "x" * 200
        result = await self._envelope(self._real_data_error(note_value))

        assert result["ok"] is False
        assert result.get("db_errno") == 1406, result
        err = result.get("error", "")
        assert "1406" in err, err
        assert "display_name" in err, err
        assert "Data too long" in err, err

    @pytest.mark.asyncio
    async def test_dataerror_envelope_omits_sql_and_bound_parameters(self) -> None:
        """The envelope must not echo the statement or the bad value back.

        ``str()`` on a SQLAlchemy ``DBAPIError`` appends ``[SQL: ...]`` and
        ``[parameters: ...]``, so the pre-Car-Q wrapper put the whole 200-char
        note — the very value that was too long — into the operator-facing
        error string, plus a "Background on this error at" URL. This asserts
        the raw form really does carry that tail (so the test is not vacuous)
        and that the envelope does not.
        """
        note_value = "x" * 200
        exc = self._real_data_error(note_value)

        raw = str(exc)
        assert "[SQL:" in raw and "[parameters:" in raw, (
            "fixture is not a real DBAPIError — the omission assertion below "
            f"would be vacuous; got: {raw!r}"
        )
        assert note_value in raw

        result = await self._envelope(exc)
        err = result.get("error", "")
        assert "[SQL:" not in err, err
        assert "[parameters:" not in err, err
        assert note_value not in err, err

    @pytest.mark.asyncio
    async def test_non_driver_exception_still_reports_its_message(self) -> None:
        """A plain exception (no ``.orig``) degrades to ``str(exc)``.

        The extraction must not swallow the reason for every error that is
        not a wrapped driver failure, and must not invent a ``db_errno``.
        """
        result = await self._envelope(RuntimeError("engine went away"))

        assert result["ok"] is False
        assert result.get("error") == "engine went away"
        assert "db_errno" not in result
