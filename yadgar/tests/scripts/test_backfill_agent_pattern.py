"""Bug-bag-2 train 2026-08-23, C5 — backfill_agent_pattern_from_wiki tests.

Tasks 200 / 268 / 90. Pure-function tests against the helpers in
``scripts/backfill_agent_pattern_from_wiki.py`` — no DB, no fixture. The
side-effecting apply path is integration territory; this file pins the
non-trivial decisions on the pure helpers:

  * ``_slug_to_name`` derives the ledger ``name`` from the wiki slug
    convention and is reversible for every well-formed slug;
  * ``_content_hash`` matches the algorithm in
    ``yadgar/core/server/tools/agent_prompts.py`` so a cross-engine
    invariant comparison will read the rows back as equal;
  * ``_classify_page_type`` honours the ``agent_pattern`` /
    ``agent_discipline`` split and the legacy ``agent_prompt`` type;
  * ``_build_rows_for_apply`` filters out rows whose page_type is outside
    scope, normalises content to a string, and stamps content_hash from the
    body bytes the ledger row will be pinned to;
  * ``backfill`` is idempotent: a second call against the same wiki corpus
    with the ledger state reflected in the first call's output produces
    ``rows_inserted == 0``.

Run via ``pytest yadgar/tests/scripts/test_backfill_agent_pattern.py``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from backfill_agent_pattern_from_wiki import (  # noqa: E402
    _build_rows_for_apply,
    _classify_page_type,
    _content_hash,
    _slug_to_name,
    backfill,
)


class TestSlugToName:
    def test_standard_pattern_slug(self):
        assert _slug_to_name("agent-prompt-caveman-builder") == "caveman-builder"

    def test_standard_discipline_slug(self):
        # Discipline slug convention is symmetric; the helper is a no-op for
        # discipline slugs because the contract name == slug for those.
        assert _slug_to_name("agent-discipline-test-driven") == "agent-discipline-test-driven"

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
    def test_filters_unknown_page_type(self):
        wiki = [
            {"slug": "agent-prompt-x", "content": "body", "page_type": "agent_pattern"},
            {"slug": "adr-0001", "content": "body", "page_type": "adr"},
        ]
        out = _build_rows_for_apply(wiki, page_type_filter="both")
        assert [r["body_slug"] for r in out] == ["agent-prompt-x"]

    def test_filter_respects_page_type_filter(self):
        wiki = [
            {"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"},
            {"slug": "agent-discipline-y", "content": "b", "page_type": "agent_discipline"},
        ]
        patterns = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert [r["body_slug"] for r in patterns] == ["agent-prompt-x"]
        disciplines = _build_rows_for_apply(wiki, page_type_filter="agent_discipline")
        assert [r["body_slug"] for r in disciplines] == ["agent-discipline-y"]

    def test_stamps_content_hash(self):
        body = "abc"
        wiki = [{"slug": "agent-prompt-x", "content": body, "page_type": "agent_pattern"}]
        out = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert out[0]["content_hash"] == _content_hash(body)

    def test_skips_rows_without_slug(self):
        # A row with no slug cannot be keyed and would break the INSERT.
        wiki = [{"slug": "", "content": "x", "page_type": "agent_pattern"}]
        out = _build_rows_for_apply(wiki, page_type_filter="both")
        assert out == []

    def test_skips_rows_whose_content_is_not_a_string(self):
        # SurrealDB may hand back content as None for an empty page; the
        # apply path needs a string for the hash.
        wiki = [
            {"slug": "agent-prompt-x", "content": None, "page_type": "agent_pattern"},
            {"slug": "agent-prompt-y", "content": 42, "page_type": "agent_pattern"},
        ]
        out = _build_rows_for_apply(wiki, page_type_filter="both")
        assert out == []

    def test_name_derived_from_slug(self):
        wiki = [
            {
                "slug": "agent-prompt-caveman-builder",
                "content": "x",
                "page_type": "agent_pattern",
            }
        ]
        out = _build_rows_for_apply(wiki, page_type_filter="agent_pattern")
        assert out[0]["name"] == "caveman-builder"
        assert out[0]["body_slug"] == "agent-prompt-caveman-builder"


class TestBackfillIdempotence:
    """Two-run backfill against a fake storage — second call inserts zero."""

    def test_second_run_inserts_zero_when_first_already_ran(self, monkeypatch):
        wiki = [
            {"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"},
            {"slug": "agent-prompt-y", "content": "b", "page_type": "agent_pattern"},
        ]
        storage = _FakeStorage(wiki=wiki, ledger_names=set())
        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            lambda storage, *, row, project_id: storage.add_ledger_name(row["name"]),
        )
        first = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert first["rows_inserted"] == 2
        assert first["rows_already_present"] == 0
        assert first["rows_failed"] == 0

        # Second run: the ledger already has both names.
        second = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert second["rows_inserted"] == 0
        assert second["rows_already_present"] == 2

    def test_dry_run_does_not_insert(self, monkeypatch):
        wiki = [{"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki, ledger_names=set())
        inserted = {"count": 0}

        def _should_not_run(*args, **kwargs):
            inserted["count"] += 1

        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            _should_not_run,
        )
        result = backfill(storage, apply_changes=False, page_type="agent_pattern")
        assert result["rows_inserted"] == 0
        assert result["rows_already_present"] == 0
        assert inserted["count"] == 0

    def test_failed_insert_increments_rows_failed(self, monkeypatch):
        wiki = [{"slug": "agent-prompt-x", "content": "a", "page_type": "agent_pattern"}]
        storage = _FakeStorage(wiki=wiki, ledger_names=set())

        def _explode(*args, **kwargs):
            raise RuntimeError("write-side boom")

        monkeypatch.setattr(
            "backfill_agent_pattern_from_wiki._insert_one",
            _explode,
        )
        result = backfill(storage, apply_changes=True, page_type="agent_pattern")
        assert result["rows_failed"] == 1
        assert result["rows_inserted"] == 0
        assert result["flagged"][0]["slug"] == "agent-prompt-x"


class _FakeStorage:
    """Minimal in-memory storage stand-in for the backfill helpers."""

    def __init__(self, *, wiki: list[dict], ledger_names: set[str]):
        self._wiki = list(wiki)
        self._ledger = set(ledger_names)

    def list_wiki_pages(
        self,
        *,
        project_id: str | None,
        page_type: str,
        from_slug: str,
        limit: int,
    ) -> list[dict]:
        rows = list(self._wiki)
        if from_slug:
            rows = [r for r in rows if r.get("slug", "") > from_slug]
        if limit:
            rows = rows[:limit]
        return rows

    def list_agent_prompt_rows(self) -> list[dict]:
        return [{"name": n} for n in sorted(self._ledger)]

    def list_agent_discipline_rows(self) -> list[dict]:
        return []

    def add_ledger_name(self, name: str) -> None:
        self._ledger.add(name)
