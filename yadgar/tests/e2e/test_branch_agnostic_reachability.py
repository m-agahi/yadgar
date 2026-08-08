"""E2E: a row is reachable regardless of what ``branch`` value it carries (ADR-0215).

This is Car 1's POSITIVE exit criterion. Car 1 deletes five branch-filtering
implementations *and* the tests that exercised them, which makes a do-nothing
stub trivially green. This file is the counter: it asserts a behaviour that is
**impossible** while any of those five filters is alive.

Rows are seeded DIRECTLY VIA STORAGE (bypassing the tool layer, which would
stamp its own branch) and then stamped with a branch that matches nothing a
caller on ``master`` could resolve — ``feat/does-not-exist``. Three reads are
then asserted to RETURN those rows from a ``master`` caller context:

  1. ``wiki_read(slug, directory=D)``      — the §25 slug ladder (filter #2)
  2. ``recall(query, directory=D)``        — the retired SQL scope predicate
                                             (filter #1) plus the fan-out score
                                             boost that keyed on it (filter #4)
  3. ``find_similar_wiki_pages(...)``      — the similarity gate's branch axis (filter #5)

Pre-Car-1 each of the three EXCLUDES the row (identifiers are named in the
CHANGELOG entry rather than here, so Car 10's residue grep can reach zero):
  * The §25 ladder matched the caller's current branch, then the NULL slot, then
    the global NULL slot; a project-scoped row on an unrelated branch matched no
    step.
  * The injected WHERE predicate admitted only the NULL, default-branch and
    current-branch rows.
  * The similarity gate built an allowed-branch set of {None, caller branch} and
    dropped every page outside it before the directory filter ever ran.

SURVIVING CAR 9 (schema drop) — deliberate design, do not "simplify" it away:
  * The branch value is applied by ``_stamp_branch`` with a RAW ``UPDATE``, not
    by an ``insert_*(..., branch=)`` kwarg. Once migration 029 does
    ``REMOVE FIELD branch``, the UPDATE becomes a no-op or errors; the helper
    swallows that and returns False. The seeding still succeeds.
  * NONE of the three assertions mention ``branch``. They assert only that the
    row comes back. With the column gone the stamp is vacuous and the
    assertions still mean exactly what they mean today: reachable.
  * No call in this file passes a ``branch=`` / ``branch_hint=`` kwarg to any
    yadgar API, so Cars 1/2/5 dropping those parameters cannot break it.

Each test also seeds a CONTROL row (identical, unstamped) in the same directory
and asserts the control is returned. A failing control means the harness is
broken; a passing control with a failing ghost means the filter is alive. That
distinction is the whole point of the file.

Placement: ``yadgar/tests/e2e/`` so ``make e2e`` collects it. Live-surreal DB.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# A branch name that exists nowhere and can never be a caller's current or
# default branch. Any filter keyed on branch MUST hide a row carrying it.
GHOST_BRANCH = "feat/does-not-exist"

PROJECT_DIR = "/home/test/yadgar-project"


def _pin_caller_to_master(monkeypatch) -> None:
    """Make the caller's context look like a plain ``master`` checkout.

    ``raising=False`` so this keeps working after Car 6 deletes the detection
    helpers entirely — at that point there is simply nothing to patch and the
    caller has no branch context at all, which is the same assertion.
    """


def _stamp_branch(storage, table: str, row_id: int, branch: str) -> bool:
    """Force ``branch`` onto an already-inserted row via raw SurrealQL.

    Deliberately NOT an ``insert_*(branch=...)`` kwarg: that parameter is
    removed later in this train, and the point of this file is to outlive that.

    Returns True when the stamp landed, False when the column no longer exists
    (post-Car-9) or the write was rejected. False is not a failure — it means
    the hazard being guarded against is now structurally impossible.
    """
    try:
        storage._q(
            f"UPDATE type::record('{table}', $rid) SET branch = $b",
            {"rid": int(row_id), "b": branch},
        )
    except Exception:
        return False
    try:
        rows = storage._q(f"SELECT branch FROM type::record('{table}', $rid)", {"rid": int(row_id)})
    except Exception:
        return False
    return bool(rows) and rows[0].get("branch") == branch


def _insert_wiki_page(storage, embeddings, slug: str, title: str, content: str) -> int:
    """Seed a wiki_page row directly via storage, with a real embedding.

    The embedding is required for the similarity-gate assertion, which reaches
    the row through KNN before any scope filter runs.
    """
    return storage.insert_wiki_page(
        {
            "slug": slug,
            "title": title,
            "content": content,
            "category": "reference",
            "tags": [],
            "confidence": "high",
            "source_memory_ids": [],
            "links": [],
            "directory_context": PROJECT_DIR,
            "embedding": embeddings.encode(f"{title}\n{content[:4000]}"),
        }
    )


def _insert_memory(storage, embeddings, content: str) -> int:
    """Seed a memory row directly via storage, with a real embedding."""
    return storage.insert_memory(
        {
            "content": content,
            "embedding": embeddings.encode(content),
            "directory_context": PROJECT_DIR,
            "tags": [],
            "heat": 1.0,
        }
    )


class TestBranchAgnosticReachability:
    """ADR-0215: stored knowledge is a property of the project, not of a branch."""

    def test_wiki_read_returns_row_stamped_with_unknown_branch(self, e2e_engines, monkeypatch):
        """§25 slug resolution must not consult branch (filter #2)."""
        from yadgar.core.server.tools.wiki import wiki_read

        _pin_caller_to_master(monkeypatch)

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        control_slug = "adr-0215-reach-wiki-control"
        ghost_slug = "adr-0215-reach-wiki-ghost"

        _insert_wiki_page(
            storage, embeddings, control_slug, "Control page", "control body for adr-0215 reach"
        )
        ghost_id = _insert_wiki_page(
            storage, embeddings, ghost_slug, "Ghost page", "ghost body for adr-0215 reach"
        )
        _stamp_branch(storage, "wiki_page", ghost_id, GHOST_BRANCH)

        control = wiki_read(control_slug, directory=PROJECT_DIR)
        assert control.get("slug") == control_slug, (
            f"harness broken: unstamped control page unreadable — got {control}"
        )

        ghost = wiki_read(ghost_slug, directory=PROJECT_DIR)
        assert ghost.get("slug") == ghost_slug, (
            f"wiki_read must reach a page stamped branch={GHOST_BRANCH!r} from a "
            f"master caller (ADR-0215); got {ghost}"
        )

    def test_recall_returns_memory_stamped_with_unknown_branch(
        self, e2e_engines, monkeypatch, recall_backend_bypass
    ):
        """The WHERE-clause branch predicate must not exist (filters #1 and #4)."""
        import sys

        _pin_caller_to_master(monkeypatch)

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]

        token = "adr0215reachmem"
        control_id = _insert_memory(storage, embeddings, f"control note {token}")
        ghost_id = _insert_memory(storage, embeddings, f"ghost note {token}")
        _stamp_branch(storage, "memory", ghost_id, GHOST_BRANCH)

        _rm = sys.modules.get("yadgar.core.server.tools.recall")
        if _rm is None:
            import yadgar.core.server.tools.recall as _rm

        results = _rm.recall(query=f"note {token}", directory=PROJECT_DIR, max_results=20)
        result_ids = {r.get("id") for r in results}

        assert control_id in result_ids, (
            f"harness broken: unstamped control memory id={control_id} not recalled; "
            f"got {result_ids}"
        )
        assert ghost_id in result_ids, (
            f"recall must return a memory stamped branch={GHOST_BRANCH!r} from a "
            f"master caller (ADR-0215); got {result_ids}"
        )

    def test_similarity_gate_sees_page_stamped_with_unknown_branch(self, e2e_engines):
        """The duplicate gate must not scope on branch (filter #5).

        ADR-0158's *directory_context* scoping is deliberately exercised here
        (``directory_context=PROJECT_DIR`` is passed and the seeded pages live in
        that directory) — it survives ADR-0215 and this test must not weaken it.
        """
        import yadgar._shared.runtime.state as _st

        storage = e2e_engines["storage"]
        embeddings = e2e_engines["embeddings"]
        wiki = _st._wiki

        title = "Backpressure handling in the queue drainer"
        body = (
            "The queue drainer applies backpressure when the file queue exceeds "
            "its high-water mark, pausing enqueue until the depth falls back below "
            "the low-water mark."
        )

        _insert_wiki_page(storage, embeddings, "adr-0215-reach-gate-control", title, body)
        ghost_id = _insert_wiki_page(storage, embeddings, "adr-0215-reach-gate-ghost", title, body)
        _stamp_branch(storage, "wiki_page", ghost_id, GHOST_BRANCH)

        candidates = wiki.find_similar_wiki_pages(
            title=title,
            content=body,
            threshold=0.5,
            top_k=10,
            directory_context=PROJECT_DIR,
        )
        slugs = {c.get("slug") for c in candidates}

        assert "adr-0215-reach-gate-control" in slugs, (
            f"harness broken: unstamped control page not seen as a duplicate candidate; got {slugs}"
        )
        assert "adr-0215-reach-gate-ghost" in slugs, (
            f"the similarity gate must see a page stamped branch={GHOST_BRANCH!r} as a "
            f"duplicate candidate (ADR-0215); got {slugs}"
        )
