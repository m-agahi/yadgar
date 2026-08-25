"""Bug-bag-2 train 2026-08-23, C5 — backfill_agent_pattern_from_wiki tests.

Tasks 200 / 268 / 90. Pure-function tests against the helpers in
``scripts/backfill_agent_pattern_from_wiki.py`` — no DB, no fixture. The
side-effecting apply path is integration territory; this file pins the
non-trivial decisions on the pure helpers:

  * ``_slug_to_name`` derives the ledger ``name`` from the wiki slug
    convention — for BOTH page types — and is reversible for every
    well-formed slug;
  * ``_content_hash`` matches the algorithm in
    ``yadgar/core/server/tools/agent_prompts.py`` so a cross-engine
    invariant comparison will read the rows back as equal;
  * ``_classify_page_type`` honours the ``agent_pattern`` /
    ``agent_discipline`` split and the legacy ``agent_prompt`` type;
  * ``_build_rows_for_apply`` filters out rows whose page_type is outside
    scope, normalises content to a string, stamps content_hash from the
    body bytes the ledger row will be pinned to, and COUNTS every row it
    drops so ``scanned`` reconciles (ADR-0420);
  * ``backfill`` is idempotent: a second call against the same wiki corpus
    with the ledger state reflected in the first call's output produces
    ``rows_inserted == 0``;
  * the wiki reader's keyword set is pinned against the REAL
    ``StorageEngine.list_wiki_pages`` signature, so the fake in this file can
    never again describe a method the product does not have.

Run via ``pytest yadgar/tests/scripts/test_backfill_agent_pattern.py``.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from backfill_agent_pattern_from_wiki import (  # noqa: E402
    _SCAN_KWARGS,
    SKIP_BUCKETS,
    LedgerReadError,
    _build_rows_for_apply,
    _classify_page_type,
    _content_hash,
    _existing_ledger_rows,
    _slug_to_name,
    backfill,
)


class TestSlugToName:
    def test_standard_pattern_slug(self):
        assert _slug_to_name("agent-prompt-caveman-builder") == "caveman-builder"

    def test_standard_discipline_slug(self):
        # ``discipline_save`` (agent_prompts.py) writes the page at slug
        # ``agent-discipline-<name>`` and forwards ``name=<name>`` — the BARE
        # name — to ``save_agent_discipline_row``. A passthrough here would
        # seed ``name="agent-discipline-test-driven"`` where core writes
        # ``name="test-driven"``, so the idempotency key would never match
        # core's own rows and every run would re-insert the discipline half.
        assert _slug_to_name("agent-discipline-test-driven") == "test-driven"

    def test_round_trip_for_pattern(self):
        # Reversible: ``agent-prompt-<name>`` -> ``<name>`` -> ``agent-prompt-<name>``.
        slug = "agent-prompt-something"
        assert f"agent-prompt-{_slug_to_name(slug)}" == slug

    def test_passthrough_for_unconventional_slug(self):
        # Pages outside the convention pass through unchanged — operator
        # authority on slug scheme wins over the helper's guess.
        assert _slug_to_name("legacy-page") == "legacy-page"


class TestContentHash:
    def test_matches_sha256_utf8(self):
        # The cross-engine invariant compares this hash against the one
        # ``_content_hash`` in ``yadgar/core/server/tools/agent_prompts.py``
        # emits when the row is written by ``agent_prompt_save``. They MUST
        # match — a divergent algorithm here would silently degrade every
        # check_page_row_desync check to "always mismatch".
        body = "Hello, world. \n\nThis is the prompt body."
        assert _content_hash(body) == hashlib.sha256(body.encode("utf-8")).hexdigest()

    def test_empty_body(self):
        # Empty string still produces a stable hex digest — empty pages
        # are valid wiki rows and the ledger row mirrors them faithfully.
        assert _content_hash("") == hashlib.sha256(b"").hexdigest()


class TestClassifyPageType:
    def test_agent_pattern_explicit(self):
        assert _classify_page_type("agent-prompt-x", "agent_pattern") == "agent_pattern"

    def test_agent_discipline_explicit(self):
        assert _classify_page_type("agent-discipline-x", "agent_discipline") == "agent_discipline"

    def test_legacy_agent_prompt_defaults_to_pattern(self):
        # Pre-split pages were tagged ``agent_prompt``. Default to
        # ``agent_pattern`` because the legacy type predates the
        # discipline/pattern split.
        assert _classify_page_type("agent-prompt-x", "agent_prompt") == "agent_pattern"

    def test_none_defaults_to_pattern(self):
        # A row with no page_type most likely predates the type split.
        assert _classify_page_type("agent-prompt-x", None) == "agent_pattern"

    def test_unknown_page_type_returns_empty(self):
        # Anything else — ADR, task, prose — is OUT OF SCOPE.
        assert _classify_page_type("adr-0001", "adr") == ""

    def test_empty_string_defaults_to_pattern(self):
        # Empty / falsy page_type matches the None branch.
        assert _classify_page_type("agent-prompt-x", "") == "agent_pattern"


class TestBuildRowsForApply:
    """``_build_rows_for_apply`` returns ``(rows, skips)``.

    The tuple is the ADR-0420 fix: a bare ``list[dict]`` return has nowhere to
    record WHY a row was dropped, so three drop paths were silent and the
    printed report could not attribute them.
    """

    def test_filters_unknown_page_type(self):
        wiki = [
            {"slug": "agent-prompt-x", "content": "body", "page_type": "agent_pattern"},
            {"slug": "adr-0001", "content": "body", "page_type": "adr"},
        ]
        out, skips = _build_rows_for_apply(wiki, page_type_filter="both")
        assert [r["body_slug"] for r in out] == ["agent-prompt-x"]
        assert skips["skipped_unknown_page_type"] == 1

    def test_filter_respects_page_type_filter(self):
        wiki = [
            {"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"},
            {"slug": "agent-discipline-y", "content": "b", "page_type": "agent_discipline"},
        ]
        patterns, pattern_skips = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert [r["body_slug"] for r in patterns] == ["agent-prompt-x"]
        assert pattern_skips["skipped_page_type_filtered"] == 1
        disciplines, disc_skips = _build_rows_for_apply(wiki, page_type_filter="agent_discipline")
        assert [r["body_slug"] for r in disciplines] == ["agent-discipline-y"]
        assert disc_skips["skipped_page_type_filtered"] == 1

    def test_stamps_content_hash(self):
        body = "abc"
        wiki = [{"slug": "agent-prompt-x", "content": body, "page_type": "agent_pattern"}]
        out, _ = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert out[0]["content_hash"] == _content_hash(body)

    def test_skips_rows_without_slug(self):
        # A row with no slug cannot be keyed and would break the INSERT.
        # It is counted, not silently dropped.
        wiki = [{"slug": "", "content": "x", "page_type": "agent_pattern"}]
        out, skips = _build_rows_for_apply(wiki, page_type_filter="both")
        assert out == []
        assert skips["skipped_empty_slug"] == 1

    def test_skips_rows_whose_content_is_not_a_string(self):
        # SurrealDB may hand back content as None for an empty page; the
        # apply path needs a string for the hash.
        wiki = [
            {"slug": "agent-prompt-x", "content": None, "page_type": "agent_pattern"},
            {"slug": "agent-prompt-y", "content": 42, "page_type": "agent_pattern"},
        ]
        out, skips = _build_rows_for_apply(wiki, page_type_filter="both")
        assert out == []
        assert skips["skipped_non_string_content"] == 2

    def test_every_row_lands_in_exactly_one_bucket(self):
        # The reconciliation identity the gate stands on: kept + every skip
        # bucket == rows handed in. If a future drop path forgets its counter
        # this fails.
        wiki = [
            {"slug": "agent-prompt-ok", "content": "a", "page_type": "agent_pattern"},
            {"slug": "adr-0001", "content": "a", "page_type": "adr"},
            {"slug": "agent-discipline-d", "content": "a", "page_type": "agent_discipline"},
            {"slug": "agent-prompt-bad", "content": None, "page_type": "agent_pattern"},
            {"slug": "", "content": "a", "page_type": "agent_pattern"},
        ]
        out, skips = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert set(skips) == set(SKIP_BUCKETS)
        assert len(out) + sum(skips.values()) == len(wiki)

    def test_name_derived_from_slug(self):
        wiki = [
            {
                "slug": "agent-prompt-caveman-builder",
                "content": "x",
                "page_type": "agent_pattern",
            }
        ]
        out, _ = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert out[0]["name"] == "caveman-builder"
        assert out[0]["body_slug"] == "agent-prompt-caveman-builder"

    def test_discipline_name_strips_its_own_prefix(self):
        wiki = [
            {"slug": "agent-discipline-recall-first", "content": "x", "page_type": None},
        ]
        out, _ = _build_rows_for_apply(wiki, page_type_filter="both")
        assert out[0]["name"] == "recall-first"
        assert out[0]["page_type"] == "agent_discipline"


class _FakeLedger:
    """In-memory stand-in for the two ``list_*_rows`` admin ops.

    The ledger is engine #2 (MariaDB): ``list_agent_prompt_rows`` /
    ``list_agent_discipline_rows`` are ``async`` methods on
    ``MariaStorageEngine`` reached over ``_forward_admin``, NOT methods on the
    wiki-side ``StorageEngine``. The previous fake put them on the storage
    object, which is why calling them off a real ``StorageEngine`` was never
    caught.
    """

    def __init__(self, rows: dict[str, dict] | None = None):
        self.rows: dict[str, dict] = dict(rows or {})

    def read(self, *, page_type: str) -> dict[str, dict]:
        if page_type != "agent_pattern":
            return {}
        return dict(self.rows)

    def add(self, name: str, *, content_hash: str = "") -> None:
        self.rows[name] = {"name": name, "content_hash": content_hash}


class TestBackfillIdempotence:
    """Two-run backfill against a fake storage — second call inserts zero."""

    def test_second_run_inserts_zero_when_first_already_ran(self, monkeypatch):
        wiki = [
            {"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"},
            {"slug": "agent-prompt-y", "content": "b", "page_type": "agent_pattern"},
        ]
        storage = _FakeStorage(wiki=wiki)
        ledger = _FakeLedger()
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: ledger.read(page_type=page_type),
        )
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            lambda *, row: ledger.add(row["name"], content_hash=row["content_hash"]),
        )
        first = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert first["rows_inserted"] == 2
        assert first["rows_already_present"] == 0
        assert first["rows_failed"] == 0
        assert first["gate"]["exact_match"] is True

        # Second run: the ledger already has both names.
        second = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert second["rows_inserted"] == 0
        assert second["rows_already_present"] == 2
        assert second["gate"]["exact_match"] is True

    def test_dry_run_does_not_insert(self, monkeypatch):
        wiki = [{"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki)
        inserted = {"count": 0}

        def _should_not_run(*args, **kwargs):
            inserted["count"] += 1

        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: {},
        )
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            _should_not_run,
        )
        result = backfill(storage, apply_changes=False, page_type="agent_pattern")
        assert result["rows_inserted"] == 0
        assert result["rows_already_present"] == 0
        assert result["would_insert"] == 1
        assert inserted["count"] == 0

    def test_failed_insert_increments_rows_failed(self, monkeypatch):
        wiki = [{"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki)

        def _explode(*args, **kwargs):
            raise RuntimeError("write-side boom")

        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: {},
        )
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            _explode,
        )
        result = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert result["rows_failed"] == 1
        assert result["rows_inserted"] == 0
        assert result["flagged"][0]["slug"] == "agent-prompt-x"
        # A failed row is still ACCOUNTED for, so the identity holds even
        # though the run did not finish cleanly. ``rows_failed`` is the
        # signal there, not the gate.
        assert result["gate"]["exact_match"] is True


class TestReportIsAttributable:
    """ADR-0420: every row the report does not act on is attributed."""

    def test_dry_run_gate_is_not_a_bare_false(self, monkeypatch):
        # The old dry-run gate was the literal ``{"exact_match": False}``,
        # unconditionally — printed next to real tallies as if it were data,
        # and reading as "this backfill will not reconcile" about a run that
        # had not happened.
        wiki = [{"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki)
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: {},
        )
        gate = backfill(storage, apply_changes=False, page_type="agent_pattern")["gate"]
        assert gate["applicable"] is False
        assert gate["would_reconcile"] is True
        assert "exact_match" not in gate

    def test_gate_accounts_for_skipped_rows(self, monkeypatch):
        # A skipped row must still reconcile: the three-term identity the CLI
        # docstring advertised (inserted + failed + already_present ==
        # scanned) cannot hold whenever anything is skipped.
        wiki = [
            {"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"},
            {"slug": "agent-prompt-bad", "content": None, "page_type": "agent_pattern"},
        ]
        storage = _FakeStorage(wiki=wiki)
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: {},
        )
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            lambda *, row: None,
        )
        result = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert result["scanned"] == 2
        assert result["rows_inserted"] == 1
        assert result["skipped_non_string_content"] == 1
        assert result["gate"]["exact_match"] is True
        assert result["gate"]["accounted"] == result["scanned"]

    def test_next_id_basis_key_is_gone(self, monkeypatch):
        # ``next_id_basis`` was returned always-zero and printed as if it were
        # data. ``name`` is this ledger's key, not an AUTO_INCREMENT id, so
        # the concept does not apply here at all.
        storage = _FakeStorage(wiki=[])
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: {},
        )
        result = backfill(storage, apply_changes=False)
        assert "next_id_basis" not in result

    def test_content_hash_mismatch_is_populated_not_decorative(self, monkeypatch):
        # ``content_hash_mismatches`` used to be returned always-empty. An
        # already-present row pinned to different bytes than its page is the
        # exact desync check_page_row_desync reports — real operator signal.
        body = "a"
        wiki = [{"slug": "agent-prompt-x", "content": body, "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki)
        ledger = _FakeLedger({"x": {"name": "x", "content_hash": "deadbeef" * 8}})
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: ledger.read(page_type=page_type),
        )
        result = backfill(storage, apply_changes=False, page_type="agent_pattern")
        assert result["rows_already_present"] == 1
        assert len(result["content_hash_mismatches"]) == 1
        entry = result["content_hash_mismatches"][0]
        assert entry["body_slug"] == "agent-prompt-x"
        assert entry["page_hash"] == _content_hash(body)[:16]

    def test_matching_hash_is_not_reported_as_a_mismatch(self, monkeypatch):
        body = "a"
        wiki = [{"slug": "agent-prompt-x", "content": body, "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki)
        ledger = _FakeLedger({"x": {"name": "x", "content_hash": _content_hash(body)}})
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: ledger.read(page_type=page_type),
        )
        result = backfill(storage, apply_changes=False, page_type="agent_pattern")
        assert result["content_hash_mismatches"] == []


class TestLedgerReadNeverDegradesToEmpty:
    """A failed ledger read must RAISE, never read as 'nothing present'.

    An empty already-present set makes the whole corpus look insertable, so
    the apply path would upsert every row and clobber ``purpose`` / ``status``
    install-wide — ADR-0005's duplicate-the-corpus failure mode through a new
    mechanism.
    """

    def test_error_envelope_raises(self, monkeypatch):
        monkeypatch.setattr(
            "yadgar.core.forward._forward_admin",
            lambda op, payload: {"ok": False, "error": "engine #2 not composed"},
        )
        with pytest.raises(LedgerReadError):
            _existing_ledger_rows(page_type="agent_pattern")

    def test_missing_rows_key_raises(self, monkeypatch):
        monkeypatch.setattr(
            "yadgar.core.forward._forward_admin",
            lambda op, payload: {},
        )
        with pytest.raises(LedgerReadError):
            _existing_ledger_rows(page_type="agent_pattern")

    def test_rows_are_keyed_by_name(self, monkeypatch):
        monkeypatch.setattr(
            "yadgar.core.forward._forward_admin",
            lambda op, payload: {"rows": [{"name": "a", "content_hash": "h"}, {"name": None}]},
        )
        out = _existing_ledger_rows(page_type="agent_pattern")
        assert set(out) == {"a"}
        assert out["a"]["content_hash"] == "h"


class TestScanUsesTheRealStorageSignature:
    """Pin the script's wiki reader against the PRODUCT signature.

    The defect this exists to prevent: ``_scan_wiki_pages`` called
    ``list_wiki_pages(project_id=..., page_type=..., from_slug=..., limit=...)``
    — three parameters ``StorageEngine.list_wiki_pages`` has never had. The
    call raised ``TypeError`` on the DRY RUN, before any write, so the script
    could not execute at all. Twenty-one tests passed anyway because
    ``_FakeStorage.list_wiki_pages`` was written to the script's imagined
    signature instead of the product's.

    Same technique as ``test_admin_ledger_list_pattern_composes.py`` — an
    ``inspect.signature`` pin against the real class, so a fake can never
    drift away from it unnoticed again.
    """

    @staticmethod
    def _real_params() -> set[str]:
        from yadgar._shared.storage import StorageEngine

        return {
            p.name
            for p in inspect.signature(StorageEngine.list_wiki_pages).parameters.values()
            if p.name != "self"
        }

    def test_script_only_passes_parameters_the_engine_accepts(self):
        missing = set(_SCAN_KWARGS) - self._real_params()
        assert not missing, (
            f"script passes {sorted(missing)} which StorageEngine.list_wiki_pages does not accept"
        )

    def test_fake_storage_signature_matches_the_engine(self):
        # The fake must accept exactly the keywords the real engine accepts
        # from this script — otherwise the suite green-lights a call shape the
        # product rejects.
        fake_params = {
            p.name
            for p in inspect.signature(_FakeStorage.list_wiki_pages).parameters.values()
            if p.name != "self"
        }
        assert fake_params <= self._real_params()
        assert set(_SCAN_KWARGS) <= fake_params

    def test_engine_has_no_project_id_page_type_or_from_slug(self):
        # The three parameters the broken call invented. Pinned negatively so
        # that if the engine ever DOES grow them, this test fails and the
        # script's scan is revisited deliberately rather than by accident.
        real = self._real_params()
        assert "project_id" not in real
        assert "page_type" not in real
        assert "from_slug" not in real

    def test_scan_narrows_by_slug_prefix_per_page_type(self, monkeypatch):
        # "both" is loop control: one call per page type, each with that
        # type's slug prefix. Never a filter value handed to storage.
        calls: list[dict] = []
        storage = _FakeStorage(wiki=[], record=calls)
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._existing_ledger_rows",
            lambda *, page_type: {},
        )
        backfill(storage, apply_changes=False, page_type="both")
        assert [c["slug_prefix"] for c in calls] == ["agent-prompt-", "agent-discipline-"]
        assert all(c["slug_prefix"] != "both" for c in calls)


class _FakeStorage:
    """Minimal in-memory storage stand-in for the backfill helpers.

    The signature below MUST stay a subset of the real
    ``StorageEngine.list_wiki_pages`` — pinned by
    ``TestScanUsesTheRealStorageSignature``.
    """

    def __init__(self, *, wiki: list[dict], record: list[dict] | None = None):
        self._wiki = list(wiki)
        self._record = record

    def list_wiki_pages(
        self,
        category: str | None = None,
        slug_prefix: str | None = None,
        limit: int | None = None,
        directory: str | None = None,
    ) -> list[dict]:
        if self._record is not None:
            self._record.append(
                {
                    "category": category,
                    "slug_prefix": slug_prefix,
                    "limit": limit,
                    "directory": directory,
                }
            )
        rows = list(self._wiki)
        if slug_prefix:
            rows = [r for r in rows if str(r.get("slug", "")).startswith(slug_prefix)]
        if limit:
            rows = rows[:limit]
        return rows
