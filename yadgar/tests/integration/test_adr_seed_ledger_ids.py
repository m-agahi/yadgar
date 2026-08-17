"""``seed_adr_rows`` against BOTH engines at once — the id↔slug alignment proof.

Marked ``integration`` — the default addopts exclude it
(``-m 'not integration and not e2e'``). Run explicitly::

    pytest yadgar/tests/integration/test_adr_seed_ledger_ids.py -m integration -v

WHY THIS FILE EXISTS AT ALL
---------------------------
``seed_adr_rows`` is the one-shot op that lifts 230 historical ADR wiki pages
into the ``adr`` ledger table. ``adr.id`` is ``AUTO_INCREMENT`` and **is** the
ADR number (ADR-0197): it never moves backwards, and ``TRUNCATE`` is FK-blocked
by ``adr_supersedes``. A wrong insert is permanent and unrepairable.

The op straddles both engines — it READS wiki pages out of SurrealDB and WRITES
ledger rows into MariaDB — and until this file nothing in the suite composed the
two. Every existing test injects a ``row_inserter`` seam which, per
``adr_seed.py``'s own idempotency branch, bypasses BOTH the dedup check and the
real ``create_adr_row``, so every id those tests observe is a synthetic
``{"id": len(inserted_order)}``. **No test had ever exercised a real
AUTO_INCREMENT id**, which is why four defects lived here at once:

* the op resolved ONE storage handle (the SurrealDB one) and then called
  ``create_adr_row`` / ``list_adr_rows`` / ``set_adr_body_slug`` on it. The
  SurrealDB engine has zero ADR methods, every ``AttributeError`` was swallowed
  by a blanket ``except Exception``, and the resulting ``rows_inserted=0,
  rows_skipped=236`` reads as "already backfilled" rather than "never ran".
* ADR-0006 mandates skipping the ADRs whose ids are already spent. No layer
  could express that — not the CLI, not the payload, not the op signature.
* the dedup looked rows up under a project key regex-parsed out of the page
  slug (``yadgar-adr-0001`` → ``yadgar``) while the rows are stored under
  ``m-agahi/yadgar``, so it matched nothing for any slug shape.
* ``rows_skipped`` conflated "already had a row", "insert raised" and "insert
  returned no id", and there was no dry run at all.

WHAT THE FIXTURE MIRRORS
------------------------
The live corpus, enumerated 2026-08-17 (task 168): 230 legacy pages
``yadgar-adr-0001…0230`` fully contiguous, a ``yadgar-adr-index`` page, and six
canonical pages ``m-agahi_yadgar_adr-{0001,0005,0006,0007,0008,0009}`` written
2026-08-15/16 for unrelated NEW decisions that happen to occupy the low ids.
The ledger holds exactly those six rows, at ids 1 and 5–9, so the counter sits
at 9.

Inserting rows with EXPLICIT ids is what advances the counter here — ADR-0006
measured that deleting rows leaves it untouched, so a delete-based fixture
would not reproduce the state under test.

Target: skip ADR numbers 1–9 (all nine ids are spent), insert historical 0010
FIRST so it lands on id 10, then 0011→11 … 0230→230. 221 rows exact, 9
permanently displaced. That is the best available arrangement, not a preference.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed (sql extra)")
pytest.importorskip("alembic", reason="alembic not installed (sql extra)")

from yadgar._shared.storage.sql import MariaStorageEngine  # noqa: E402
from yadgar._shared.storage.sql.migrate import upgrade_to_head  # noqa: E402
from yadgar.tests.integration._podman import (  # noqa: E402
    container_is_running,
    container_logs,
    make_socket_dir,
    podman_env,
    remove_container_dir,
    select_container_runtime,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("engine2_mariadb")]

# ADR-0212 pins the engine-#2 server version; the sibling files use the same tag
# so the MariaDB integration modules share one pulled image.
_IMAGE = "docker.io/library/mariadb:11.4"
_DB = "yadgar"
_APP_USER = "yadgar_app"
_APP_PASS = "adr-seed-integration-password"
_BOOT_TIMEOUT_SEC = 180.0
_MOUNT_VISIBLE_TIMEOUT_SEC = 30.0

_PROJECT = "m-agahi/yadgar"
# The canonical slug form for _PROJECT (ADR-0202 / D32 ③: ``/`` → ``_``).
_CANON = "m-agahi_yadgar"
# The legacy Car 2 slug form — basename(directory) + "-adr-".
_LEGACY = "yadgar"

# The six ledger rows the live corpus holds, at these exact ids. Inserting them
# with explicit ids leaves the AUTO_INCREMENT counter at 10.
_SPENT_ROW_IDS = (1, 5, 6, 7, 8, 9)
# All nine low ids are spent, so all nine historical ADRs are skipped.
_SKIP_NUMBERS = tuple(range(1, 10))
_LAST_ADR = 230
# 230 legacy + 6 canonical pages (the index page is excluded by the op).
_PAGES_SEEN = _LAST_ADR + len(_SPENT_ROW_IDS)
# Every legacy page whose number is not skipped.
_EXPECTED_INSERTS = _LAST_ADR - len(_SKIP_NUMBERS)


def _cnf_body(socket: str) -> str:
    return (
        "\n".join(
            [
                "[client]",
                f"socket = {socket}",
                f"user = {_APP_USER}",
                f"password = {_APP_PASS}",
                f"database = {_DB}",
            ]
        )
        + "\n"
    )


@pytest.fixture(scope="module")
def live_mariadb():
    """Scratch MariaDB over a unix socket, torn down with its anonymous volume.

    Fixture body duplicated from the sibling integration modules on purpose —
    the ``xdist_group`` marker is what keeps the files off each other, and a
    shared module-scoped fixture would defeat it. Only the podman helpers are
    imported (car G6: the socket directory cannot be ``/tmp`` under a
    dind-backed runner).
    """
    runtime = select_container_runtime()
    if runtime is None:
        pytest.skip(
            "no working container runtime on this host "
            "(podman/docker absent, or present but non-functional)"
        )

    name = f"yadgar-adrseed-mdb-{uuid.uuid4().hex[:8]}"
    sock_dir = make_socket_dir(runtime, image=_IMAGE, prefix="ymdbs")
    socket_path = sock_dir / "mysqld.sock"

    started = subprocess.run(
        [
            runtime, "run", "-d", "--name", name,
            "--memory", "512m", "--cpus", "1",
            "-e", "MARIADB_ROOT_PASSWORD=adr-seed-root",
            "-e", f"MARIADB_DATABASE={_DB}",
            "-e", f"MARIADB_USER={_APP_USER}",
            "-e", f"MARIADB_PASSWORD={_APP_PASS}",
            "-v", f"{sock_dir}:/sockets:Z",
            _IMAGE,
            "--socket=/sockets/mysqld.sock",
        ],
        capture_output=True, text=True, check=False, timeout=300, env=podman_env(),
    )  # fmt: skip
    if started.returncode != 0:
        shutil.rmtree(sock_dir, ignore_errors=True)
        pytest.skip(f"could not start MariaDB container: {started.stderr.strip()}")

    cnf = sock_dir / "client.cnf"
    cnf.write_text(_cnf_body(str(socket_path)), encoding="utf-8")
    cnf.chmod(0o600)

    try:
        _await_ready(cnf, runtime, name, socket_path)
        yield {"cnf": cnf}
    finally:
        subprocess.run(
            [runtime, "rm", "-f", "-v", name],
            capture_output=True, check=False, timeout=120, env=podman_env(),
        )  # fmt: skip
        remove_container_dir(runtime, sock_dir, image=_IMAGE)


def _await_ready(cnf: Path, runtime: str, name: str, socket_path: Path) -> None:
    """The socket appears before the server is usable (bootstrap server first).

    Exits early on a dead container or a socket that never crosses the mount,
    rather than retrying for the full timeout against something that cannot
    start answering (car G6).
    """
    import asyncio

    async def _probe() -> None:
        deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
        mount_deadline = time.monotonic() + _MOUNT_VISIBLE_TIMEOUT_SEC
        last: Exception | None = None
        while time.monotonic() < deadline:
            if not container_is_running(runtime, name):
                raise AssertionError(
                    f"the MariaDB container {name} exited during boot; "
                    f"last logs:\n{container_logs(runtime, name)}"
                )
            if not socket_path.exists() and time.monotonic() > mount_deadline:
                raise AssertionError(
                    f"{socket_path} never appeared on this side of the mount "
                    f"within {_MOUNT_VISIBLE_TIMEOUT_SEC}s while the container was "
                    "still running — the bind mount is not shared with the "
                    f"{Path(runtime).name} daemon. Set "
                    "YADGAR_TEST_SHARED_MOUNT_ROOT to a directory both sides see."
                )
            engine = MariaStorageEngine.from_option_file(cnf)
            try:
                await engine.verify()
                return
            except Exception as exc:  # noqa: BLE001 — boot race, retry
                last = exc
                await asyncio.sleep(1.0)
            finally:
                await engine.dispose()
        raise AssertionError(f"MariaDB not ready within {_BOOT_TIMEOUT_SEC}s: {last}")

    asyncio.run(_probe())


@pytest.fixture
async def engine(live_mariadb):
    eng = MariaStorageEngine.from_option_file(live_mariadb["cnf"])
    try:
        yield eng
    finally:
        await eng.dispose()


async def _reset_to_base(eng: MariaStorageEngine) -> None:
    """Drop whatever tables the catalog ACTUALLY holds, FK checks off.

    Catalog-driven rather than a hardcoded DROP list or ``alembic downgrade
    base`` — car G6 measured both failure modes (a hardcoded list falls behind
    the migration chain; a stamp-driven downgrade dies on a corrupted stamp and
    takes every later test with it).
    """
    from sqlalchemy import text  # noqa: PLC0415

    tables = await eng.list_tables()
    if not tables:
        return
    async with eng.engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in tables:
            # names come straight from information_schema — catalog state, not input
            await conn.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


async def _exec(eng: MariaStorageEngine, sql: str, params: dict | None = None) -> None:
    """Raw statement helper — the fixtures need SQL the storage surface has no
    method for (an explicit-id INSERT is how the counter gets pre-advanced)."""
    from sqlalchemy import text  # noqa: PLC0415

    async with eng.engine.begin() as conn:
        await conn.execute(text(sql), params or {})


async def _adr_rows(eng: MariaStorageEngine) -> list[dict]:
    from sqlalchemy import text  # noqa: PLC0415

    async with eng.engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id, body_slug, title, status FROM adr ORDER BY id ASC")
        )
        return [dict(r._mapping) for r in result]


@pytest.fixture
async def migrated(engine):
    """A migrated schema with ``_PROJECT`` registered — the state the op needs.

    ``create_adr_row`` calls ``assert_project_registered`` (C6), so an
    unregistered project makes every insert raise before any id is allocated.
    """
    await upgrade_to_head(engine.engine)
    try:
        await engine.create_project_row(key=_PROJECT, kind="git")
        yield engine
    finally:
        await _reset_to_base(engine)


@pytest.fixture
async def spent_counter(migrated):
    """The live ledger state: six rows at ids 1 and 5–9, counter left at 10.

    Explicit-id INSERTs, because that is what actually advances
    ``AUTO_INCREMENT``. ADR-0006 measured the alternative on
    ``mariadb:11.4.12``: deleting all rows and restarting leaves the counter
    untouched, so a fixture that inserted 1–9 and deleted 2–4 would produce the
    right ROWS and the wrong COUNTER — and the counter is the whole subject.

    The six rows carry CANONICAL ``body_slug`` values while their historical
    namesake pages carry LEGACY slugs. That mismatch is not incidental: it is
    exactly the shape the dedup has to recognise (Car 3).
    """
    for n in _SPENT_ROW_IDS:
        await _exec(
            migrated,
            "INSERT INTO adr (id, project_id, title, status, decided_on, body_slug) "
            "VALUES (:id, :project_id, :title, 'accepted', '2026-08-15', :body_slug)",
            {
                "id": n,
                "project_id": _PROJECT,
                "title": f"new decision occupying id {n}",
                "body_slug": f"{_CANON}_adr-{n:04d}",
            },
        )
    return migrated


# ── the SurrealDB half ────────────────────────────────────────────────────────


def _adr_body(number: int, title: str) -> str:
    """A per-ADR page body in the shape ``adr_render._build_adr_body`` emits."""
    return (
        f"# ADR-{number:04d}: {title}\n"
        f"\n"
        f"- status: accepted\n"
        f"- date: 2026-01-{(number % 28) + 1:02d}\n"
        f"- supersedes: none\n"
        f"\n"
        f"## Context\n\ncontext for {number}\n\n"
        f"## Decision\n\ndecision for {number}\n"
    )


def _index_body(count: int) -> str:
    """The legacy ``<project>-adr-index`` table body the D35c gate counts rows in."""
    lines = ["| ADR | Title | Status |", "| --- | --- | --- |"]
    lines += [f"| ADR-{n:04d} | historical {n} | accepted |" for n in range(1, count + 1)]
    return "\n".join(lines) + "\n"


def _insert_page(store, *, slug: str, title: str, content: str, directory: str) -> None:
    store.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "decision",
            "tags": ["adr", "decisions"],
            "confidence": "high",
            "page_type": "adr",
            "directory_context": directory,
            "project_id": _PROJECT,
            # page_type='adr' is mutability='locked' (D26), and the gate fires on
            # INSERT too — the real corpus was written through the sanctioned
            # canonical-writer path, so the fixture uses the same door.
            "_sanctioned": True,
        }
    )


@pytest.fixture
def wiki_corpus(tmp_path):
    """An embedded SurrealDB holding the live page census, plus its directory.

    Embedded ``StorageEngine(db_path=...)`` — no second container. No other
    integration module does this; the seed is the only op that needs both
    engines answering in the same call.

    Both slug shapes are present, which is what makes the union path and the
    legacy↔canonical dedup observable at all. There is no bulk page-insert
    helper on the wiki surface, so this loops the ordinary per-page insert.
    """
    from yadgar._shared.storage import StorageEngine

    # basename must be "yadgar": the legacy slug prefix and the legacy index
    # slug are both built from basename(directory) (adr_seed._adr_slug_prefixes
    # / _count_legacy_index_rows).
    project_dir = tmp_path / _LEGACY
    project_dir.mkdir()
    directory = str(project_dir)

    store = StorageEngine(str(tmp_path / "storage.db"))
    try:
        for n in range(1, _LAST_ADR + 1):
            _insert_page(
                store,
                slug=f"{_LEGACY}-adr-{n:04d}",
                title=f"historical ADR {n}",
                content=_adr_body(n, f"historical {n}"),
                directory=directory,
            )
        for n in _SPENT_ROW_IDS:
            _insert_page(
                store,
                slug=f"{_CANON}_adr-{n:04d}",
                title=f"new decision {n}",
                content=_adr_body(n, f"new decision {n}"),
                directory=directory,
            )
        _insert_page(
            store,
            slug=f"{_LEGACY}-adr-index",
            title="ADR index",
            content=_index_body(_LAST_ADR),
            directory=directory,
        )
        yield store, directory
    finally:
        store.close()


async def _seed(store, directory, sql, **kwargs):
    from yadgar.backend.admin_exec.adr_seed import seed_adr_rows

    return await seed_adr_rows(
        project_id=_PROJECT,
        directory=directory,
        storage=store,
        sql_storage=sql,
        **kwargs,
    )


# ── the id↔slug alignment proof ───────────────────────────────────────────────


class TestSeedLandsEveryAdrOnItsOwnNumber:
    """The property the whole op exists for: ``id == number(body_slug)``.

    Asserted against a REAL ``AUTO_INCREMENT`` sequence over the full 230-ADR
    range, at the bottom, the middle and the top. Nothing in the suite asserted
    this before — the ``row_inserter`` seam every other test injects hands back
    a synthetic id, so an off-by-nine misnumbering was invisible by
    construction.
    """

    async def test_skipped_numbers_are_never_inserted(self, spent_counter, wiki_corpus):
        store, directory = wiki_corpus
        result = await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)

        assert result.get("rows_inserted") == _EXPECTED_INSERTS, (
            f"expected {_EXPECTED_INSERTS} inserts (230 historical ADRs minus the "
            f"nine spent numbers), got {result.get('rows_inserted')} — result={result}"
        )
        rows = await _adr_rows(spent_counter)
        for n in _SKIP_NUMBERS:
            assert f"{_LEGACY}-adr-{n:04d}" not in {r["body_slug"] for r in rows}, (
                f"ADR-{n:04d} was in skip_adr_numbers but a row for the legacy "
                f"page was inserted anyway — every later ADR is now off by one"
            )

    async def test_first_insert_lands_on_id_ten(self, spent_counter, wiki_corpus):
        """Historical 0010 must be the FIRST insert so it takes id 10.

        Ascending insertion order is the whole mechanism: the op cannot supply
        an id, so the only lever on which number a page lands is the order the
        INSERTs go out in.
        """
        store, directory = wiki_corpus
        await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)

        rows = await _adr_rows(spent_counter)
        fresh = [r for r in rows if r["id"] > max(_SPENT_ROW_IDS)]
        assert fresh, "no rows were inserted above the spent range"
        assert fresh[0]["id"] == 10
        assert fresh[0]["body_slug"] == f"{_LEGACY}-adr-0010"

    async def test_id_equals_adr_number_across_the_whole_range(self, spent_counter, wiki_corpus):
        store, directory = wiki_corpus
        await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)

        rows = await _adr_rows(spent_counter)
        by_id = {r["id"]: r["body_slug"] for r in rows}

        misaligned = [
            (n, by_id.get(n))
            for n in range(max(_SPENT_ROW_IDS) + 1, _LAST_ADR + 1)
            if by_id.get(n) != f"{_LEGACY}-adr-{n:04d}"
        ]
        assert not misaligned, (
            f"{len(misaligned)} ledger id(s) do not carry their own ADR number's "
            f"page; first ten: {misaligned[:10]}"
        )
        # Bottom, middle and top, spelled out — a single set comparison that
        # regressed would not say WHERE the range broke.
        assert by_id[10] == f"{_LEGACY}-adr-0010"
        assert by_id[120] == f"{_LEGACY}-adr-0120"
        assert by_id[_LAST_ADR] == f"{_LEGACY}-adr-{_LAST_ADR:04d}"
        assert max(by_id) == _LAST_ADR, (
            f"the top id is {max(by_id)}, not {_LAST_ADR} — the corpus overran its own numbering"
        )

    async def test_pages_seen_counts_both_slug_shapes(self, spent_counter, wiki_corpus):
        """236, not 230: numbers 1 and 5–9 are enumerated under BOTH prefixes.

        The op unions by SLUG, so a number owning a legacy page and a canonical
        page is seen twice. Keying the skip on the NUMBER is what collapses
        them — a slug-keyed skip set would need 15 entries and would silently
        miss whichever shape the operator did not type.
        """
        store, directory = wiki_corpus
        result = await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)
        assert result.get("pages_seen") == _PAGES_SEEN


class TestOutcomeCountersAreDistinguishable:
    """``rows_skipped`` conflated three outcomes; the split must separate them."""

    async def test_second_run_inserts_nothing_and_reports_them_present(
        self, spent_counter, wiki_corpus
    ):
        store, directory = wiki_corpus
        first = await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)
        assert first.get("rows_inserted") == _EXPECTED_INSERTS

        second = await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)
        assert second.get("rows_inserted") == 0, (
            "a second run inserted rows — the dedup does not bind, and every "
            f"re-run permanently duplicates the corpus. result={second}"
        )
        assert second.get("rows_already_present") == _EXPECTED_INSERTS
        assert second.get("rows_failed") == 0
        rows = await _adr_rows(spent_counter)
        assert len(rows) == len(_SPENT_ROW_IDS) + _EXPECTED_INSERTS

    async def test_result_no_longer_carries_the_conflated_counter(self, spent_counter, wiki_corpus):
        store, directory = wiki_corpus
        result = await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)
        assert "rows_skipped" not in result, (
            "rows_skipped conflated 'already present', 'insert raised' and "
            "'insert returned no id' — keeping it alongside the split counters "
            "leaves the ambiguous number in the operator's report"
        )
        assert result.get("rows_failed") == 0
        assert result.get("rows_skipped_by_request") == len(_SKIP_NUMBERS) + len(_SPENT_ROW_IDS), (
            "the skip must be reported: nine legacy pages plus the six "
            "canonical pages sharing those numbers"
        )


class TestVerificationGateIsNonVacuous:
    """With a stub storage every gate count is 0 and ``exact_match`` compares
    zeros — vacuously reconciled. Against real engines the numbers are real."""

    async def test_gate_reports_the_three_real_counts(self, spent_counter, wiki_corpus):
        store, directory = wiki_corpus
        result = await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)
        gate = result.get("gate") or {}

        assert gate.get("index_rows") == _LAST_ADR, (
            "the legacy index page holds 230 table rows; a 0 here means "
            "_count_legacy_index_rows never resolved the page"
        )
        assert gate.get("pages_seen") == _PAGES_SEEN
        assert gate.get("page_type_adr_rows") == len(_SPENT_ROW_IDS) + _EXPECTED_INSERTS
        # 230 != 236 != 227. The three counts CANNOT reconcile for this corpus:
        # the six canonical pages are counted as pages but share numbers with
        # rows that already existed. exact_match=False is the honest outcome,
        # and it is what the CLI turns into a non-zero exit AFTER the writes
        # have already committed.
        assert gate.get("exact_match") is False


class TestDryRunPredictsTheApply:
    """A dry run that cannot be checked against the apply is decoration.

    ``--adr-rows`` had no dry run at all, and ``--apply`` was wired only to the
    reslug branch — so the irreversible half of the CLI was the half with no
    preview.
    """

    async def test_dry_run_writes_nothing(self, spent_counter, wiki_corpus):
        store, directory = wiki_corpus
        before = await _adr_rows(spent_counter)
        result = await _seed(
            store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS, dry_run=True
        )
        assert result.get("dry_run") is True
        assert result.get("rows_inserted") == 0
        assert await _adr_rows(spent_counter) == before

    async def test_dry_run_plan_equals_what_apply_produces(self, spent_counter, wiki_corpus):
        store, directory = wiki_corpus
        planned = await _seed(
            store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS, dry_run=True
        )
        plan = planned.get("plan") or []
        assert len(plan) == _EXPECTED_INSERTS
        predicted = {int(e["planned_id"]): e["slug"] for e in plan}
        assert predicted[10] == f"{_LEGACY}-adr-0010"
        assert predicted[_LAST_ADR] == f"{_LEGACY}-adr-{_LAST_ADR:04d}"

        await _seed(store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS)
        actual = {
            r["id"]: r["body_slug"]
            for r in await _adr_rows(spent_counter)
            if r["id"] > max(_SPENT_ROW_IDS)
        }
        assert predicted == actual, (
            "the dry run predicted a different (ADR number → id) mapping than "
            "the apply produced — the preview an operator gates an "
            "unrepairable write on is wrong"
        )

    async def test_next_id_basis_is_reported(self, spent_counter, wiki_corpus):
        """``max(id)+1`` is wrong exactly in this state: ADR-0006 measured that
        deleting rows leaves the counter ahead of the highest surviving id. The
        result must say which source the prediction came from."""
        store, directory = wiki_corpus
        result = await _seed(
            store, directory, spent_counter, skip_adr_numbers=_SKIP_NUMBERS, dry_run=True
        )
        assert result.get("next_id_basis") == "information_schema"
        assert result.get("next_id") == max(_SPENT_ROW_IDS) + 1


# ── Car 3: legacy ↔ canonical dedup, both directions ─────────────────────────


@pytest.fixture
def mini_corpus(tmp_path):
    """A three-page corpus for the directed dedup cases.

    Kept apart from ``wiki_corpus`` because each case needs a DIFFERENT
    row/page slug pairing, and 236 pages is a slow way to assert one lookup.
    """
    from yadgar._shared.storage import StorageEngine

    project_dir = tmp_path / _LEGACY
    project_dir.mkdir()
    directory = str(project_dir)
    store = StorageEngine(str(tmp_path / "mini.db"))

    def _add(slug: str, number: int) -> None:
        _insert_page(
            store,
            slug=slug,
            title=f"ADR {number}",
            content=_adr_body(number, f"adr {number}"),
            directory=directory,
        )

    try:
        yield store, directory, _add
    finally:
        store.close()


class TestDedupNormalisesBothSlugShapes:
    """A row whose ``body_slug`` is canonical must be recognised when the page
    still carries its legacy slug — and vice versa.

    The failure this guards is asymmetric and permanent in both directions: a
    normalisation that is too LOOSE skips a page that needed inserting and
    leaves a hole in the numbering; one that is too TIGHT inserts a duplicate.
    Both are unrepairable, so both directions are asserted.
    """

    async def test_canonical_row_matches_legacy_page(self, migrated, mini_corpus):
        store, directory, add = mini_corpus
        add(f"{_LEGACY}-adr-0042", 42)
        await _exec(
            migrated,
            "INSERT INTO adr (project_id, title, status, body_slug) "
            "VALUES (:p, 'existing', 'accepted', :s)",
            {"p": _PROJECT, "s": f"{_CANON}_adr-0042"},
        )

        result = await _seed(store, directory, migrated)
        assert result.get("rows_inserted") == 0, (
            "the ledger already holds ADR-0042 under its CANONICAL body_slug; "
            "the legacy page must not be inserted a second time"
        )
        assert result.get("rows_already_present") == 1

    async def test_legacy_row_matches_canonical_page(self, migrated, mini_corpus):
        store, directory, add = mini_corpus
        add(f"{_CANON}_adr-0042", 42)
        await _exec(
            migrated,
            "INSERT INTO adr (project_id, title, status, body_slug) "
            "VALUES (:p, 'existing', 'accepted', :s)",
            {"p": _PROJECT, "s": f"{_LEGACY}-adr-0042"},
        )

        result = await _seed(store, directory, migrated)
        assert result.get("rows_inserted") == 0, (
            "the inverse direction: a LEGACY body_slug in the ledger must be "
            "recognised when the page has been re-slugged to canonical"
        )
        assert result.get("rows_already_present") == 1

    async def test_a_different_number_is_still_inserted(self, migrated, mini_corpus):
        """The guard against the permanent hole: normalisation must not collapse
        DIFFERENT ADR numbers onto each other."""
        store, directory, add = mini_corpus
        add(f"{_LEGACY}-adr-0043", 43)
        await _exec(
            migrated,
            "INSERT INTO adr (project_id, title, status, body_slug) "
            "VALUES (:p, 'existing', 'accepted', :s)",
            {"p": _PROJECT, "s": f"{_CANON}_adr-0042"},
        )

        result = await _seed(store, directory, migrated)
        assert result.get("rows_inserted") == 1, (
            "ADR-0043 has no ledger row — a normalisation that treats it as "
            "already present leaves a permanent hole in the numbering"
        )
        rows = await _adr_rows(migrated)
        assert f"{_LEGACY}-adr-0043" in {r["body_slug"] for r in rows}

    async def test_project_id_scope_is_the_real_key(self, migrated, mini_corpus):
        """The dedup looked rows up under a project key regex-parsed out of the
        page slug (``yadgar-adr-0042`` → ``yadgar``) while rows live under
        ``m-agahi/yadgar``. That lookup matched nothing for any slug shape, so
        the idempotency claim was never a property."""
        from yadgar.backend.admin_exec import adr_seed

        assert not hasattr(adr_seed, "_project_slug_from_page_slug"), (
            "the slug-fragment project derivation is the defect; the op already "
            "holds the real project_id and must use it"
        )
