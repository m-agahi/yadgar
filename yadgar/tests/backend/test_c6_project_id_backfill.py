"""C6 — the operator-invoked ``project_id`` backfill op.

Modelled on ``reslug_adr_pages``: build a manifest, return it UN-APPLIED,
operator reviews, re-runs with ``dry_run=False``. The op derives NOTHING — it
takes a host-resolved ``directory_context → project_id`` mapping produced by
the C2 mint running host-side, because neither image installs git nor mounts a
host project directory (ADR-0227).

The measured corpus (live ``db_inspect``, 2026-08-10) is why the manifest is
REVIEWED rather than derived: 1,033 of 5,349 rows — ~19% — carry a sentinel
``directory_context`` (``global`` / ``system``) that no path mapping covers,
and 18 more distinct values are free-text prose. A backfill that reported
success while silently bucketing 19% is exactly the ADR-0222 failure mode.

The two REFUSALS are the mechanism that makes "reviewed" real, and they are
what these tests spend most of their assertions on:

  * an apply with a non-empty ``unmapped`` bucket REFUSES unless the operator
    passes ``quarantine_unmapped=True``;
  * an apply that would DELETE rows REFUSES unless the operator passes
    ``confirm_deletes=True``.

Without those, the manifest is decorative.
"""

from __future__ import annotations

from typing import Any

import pytest

from yadgar.backend.admin_exec.project_backfill import (
    MEMIFY_CONTENT_MARKER,
    project_id_backfill,
)

# ── fake storage ────────────────────────────────────────────────────────────

_YADGAR = "/home/max/git/yadgar"
_QWFM = "/home/max/quinyx/qwfm"
_PROSE = "Hard rule from Max set 2026-05-07 — must survive compaction"


class _FakeStorage:
    """In-memory double for the SurrealDB half.

    Answers the two discovery SELECTs the op issues and RECORDS every
    statement, so a test can assert that a dry run issued no mutation at all
    — the property the whole op design rests on.
    """

    def __init__(self, memory: list[dict], wiki: list[dict]) -> None:
        self.memory = memory
        self.wiki = wiki
        self.statements: list[tuple[str, dict | None]] = []

    def _q(self, query: str, params: dict | None = None) -> list[dict]:
        self.statements.append((query, params))
        upper = query.strip().upper()
        if upper.startswith("SELECT"):
            table = "memory" if " FROM MEMORY" in upper else "wiki_page"
            rows = self.memory if table == "memory" else self.wiki
            return [dict(r) for r in rows]
        return []

    @property
    def mutations(self) -> list[tuple[str, dict | None]]:
        """Every non-SELECT statement issued."""
        return [(q, p) for q, p in self.statements if not q.strip().upper().startswith("SELECT")]

    def _extract_id(self, raw: Any) -> int:
        return int(str(raw).split(":")[-1])


class _FakeSql:
    """Registry half — answers ``list_project_rows`` with the known keys."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    async def list_project_rows(self) -> list[dict]:
        return [{"key": k, "kind": "git"} for k in self.keys]


def _memify_row(rid: int, dc: str) -> dict:
    """A ``_memify_derive`` co-occurrence row — the D4 producer signature."""
    return {
        "id": f"memory:{rid}",
        "directory_context": dc,
        "tags": ["derived", "auto-generated"],
        "content": f"foo.py and bar.py {MEMIFY_CONTENT_MARKER}",
    }


def _corpus() -> tuple[list[dict], list[dict]]:
    """A miniature of the measured corpus: every class present, small counts."""
    memory = [
        # real paths with a mapping
        {"id": "memory:1", "directory_context": _YADGAR, "tags": ["a"], "content": "x"},
        {"id": "memory:2", "directory_context": _QWFM, "tags": ["b"], "content": "y"},
        # a row that already carries the `global` reach tag on a REAL dir —
        # the visibility-change class (measured live: 4 of 7).
        {
            "id": "memory:3",
            "directory_context": _YADGAR,
            "tags": ["global"],
            "content": "z",
        },
        # `system` — D3, deleted
        {"id": "memory:4", "directory_context": "system", "tags": ["derived"], "content": "s"},
        # `global` — the D4 cohort (deleted) and a genuine global (kept)
        _memify_row(5, "global"),
        {"id": "memory:6", "directory_context": "global", "tags": ["c"], "content": "keep me"},
        # free-text prose — the genuine quarantine set
        {"id": "memory:7", "directory_context": _PROSE, "tags": [], "content": "p"},
        # same producer at a REAL dir — D4 says KEEP and migrate normally
        _memify_row(8, _YADGAR),
        # NO directory_context at all. Probably extinct (migrations 018/023
        # backfilled the empty case and the memory table carries a non-empty
        # ASSERT) — present here so the totals identity below is checked
        # against this class rather than passing because no row exercises it.
        {"id": "memory:9", "directory_context": None, "tags": [], "content": "n"},
    ]
    wiki = [
        {"id": "wiki_page:1", "directory_context": _YADGAR, "tags": ["adr"], "content": "w"},
        {"id": "wiki_page:2", "directory_context": "global", "tags": [], "content": "g"},
    ]
    return memory, wiki


_MAPPING = {
    _YADGAR: "m-agahi/yadgar",
    _QWFM: "quinyx/qwfm",
    "global": "local/aws-work",
}
_REGISTERED = ["m-agahi/yadgar", "quinyx/qwfm", "local/aws-work"]


@pytest.fixture
def fakes(monkeypatch):
    """Install the two storage doubles and hand them back."""
    memory, wiki = _corpus()
    storage = _FakeStorage(memory, wiki)
    sql = _FakeSql(_REGISTERED)
    import yadgar.backend.admin_exec.project_backfill as mod

    monkeypatch.setattr(mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(mod, "_get_sql_storage", lambda: sql)
    return storage, sql


@pytest.fixture
def clean_fakes(monkeypatch):
    """``fakes`` minus the directory-less row.

    The apply-path tests need a corpus the op will actually apply. A row with
    no ``directory_context`` is an unconditional refusal — it admits no
    acknowledgement flag — so it is excluded here rather than waved through,
    which would defeat the point of the refusal.
    """
    memory, wiki = _corpus()
    memory = [r for r in memory if r["directory_context"] is not None]
    storage = _FakeStorage(memory, wiki)
    sql = _FakeSql(_REGISTERED)
    import yadgar.backend.admin_exec.project_backfill as mod

    monkeypatch.setattr(mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(mod, "_get_sql_storage", lambda: sql)
    return storage, sql


def _payload(**over) -> dict:
    base = {"mapping": dict(_MAPPING)}
    base.update(over)
    return base


# ── dry run is the default, and it cannot write ─────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_is_the_default(fakes):
    """Omitting ``dry_run`` must NOT apply. The safe value is the default one.

    An operator op whose default mutates is one typo away from an unreviewed
    corpus rewrite.
    """
    storage, _ = fakes
    result = await project_id_backfill(_payload())
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert storage.mutations == [], f"dry run issued writes: {storage.mutations}"


@pytest.mark.asyncio
async def test_dry_run_returns_the_full_manifest_unapplied(fakes):
    """The manifest is complete on a dry run — nothing is withheld until apply.

    The operator reviews THIS structure and then re-runs; a manifest that
    only materialised on apply would make the review impossible.
    """
    _, _ = fakes
    result = await project_id_backfill(_payload())
    for key in (
        "updates",
        "deletes",
        "quarantine",
        "unmapped",
        "no_directory",
        "visibility_changes",
        "totals",
    ):
        assert key in result, f"manifest missing {key!r}"


@pytest.mark.asyncio
async def test_dry_run_classifies_every_row_exactly_once(fakes):
    """Every row lands in exactly one class — the anti-ADR-0222 arithmetic.

    The failure this pins is a backfill reporting success while some rows
    fell through every branch: seen must equal updated + deleted +
    quarantined + unmapped.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    t = r["totals"]
    assert t["rows_seen"] == (
        t["rows_updated"]
        + t["rows_deleted"]
        + t["rows_quarantined"]
        + t["rows_unmapped"]
        + t["rows_no_directory"]
    ), t


# ── the mapping is host-resolved; the op derives nothing ────────────────────


@pytest.mark.asyncio
async def test_a_missing_mapping_is_refused(fakes):
    """No mapping → no run. The op must never invent one (ADR-0227).

    The pre-C6 migration derived ``local/<basename>`` inside a container that
    installs no git and mounts no project directory — a well-formed key that
    is indistinguishable at read time from a correct one.
    """
    storage, _ = fakes
    result = await project_id_backfill({"dry_run": False})
    assert result["ok"] is False
    assert result["reason"] == "missing_mapping"
    assert storage.mutations == []


@pytest.mark.asyncio
async def test_mapped_directories_get_their_host_resolved_project_id(fakes):
    """Each mapped ``directory_context`` becomes its mapped project_id."""
    _, _ = fakes
    r = await project_id_backfill(_payload())
    by_key = {(u["table"], u["directory_context"]): u for u in r["updates"]}
    assert by_key[("memory", _YADGAR)]["project_id"] == "m-agahi/yadgar"
    assert by_key[("wiki_page", _YADGAR)]["project_id"] == "m-agahi/yadgar"
    assert by_key[("memory", _QWFM)]["project_id"] == "quinyx/qwfm"


@pytest.mark.asyncio
async def test_an_empty_string_mapping_target_is_treated_as_unmapped(fakes):
    """G2 item 5 — an operator-supplied ``""`` target is not a project_id.

    ``_plan_updates`` used ``if target is None:`` to decide unmapped, so a
    mapping entry like ``{_YADGAR: ""}`` (an operator typo, a stripped
    value, a bad host-side join) satisfied ``target is not None`` and was
    bucketed as a real UPDATE carrying ``project_id=""`` — visible in the
    dry-run manifest as if it were a legitimate target, and (were ``_apply``
    ever invoked directly, bypassing the registry gate) would have bound the
    empty string as a real parameter rather than the ``NONE`` literal
    ``project_id_set_fragment`` uses everywhere else in this train. An empty
    string is exactly as "no derivable owner" as a mapping key that is
    simply absent, so it must land in ``unmapped`` (the reviewed/quarantine
    path), not ``updates``.

    Proves the corpus row genuinely exists first (``_YADGAR`` rows are
    present in the fixture corpus — memory:1, memory:3, memory:8,
    wiki_page:1) rather than asserting absence over an empty bucket.
    """
    _, _ = fakes
    bad_mapping = dict(_MAPPING)
    bad_mapping[_YADGAR] = ""
    r = await project_id_backfill(_payload(mapping=bad_mapping))

    updated_dcs = {u["directory_context"] for u in r["updates"]}
    assert _YADGAR not in updated_dcs, (
        f"an empty-string mapping target was bucketed as an update: {r['updates']}"
    )
    unmapped_dcs = {u["directory_context"] for u in r["unmapped"]}
    assert _YADGAR in unmapped_dcs, (
        f"an empty-string mapping target must land in unmapped, got: {r['unmapped']}"
    )


@pytest.mark.asyncio
async def test_global_rows_get_an_owner_and_keep_their_reach(fakes):
    """Decision G: ``global`` gets a project_id owner PLUS the ``global`` tag.

    Owner and reach are recorded separately (§1.4). Dropping the tag would
    silently narrow 429 rows from every-project to one-project visibility;
    dropping the owner would leave them unscoped.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    entry = next(
        u for u in r["updates"] if u["table"] == "memory" and u["directory_context"] == "global"
    )
    assert entry["project_id"] == "local/aws-work"
    assert entry["add_global_tag"] is True
    # A real-path update must NOT gain the reach tag.
    real = next(
        u for u in r["updates"] if u["table"] == "memory" and u["directory_context"] == _YADGAR
    )
    assert real["add_global_tag"] is False


@pytest.mark.asyncio
async def test_unknown_registry_targets_are_surfaced(fakes):
    """A mapping target that is not a registered project is named, not used.

    ADR-0223 makes registry enforcement FAIL LOUD. Discovering an
    unregistered target as a per-row FK error halfway through the apply is
    the failure this check exists to prevent.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload(mapping={_YADGAR: "m-agahi/typo"}))
    assert r["registry"]["unknown_targets"] == ["m-agahi/typo"]


# ── the two refusals ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_refuses_while_rows_are_unmapped(fakes):
    """THE gate. An unreviewed bucket blocks the apply and writes nothing.

    Without this the op silently quarantines whatever the host mapping
    missed and reports success — ADR-0222 rebuilt with extra steps.
    """
    storage, _ = fakes
    r = await project_id_backfill(_payload(dry_run=False, confirm_deletes=True))
    assert r["ok"] is False
    assert r["reason"] == "unreviewed_directory_contexts"
    assert any(u["directory_context"] == _PROSE for u in r["unmapped"])
    assert storage.mutations == [], "a refused apply must write nothing"


@pytest.mark.asyncio
async def test_apply_refuses_unconfirmed_deletes(fakes):
    """Deletes need their own acknowledgement — D4 destroys readable rows."""
    storage, _ = fakes
    r = await project_id_backfill(_payload(dry_run=False, quarantine_unmapped=True))
    assert r["ok"] is False
    assert r["reason"] == "unconfirmed_deletes"
    assert storage.mutations == []


@pytest.mark.asyncio
async def test_apply_refuses_an_unregistered_target(fakes):
    """A bad mapping target blocks the apply outright."""
    storage, _ = fakes
    r = await project_id_backfill(
        _payload(
            mapping={_YADGAR: "m-agahi/typo"},
            dry_run=False,
            quarantine_unmapped=True,
            confirm_deletes=True,
        )
    )
    assert r["ok"] is False
    assert r["reason"] == "unknown_registry_targets"
    assert storage.mutations == []


# ── the delete cohorts ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_cohort_is_deleted_and_marked_already_unreadable(fakes):
    """D3: the 604 ``system`` rows go. Deletion changes NO observable behaviour.

    ``'system'`` was removed from ``_ALWAYS_ELIGIBLE`` in v5.65, so the rows
    are already unreadable — the manifest records that so a reviewer can tell
    this cohort apart from D4's.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    system = next(d for d in r["deletes"] if d["cohort"] == "system")
    assert system["table"] == "memory"
    assert system["currently_readable"] is False
    assert system["rows"] == 1
    assert system["ids"] == ["memory:4"]


@pytest.mark.asyncio
async def test_memify_cohort_is_marked_as_a_real_behaviour_change(fakes):
    """D4: unlike D3 these rows ARE currently readable. Say so in the manifest.

    "Already unreadable" and "readable, and being deleted" are different
    decisions for a reviewer; collapsing them into one ``deletes`` list with
    no distinction is how the second one gets waved through.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    cohort = next(d for d in r["deletes"] if d["cohort"] == "memify_global")
    assert cohort["currently_readable"] is True
    assert cohort["ids"] == ["memory:5"]


@pytest.mark.asyncio
async def test_memify_cohort_keeps_the_same_producer_at_a_real_directory(fakes):
    """D4 KEEPS the ~113 same-producer rows that carry a real project dir.

    The single-project ones are the ones the ``dominant_directory`` vote got
    right; they migrate normally.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    cohort = next(d for d in r["deletes"] if d["cohort"] == "memify_global")
    assert "memory:8" not in cohort["ids"], "a real-dir row of the same producer was deleted"


@pytest.mark.parametrize(
    "row",
    [
        # missing the `derived` tag
        {
            "id": "memory:90",
            "directory_context": "global",
            "tags": ["auto-generated"],
            "content": f"a and b {MEMIFY_CONTENT_MARKER}",
        },
        # missing the `auto-generated` tag
        {
            "id": "memory:91",
            "directory_context": "global",
            "tags": ["derived"],
            "content": f"a and b {MEMIFY_CONTENT_MARKER}",
        },
        # missing the content marker
        {
            "id": "memory:92",
            "directory_context": "global",
            "tags": ["derived", "auto-generated"],
            "content": "a genuinely global note",
        },
        # missing the directory_context
        {
            "id": "memory:93",
            "directory_context": _YADGAR,
            "tags": ["derived", "auto-generated"],
            "content": f"a and b {MEMIFY_CONTENT_MARKER}",
        },
    ],
)
@pytest.mark.asyncio
async def test_three_of_four_conjuncts_is_not_enough_to_delete(row, monkeypatch):
    """The D4 signature is a FOUR-way conjunction — any three must SURVIVE.

    Implementing it as ``directory_context='global' AND content LIKE
    '%frequently modified together%'`` over-deletes; dropping either tag
    condition over-deletes differently. Each parametrised row is missing
    exactly one conjunct.
    """
    import yadgar.backend.admin_exec.project_backfill as mod

    storage = _FakeStorage([row], [])
    monkeypatch.setattr(mod, "_get_storage", lambda: storage)
    monkeypatch.setattr(mod, "_get_sql_storage", lambda: _FakeSql(_REGISTERED))

    r = await project_id_backfill(_payload())
    deleted_ids = [i for d in r["deletes"] for i in d["ids"]]
    assert row["id"] not in deleted_ids, "a row missing one conjunct was scheduled for deletion"


# ── quarantine (``legacy_directory``) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_unmapped_prose_is_quarantined_not_guessed(clean_fakes):
    """Free-text prose has no derivable owner — it goes to ``legacy_directory``.

    ``memorize(context=)`` used as a description is what its own docstring
    forbids; the original value is preserved for human adjudication rather
    than dropped or heuristically basenamed.
    """
    storage, _ = clean_fakes
    r = await project_id_backfill(
        _payload(dry_run=False, quarantine_unmapped=True, confirm_deletes=True)
    )
    assert r["ok"] is True
    assert any(q["directory_context"] == _PROSE for q in r["quarantine"])
    quarantine_writes = [(q, p) for q, p in storage.mutations if "legacy_directory" in q.lower()]
    assert quarantine_writes, "quarantine wrote no legacy_directory"
    assert all("project_id" not in q for q, _ in quarantine_writes), (
        "a quarantined row must NOT be stamped with a guessed project_id"
    )


# ── the apply path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_writes_exactly_the_dry_run_manifest(clean_fakes, monkeypatch):
    """``dry_run=False`` applies EXACTLY what ``dry_run=True`` reported.

    Compared entry-for-entry against a dry run over the same corpus: the
    review is worthless if the apply can do something the manifest did not
    describe.
    """
    _, _ = clean_fakes
    ack = {"quarantine_unmapped": True, "confirm_deletes": True}
    preview = await project_id_backfill(_payload(**ack))

    # Fresh doubles so the apply sees the same corpus the preview did.
    import yadgar.backend.admin_exec.project_backfill as mod

    memory, wiki = _corpus()
    memory = [r for r in memory if r["directory_context"] is not None]
    storage2 = _FakeStorage(memory, wiki)
    monkeypatch.setattr(mod, "_get_storage", lambda: storage2)
    monkeypatch.setattr(mod, "_get_sql_storage", lambda: _FakeSql(_REGISTERED))
    applied = await project_id_backfill(_payload(dry_run=False, **ack))

    assert applied["ok"] is True
    assert applied["applied"] is True
    for key in ("updates", "deletes", "quarantine"):
        assert applied[key] == preview[key], f"{key} diverged between preview and apply"


@pytest.mark.asyncio
async def test_apply_deletes_before_it_stamps_the_global_cohort(clean_fakes):
    """Ordering is load-bearing: the D4 cohort is a SUBSET of ``global``.

    Stamping first would write a project_id onto rows about to be deleted,
    and — worse — the manifest's ``global`` row count would describe a set
    larger than the one that survives.
    """
    storage, _ = clean_fakes
    await project_id_backfill(
        _payload(dry_run=False, quarantine_unmapped=True, confirm_deletes=True)
    )
    kinds = [
        "DELETE" if q.strip().upper().startswith("DELETE") else "UPDATE"
        for q, _ in storage.mutations
    ]
    assert "DELETE" in kinds and "UPDATE" in kinds
    assert kinds.index("DELETE") < kinds.index("UPDATE"), f"updates ran before deletes: {kinds}"


@pytest.mark.asyncio
async def test_global_update_count_excludes_the_rows_being_deleted(fakes):
    """The manifest's ``global`` count is the SURVIVORS, not the whole class.

    349 measured ``global`` memory rows minus D4's 238 leaves ~111. A count
    that reported 349 would tell the reviewer the wrong thing about what the
    stamp actually touches.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    entry = next(
        u for u in r["updates"] if u["table"] == "memory" and u["directory_context"] == "global"
    )
    assert entry["rows"] == 1, "expected only the surviving genuine-global row"


# ── the visibility-change review list (D2) ──────────────────────────────────


@pytest.mark.asyncio
async def test_rows_that_already_carry_a_global_tag_are_listed_for_review(fakes):
    """D2: a row with a real dir AND a ``global`` tag becomes globally visible.

    The reach tag is SELF-GRANTED — it lives in the free-text ``tags`` array
    any agent writes. Measured live: 4 of 7 tagged memory rows have a real
    project directory, so they flip from project-scoped to globally visible
    the moment C7's predicate switches. That is a visibility change, not a
    migration detail, and it gets eyeball approval.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    ids = {v["id"] for v in r["visibility_changes"]}
    assert "memory:3" in ids
    # A row whose directory_context IS the sentinel is not a CHANGE — it was
    # already globally visible.
    assert "memory:6" not in ids


# ── registration ────────────────────────────────────────────────────────────


def test_backfill_op_is_registered_on_the_admin_dispatch():
    """The op must be reachable over ``/admin`` — it is the operator surface."""
    from yadgar.backend.admin_exec import admin_ops

    assert "project_id_backfill" in admin_ops()


# ── the no-directory bucket ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_row_with_no_directory_lands_in_its_own_bucket(fakes):
    """A directory-less row is NAMED, never dropped.

    It counts in ``rows_seen`` and cannot be mapped, deleted or quarantined,
    so without its own bucket it would fall through every branch while the
    op reported success — the exact silent-bucketing failure this manifest
    exists to prevent, and one the totals identity could not catch because
    the identity would simply never be checked against it.
    """
    _, _ = fakes
    r = await project_id_backfill(_payload())
    assert [row["id"] for row in r["no_directory"]] == ["memory:9"]
    assert r["totals"]["rows_no_directory"] == 1


@pytest.mark.asyncio
async def test_apply_refuses_while_any_row_has_no_directory(fakes):
    """No acknowledgement flag exists for this class, and that is deliberate.

    A row with no directory has no basis for ANY of the three decisions the
    other gates cover — not a mapping, not a cohort, and not a quarantine
    (there is nothing to preserve). The operator's move is to fix or forget
    those rows, not to wave them through.
    """
    storage, _ = fakes
    r = await project_id_backfill(
        _payload(dry_run=False, quarantine_unmapped=True, confirm_deletes=True)
    )
    assert r["ok"] is False
    assert r["reason"] == "rows_without_a_directory_context"
    assert storage.mutations == []


@pytest.mark.asyncio
async def test_wiki_scan_does_not_pull_page_bodies(fakes):
    """``content`` is projected for ``memory`` only.

    It feeds exactly one predicate (D4's) and both delete cohorts are
    ``memory`` cohorts, so projecting it for ``wiki_page`` would pull 2,343
    full page bodies — every ADR body among them — that nothing reads. The
    fakes carry three-character bodies, so nothing else in this file would
    ever show that cost.
    """
    storage, _ = fakes
    await project_id_backfill(_payload())
    wiki_selects = [
        q
        for q, _ in storage.statements
        if q.strip().upper().startswith("SELECT") and "wiki_page" in q
    ]
    assert wiki_selects, "no wiki_page SELECT was issued"
    assert all("content" not in q for q in wiki_selects), wiki_selects


# ── string-tagged rows must not sink the whole apply ────────────────────────


class _TagShapeStorage:
    """Records statements; answers the cohort SELECT with *rows*."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, dict | None]] = []

    def _q(self, query: str, params: dict | None = None) -> list[dict]:
        self.statements.append((query, params))
        if query.strip().upper().startswith("SELECT"):
            return [dict(r) for r in self.rows]
        return []

    def _extract_id(self, raw: Any) -> int:
        return int(str(raw).split(":")[-1])

    def tag_writes(self) -> dict[int, list]:
        """``{row id: tags written}`` for every tag UPDATE issued."""
        return {
            p["id"]: p["tags"] for q, p in self.statements if p and "tags" in p and "SET tags" in q
        }


class TestGlobalTagSurvivesNonArrayTags:
    """The reach tag must reach rows whose ``tags`` is a JSON string.

    Found on the sandbox VM 2026-08-15, twice. First the apply died with

        SurrealDB error: Incorrect arguments for function array::union().
        Argument 1 was the wrong type. Expected `array` but found
        `'["performance", "surreal-v3", "migration", "startup", "bugfix"]'`

    because one UPDATE carried both ``project_id = $pid`` and a set-based
    ``array::union`` — so a single string-tagged row rejected the whole
    statement and every row in that ``directory_context`` got NEITHER the
    tag NOR its project_id. Splitting the statement fixed the coupling; the
    union itself still could not tag those rows.

    So the tag step is a read-modify-write in Python. Both shapes are
    normalised, which REPAIRS the string rows on the way past — dropping
    them would silently narrow those rows to a single project, reversing
    the decision the ``global`` sentinel split exists to honour (owner and
    reach are separate axes, §1.4).
    """

    def _manifest(self, add_global_tag: bool = True) -> dict:
        return {
            "deletes": [],
            "quarantine": [],
            "updates": [
                {
                    "table": "memory",
                    "directory_context": "global" if add_global_tag else _YADGAR,
                    "project_id": "local/aws-work",
                    "rows": 3,
                    "add_global_tag": add_global_tag,
                }
            ],
        }

    def test_project_id_stamp_is_not_coupled_to_the_tag_write(self) -> None:
        """The stamp must be its own statement — it can never fail on tags."""
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([])
        _apply(storage, self._manifest())

        stamps = [q for q, _ in storage.statements if "project_id = $pid" in q and "tags" not in q]
        assert stamps, "project_id stamp is still coupled to the tag write"

    def test_string_tagged_row_is_repaired_to_an_array_and_keeps_its_tags(self) -> None:
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([{"id": "memory:42", "tags": '["performance", "surreal-v3"]'}])
        _apply(storage, self._manifest())

        written = storage.tag_writes()
        assert 42 in written, "string-tagged row never got the reach tag"
        assert isinstance(written[42], list)
        assert written[42] == ["performance", "surreal-v3", "global"]

    def test_array_tagged_row_is_tagged_normally(self) -> None:
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([{"id": "memory:7", "tags": ["a", "b"]}])
        _apply(storage, self._manifest())

        assert storage.tag_writes()[7] == ["a", "b", "global"]

    def test_already_tagged_row_is_left_alone(self) -> None:
        """Idempotence: re-running must not duplicate the tag or rewrite rows."""
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([{"id": "memory:9", "tags": ["x", "global"]}])
        _apply(storage, self._manifest())

        assert storage.tag_writes() == {}

    def test_unparseable_tags_are_preserved_not_dropped(self) -> None:
        """A value that is not JSON is still one tag — losing it is data loss."""
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([{"id": "memory:5", "tags": "not-json-at-all"}])
        _apply(storage, self._manifest())

        assert storage.tag_writes()[5] == ["not-json-at-all", "global"]

    def test_no_tag_work_when_the_cohort_is_not_global(self) -> None:
        """Ordinary directories stay one set-based UPDATE — no per-row cost."""
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([{"id": "memory:1", "tags": '["x"]'}])
        _apply(storage, self._manifest(add_global_tag=False))

        assert storage.tag_writes() == {}
        assert not [q for q, _ in storage.statements if q.strip().upper().startswith("SELECT")]

    def test_uses_only_surql_the_repo_already_proves(self) -> None:
        """No `type::is::*` and no `!` negation.

        The first attempt at this fix gated a set-based union on
        `type::is::array(tags)`, which SurrealDB v3.1.5 answered with 400
        Bad Request — a function this repo uses nowhere else. The shapes
        below (`SELECT ... WHERE col = $p`, `UPDATE type::record(...)`) are
        the ones every other storage module already issues.
        """
        from yadgar.backend.admin_exec.project_backfill import _apply

        storage = _TagShapeStorage([{"id": "memory:3", "tags": ["q"]}])
        _apply(storage, self._manifest())

        joined = " ".join(q for q, _ in storage.statements)
        assert "type::is" not in joined
        assert "array::union" not in joined
