"""Car 19 (ledger task 176) — a clean ``--adr-rows`` dry run must predict ``--apply``.

THE DEFECT
----------
``seed_adr_rows``'s dry run stops at ``_seed_one_page``'s ``if run.dry_run:``
branch, which returns BEFORE ``_insert_one_row`` — so it never reaches
``MariaStorageEngine.create_adr_row``, and therefore never reaches the ADR-0078
project-registry guard (``await self.assert_project_registered(project_id)``)
that lives inside it. Every other call the dry run makes is an unguarded READ
(``list_adr_rows``, ``information_schema.TABLES``), so there is no input the
dry run rejects that the apply would also reject.

That is not a dry run. It is a green light with no braking distance, and it
has already fired once: the 2026-08-18 VM rehearsal (ledger task 175) ran the
apply against a VM whose ``project`` registry had never been seeded and all 230
pages raised ``UnknownProjectError``. The preview said nothing.

``sql_storage is None`` (engine #2 not composed) is the SAME defect a second
time: every read helper returns its empty default, the dry run reports clean,
and the apply dies on the first ``create_adr_row`` against a ``None`` handle.

THE INVARIANT (the durable half)
--------------------------------
``TestGuardParityInvariant`` fails if a guard is ever added to
``create_adr_row`` without the dry-run preflight gaining it too. It ASTs the
method and compares its direct ``self.X()`` calls against
``adr_seed._WRITE_PATH_GUARDS``, which is the same tuple the preflight
iterates — so extending the tuple to satisfy the test also makes the preflight
run the new guard. It cannot be satisfied cosmetically.

Known limit, stated rather than hidden: a future guard written as a
module-level call (``some_guard(pid, engine=self)``) rather
than a ``self.`` method call escapes the AST rule. The invariant is deliberately
scoped to the ADR seed's write path; it does not police ``create_task_row``.

NO ``row_inserter`` SEAM IN THIS FILE. ``test_adr_seed_ledger_ids.py``'s own
docstring records why: every existing unit test injects that seam, which
bypasses the real ``create_adr_row`` entirely, "which is why four defects lived
here at once". A test that injects it proves nothing about the production path,
so these tests leave it ``None`` and put a real-shaped ledger fake behind it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from yadgar.backend.admin_exec import adr_seed

_PROJECT_ID = "m-agahi/yadgar"
_DIRECTORY = "/home/max/git/yadgar"


class _UnknownProjectError(RuntimeError):
    """Stand-in for the ADR-0078 registry guard's error.

    Deliberately not ``AttributeError``/``TypeError`` — task 175's whole point
    is that the seed must not recognise a structural fault by exception type.
    """


class _WikiPages:
    """Minimal wiki handle — ``list_wiki_pages`` and nothing else."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def list_wiki_pages(self, slug_prefix: str, limit: int = 10000) -> list[dict[str, Any]]:
        return [p for p in self._pages if str(p.get("slug", "")).startswith(slug_prefix)]


def _two_pages() -> _WikiPages:
    return _WikiPages(
        [
            {"slug": "yadgar-adr-0001", "content": "# ADR-0001: first\nbody", "tags": []},
            {"slug": "yadgar-adr-0002", "content": "# ADR-0002: second\nbody", "tags": []},
        ]
    )


class _LedgerFake:
    """Ledger handle carrying the SAME surface the real write path needs.

    ``assert_project_registered`` is the guard; ``create_adr_row`` records
    whether the write path was reached at all — a dry run that touches it has
    stopped being a dry run.
    """

    def __init__(self, *, registered: bool = True) -> None:
        self.registered = registered
        self.guard_calls: list[str] = []
        self.guard_refresh: list[bool] = []
        self.inserts: list[dict[str, Any]] = []

    async def assert_project_registered(self, project_id: str, *, refresh: bool = True) -> None:
        # ``refresh`` mirrors the real guard (task 384/385 follow-up): True bumps
        # ``project.last_validated_at``. Recorded so the dry-run test can assert
        # the preview asked for a check WITHOUT the write.
        self.guard_calls.append(project_id)
        self.guard_refresh.append(refresh)
        if not self.registered:
            raise _UnknownProjectError(f"unknown project_id: {project_id!r}")

    def list_adr_rows(self, **_kw: object) -> list[dict[str, Any]]:
        return []

    async def create_adr_row(self, **kw: Any) -> dict[str, Any]:
        await self.assert_project_registered(str(kw.get("project_id", "")))
        self.inserts.append(dict(kw))
        return {"id": 100 + len(self.inserts)}

    async def set_adr_body_slug(self, **_kw: object) -> None:
        return None


async def _run(*, sql_storage: Any, dry_run: bool) -> dict[str, object]:
    return await adr_seed.seed_adr_rows(
        project_id=_PROJECT_ID,
        directory=_DIRECTORY,
        storage=_two_pages(),
        sql_storage=sql_storage,
        dry_run=dry_run,
    )


class TestDryRunRunsTheWritePathGuards:
    """The preview must reject exactly what the apply rejects."""

    @pytest.mark.asyncio
    async def test_dry_run_rejects_an_unregistered_project(self) -> None:
        """The red test. A dry run that only ever exits 0 reproduces the defect."""
        ledger = _LedgerFake(registered=False)

        result = await _run(sql_storage=ledger, dry_run=True)

        assert result.get("ok") is False, (
            "the dry run passed an input the apply path rejects — this is the defect"
        )
        assert _PROJECT_ID in str(result.get("error", "")), (
            "the error must name the offending project_id, not just fail"
        )
        assert ledger.guard_calls, "the registry guard was never reached on the dry-run path"
        assert not ledger.inserts, "a dry run must not write"

    @pytest.mark.asyncio
    async def test_apply_rejects_the_same_input(self) -> None:
        """Parity, stated from the other side: same fake, same verdict."""
        ledger = _LedgerFake(registered=False)

        result = await _run(sql_storage=ledger, dry_run=False)

        assert result.get("ok") is False
        assert not ledger.inserts

    @pytest.mark.asyncio
    async def test_dry_run_and_apply_agree_on_a_registered_project(self) -> None:
        """The guard must not turn every dry run into a failure — that teaches
        the operator to ignore the exit code on the run that matters."""
        dry = await _run(sql_storage=_LedgerFake(registered=True), dry_run=True)
        assert dry.get("ok") is not False
        assert dry["plan"], "a passing dry run still owes the operator its plan"

        applied_ledger = _LedgerFake(registered=True)
        applied = await _run(sql_storage=applied_ledger, dry_run=False)
        assert applied.get("ok") is not False
        assert applied["rows_inserted"] == 2
        assert len(applied_ledger.inserts) == 2

    @pytest.mark.asyncio
    async def test_dry_run_reports_an_absent_ledger_handle(self) -> None:
        """Engine #2 not composed is the same class of lie.

        Every read helper defaults to empty on a ``None`` handle, so the preview
        reads clean while the apply dies on the first ``create_adr_row`` against
        ``None`` (an ``AttributeError``, which ``_insert_one_row`` converts to a
        ``_StructuralSeedError``).
        """
        result = await _run(sql_storage=None, dry_run=True)

        assert result.get("ok") is False, (
            "a dry run with no ledger handle cannot have verified anything"
        )
        assert "engine" in str(result.get("error", "")).lower()

    @pytest.mark.asyncio
    async def test_a_ledger_missing_a_guard_method_is_a_structural_fault(self) -> None:
        """A handle that cannot run the guard must not read as "guard passed".

        This is the wrong-engine defect's shape (task 168): the SurrealDB handle
        has zero ADR methods, and every ``AttributeError`` it raised was absorbed
        into a success-shaped report.
        """

        class _NoGuard:
            def list_adr_rows(self, **_kw: object) -> list[dict[str, Any]]:
                return []

        result = await _run(sql_storage=_NoGuard(), dry_run=True)

        assert result.get("ok") is False
        assert "assert_project_registered" in str(result.get("error", ""))

    @pytest.mark.asyncio
    async def test_a_failing_preflight_still_returns_the_full_report_shape(self) -> None:
        """``_print_seed_report`` reads these keys unconditionally; a partial
        dict would print ``pages_seen=None`` at the moment the operator most
        needs the numbers."""
        result = await _run(sql_storage=_LedgerFake(registered=False), dry_run=True)

        for key in (
            "pages_seen",
            "rows_inserted",
            "rows_already_present",
            "rows_failed",
            "rows_skipped_by_request",
            "next_id",
            "next_id_basis",
            "flagged",
            "gate",
        ):
            assert key in result, f"result dict lost {key!r} on the preflight-failure path"
        assert result["rows_inserted"] == 0


class TestPreflightIsDrivenByTheGuardTuple:
    """Extending ``_WRITE_PATH_GUARDS`` must actually run the new guard."""

    @pytest.mark.asyncio
    async def test_every_named_guard_is_invoked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[str] = []

        class _TwoGuards(_LedgerFake):
            # The narrower signature (no ``refresh``) is deliberate and is now
            # itself under test: the preflight must not hand the keyword to a
            # guard that does not take it. mypy is correct that this is not
            # substitutable for the base — that is the scenario.
            async def assert_project_registered(  # type: ignore[override]
                self, project_id: str
            ) -> None:
                called.append("assert_project_registered")

            async def assert_something_else(self, project_id: str) -> None:
                called.append("assert_something_else")

        monkeypatch.setattr(
            adr_seed,
            "_WRITE_PATH_GUARDS",
            ("assert_project_registered", "assert_something_else"),
        )
        await _run(sql_storage=_TwoGuards(), dry_run=True)

        assert called == ["assert_project_registered", "assert_something_else"], (
            "_WRITE_PATH_GUARDS is a list the preflight iterates, not documentation"
        )


def _create_adr_row_self_calls() -> set[str]:
    """Names of the direct ``self.X(...)`` calls inside ``create_adr_row``.

    ``self._engine.begin()`` is an attribute-of-attribute call, so it is not a
    direct ``self.`` call and does not appear here — only the guards do.
    """
    from yadgar._shared.storage.sql.mariadb import MariaStorageEngine

    src_file = inspect.getsourcefile(MariaStorageEngine)
    assert src_file is not None
    tree = ast.parse(Path(src_file).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == MariaStorageEngine.__name__):
            continue
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                sub.name == "create_adr_row"
            ):
                return {
                    call.func.attr
                    for call in ast.walk(sub)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "self"
                }
    raise AssertionError("create_adr_row not found in MariaStorageEngine source")


class TestGuardParityInvariant:
    """The durable deliverable: a new write-path guard cannot skip the dry run."""

    def test_write_path_guards_matches_create_adr_row(self) -> None:
        assert _create_adr_row_self_calls() == set(adr_seed._WRITE_PATH_GUARDS), (
            "create_adr_row's guards and adr_seed._WRITE_PATH_GUARDS have diverged. "
            "A guard on the write path that the dry run does not run makes a clean "
            "preview stop predicting the apply (ledger task 176). Add the name to "
            "_WRITE_PATH_GUARDS — the preflight iterates that tuple, so adding it "
            "there is the whole fix."
        )

    def test_the_guard_we_know_about_is_named(self) -> None:
        """Belt and braces: set equality above also passes if BOTH sides are
        empty, which would be a silently disarmed invariant."""
        assert "assert_project_registered" in adr_seed._WRITE_PATH_GUARDS


# ── task 384/385 follow-up: a preview checks, it does not stamp ─────────────


class TestDryRunDoesNotStampTheRegistry:
    """A ``--dry-run`` that writes is ledger task 385's defect, second instance.

    ``assert_project_registered`` bumps ``project.last_validated_at`` on a
    present row (task 384). The preflight calls that guard so a preview reaches
    the same verdict the apply would — but calling it unqualified made the
    preview mutate the registry, which is exactly what ``verify-hooks`` was
    doing when it claimed to be read-only.

    Parity is preserved where it is owed: the guard still RUNS on the preview
    and still refuses identically. Only the side effect is withheld.
    """

    @pytest.mark.asyncio
    async def test_dry_run_asks_for_a_check_without_the_refresh(self) -> None:
        ledger = _LedgerFake()

        result = await _run(sql_storage=ledger, dry_run=True)

        assert result.get("ok") is not False, result.get("error")
        # The check ran — preview fidelity is unchanged.
        assert ledger.guard_calls == [_PROJECT_ID]
        # ...and asked for no write. RED before the ``refresh`` kwarg landed:
        # the preview issued UPDATE project SET last_validated_at.
        assert ledger.guard_refresh == [False]
        assert ledger.inserts == [], "a dry run reached the write path"

    @pytest.mark.asyncio
    async def test_apply_still_refreshes(self) -> None:
        """The real write keeps the stamp — the clock must still move.

        Suppressing it on the apply too would re-open task 384: nothing would
        bump ``last_validated_at`` and every project would report stale forever.
        """
        ledger = _LedgerFake()

        result = await _run(sql_storage=ledger, dry_run=False)

        assert result.get("ok") is not False, result.get("error")
        assert ledger.inserts, "the apply did not reach create_adr_row"
        # The preflight's own call is refresh=False; create_adr_row's is the
        # default True. The apply therefore stamps, the preview does not.
        assert False in ledger.guard_refresh
        assert True in ledger.guard_refresh

    @pytest.mark.asyncio
    async def test_a_guard_without_the_keyword_is_not_handed_one(self) -> None:
        """A future guard lacking ``refresh`` must not be passed it.

        The preflight reports every exception as "the guard rejected
        project_id", so a ``TypeError`` from a signature mismatch would surface
        as a FALSE rejection of a perfectly good project — "could not call" read
        as "checked and refused". The kwarg is therefore signature-probed.
        """
        seen: list[tuple[str, tuple[str, ...]]] = []

        class _NoKwargGuard(_LedgerFake):
            # Narrower than the base ON PURPOSE — a guard without ``refresh``
            # is exactly what this test hands the preflight.
            async def assert_project_registered(  # type: ignore[override]
                self, project_id: str
            ) -> None:
                seen.append(("assert_project_registered", ()))

        ledger = _NoKwargGuard()
        result = await _run(sql_storage=ledger, dry_run=True)

        assert result.get("ok") is not False, result.get("error")
        assert seen == [("assert_project_registered", ())]
