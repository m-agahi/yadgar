"""Car 5 (pulled forward) — ``anchor_renew`` tool: the sanctioned anchor-expiry renew surface.

174 memories carry ``migration_grace = true`` and share ONE ``valid_until``.  At that
instant they stop surfacing (every anchor query filters
``valid_until IS NONE OR valid_until > now``), no signal fires (``project.py`` excludes
grace rows by design, ADR-0083), and nothing deletes them — an invisible, undeleted
zombie.  Before this car there was NO sanctioned way to renew them:

  * ``memory_update``'s allowlist is ``{content, tags, is_protected, is_stale,
    importance, tier}`` — it rejects ``valid_until`` and ``migration_grace``.
  * ``db_inspect`` is read-only (VIEWER role).

``anchor_renew`` closes that gap as a DEDICATED tool, deliberately WITHOUT widening
``_MEMORY_UPDATE_ALLOWED`` (which stays a safety boundary rejecting ``heat`` /
``embedding`` / ``id`` / ``created_at``).

Two behaviours here are load-bearing and easy to get wrong:

  1. **Clearing ``migration_grace``** — that flag is what makes an expired row an
     invisible undeleted zombie.  Renewing without clearing it just moves the cliff.
  2. **Never granting immortality to a NORMAL row by omission** — ``_compute_valid_until(
     None, None, None, settings)`` returns ``None`` (no expiry).  Naively reusing it would
     make ``anchor_renew(id, reason="r")`` on a normal (non-immortal) row silently create an
     immortal anchor, which is the opposite of this tool's purpose.  The effective tier
     therefore falls back to the ROW's own stored tier and then to ``conditional`` — a
     normal row can only become immortal via an explicit ``tier="semantic_immortal"``.
     A bare renew on a row that is ALREADY stored ``semantic_immortal`` correctly
     *preserves* that tier (inheriting the row's own state, not "granting by omission").
  3. **A finite ``ttl_days`` must never coexist with an effective ``semantic_immortal``
     tier** — whether that tier came from the explicit ``tier`` argument or was resolved
     from the row's own stored tier.  Checking only the raw argument (and not the
     resolved effective tier) let ``anchor_renew(mid, ttl_days=30, reason="r")`` on a
     stored-immortal row write ``tier="semantic_immortal"`` AND a finite ``valid_until``
     in the same row — manufacturing the exact zombie shape this tool exists to repair.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yadgar.core import server


@pytest.fixture(autouse=True)
def _engines(tmp_path):
    server.init_engines(
        db_path=str(tmp_path / "anchor_renew.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


def _insert_anchor(
    *,
    content: str = "Never auto-apply or auto-import any infrastructure change",
    tags: list[str] | None = None,
    tier: str | None = "conditional",
    valid_until: str | None = None,
    migration_grace: bool | None = None,
    is_protected: bool = True,
) -> int:
    """Insert an anchor row.  Defaults mirror a live anchored memory."""
    storage = server._get_storage()
    embeddings = server._get_embeddings()
    row: dict = {
        "content": content,
        "embedding": embeddings.encode(content),
        "tags": tags if tags is not None else ["_anchor", "yadgar"],
        "store_type": "episodic",
        "directory_context": "/home/user/project",
        "heat": 1.0,
        "importance": 1.0,
        "is_protected": is_protected,
        "is_stale": False,
        "file_hash": None,
        "embedding_model": embeddings.get_model_name(),
    }
    if tier is not None:
        row["tier"] = tier
    if valid_until is not None:
        row["valid_until"] = valid_until
    if migration_grace is not None:
        row["migration_grace"] = migration_grace
    return storage.insert_memory(row)


def _stored(mid: int) -> dict:
    """Read the STORED row straight from the DB (never the tool's return value)."""
    rows = server._get_storage()._q(
        f"SELECT tier, valid_until, migration_grace, tags, "
        f"valid_until IS NONE AS vu_is_none FROM memory:{int(mid)}"
    )
    assert rows, f"memory:{mid} not found"
    return rows[0]


@pytest.mark.usefixtures("admin_backend_bypass")
class TestAnchorRenew:
    def test_ttl_days_sets_bounded_expiry_clears_grace_and_records_reason(self):
        """Exit criterion #1 — the STORED row carries a fresh bounded valid_until,
        migration_grace is gone, and the reason is recorded as an ``anchor:<reason>`` tag.
        """
        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        mid = _insert_anchor(valid_until=past, migration_grace=True)

        before = datetime.now(UTC)
        result = server.anchor_renew(mid, ttl_days=30, reason="r")
        after = datetime.now(UTC)

        row = _stored(mid)

        # valid_until lands inside a bounded range, not merely "not the old value".
        assert row["valid_until"] is not None, "renew must set a valid_until"
        got = datetime.fromisoformat(row["valid_until"])
        assert before + timedelta(days=30) <= got <= after + timedelta(days=30), (
            f"stored valid_until {got} outside [now+30d] bounded range"
        )

        # The zombie-maker must be gone, not merely moved.
        assert row["migration_grace"] in (None, False), (
            f"migration_grace must be cleared, got {row['migration_grace']!r}"
        )

        assert "anchor:r" in (row["tags"] or []), (
            f"renew must record the reason as an anchor:<reason> tag, got {row['tags']!r}"
        )
        assert "_anchor" in (row["tags"] or []), "renew must not strip the _anchor tag"

        # The tool must ECHO the resolved expiry — anchor() does not, which is half
        # the reason time-boxing goes unused.
        assert result.get("valid_until") == row["valid_until"], (
            "anchor_renew must return the resolved valid_until so the caller sees the new expiry"
        )

    def test_semantic_immortal_stores_valid_until_none(self):
        """Exit criterion #2 — tier=semantic_immortal clears expiry entirely.

        DISCRIMINATING against the obvious implementation: ``valid_until`` is
        ``option<string>``, so a Python ``None`` routed through
        ``update_memory_fields`` raises ``Expected 'none | string' but found 'NULL'``.
        Only an explicit ``SET valid_until = NONE`` makes ``valid_until IS NONE`` true —
        and ``IS NONE`` is exactly what every anchor surfacing query tests.
        """
        mid = _insert_anchor(valid_until=(datetime.now(UTC) + timedelta(days=2)).isoformat())

        result = server.anchor_renew(mid, tier="semantic_immortal", reason="hard boundary rule")

        row = _stored(mid)
        assert row["vu_is_none"] is True, (
            f"semantic_immortal must store valid_until IS NONE, got {row['valid_until']!r} "
            "(a JSON null stores as NULL, which IS NONE reports False — the row would "
            "silently stop surfacing)"
        )
        assert row["tier"] == "semantic_immortal", "tier must be written back"
        assert result.get("valid_until") is None

    def test_missing_reason_is_rejected_and_names_the_argument(self):
        """Exit criterion #3 — renewing re-asserts that something deserves a
        compaction-proof slot; 80 of 146 live anchors carry no reason at all.
        """
        mid = _insert_anchor()
        result = server.anchor_renew(mid, ttl_days=30)

        assert result.get("ok") is False or result.get("stored") is False, (
            f"missing reason must be rejected, got {result!r}"
        )
        assert "reason" in str(result.get("reason") or result.get("error") or "").lower(), (
            f"rejection message must name the missing argument, got {result!r}"
        )

        # Rejection must not have mutated the row.
        assert _stored(mid)["valid_until"] is None

    def test_non_anchor_is_rejected_not_silently_promoted(self):
        """Exit criterion #4 — a non-anchor must be refused, never silently promoted.

        Keyed on the ``_anchor`` TAG, not ``is_protected``: the corpus holds ~101
        ``is_protected`` rows WITHOUT the tag (``_active_work`` and friends), and both
        surfacing queries require the tag.  An is_protected-keyed check would happily
        "renew" a system row.
        """
        mid = _insert_anchor(tags=["yadgar", "notes"], tier=None, is_protected=True)

        result = server.anchor_renew(mid, ttl_days=30, reason="r")

        assert result.get("ok") is False or result.get("stored") is False, (
            f"non-anchor must be rejected, got {result!r}"
        )
        assert _stored(mid)["valid_until"] is None, "rejected renew must not mutate the row"

    def test_missing_memory_returns_error_not_crash(self):
        result = server.anchor_renew(999999999, ttl_days=30, reason="r")
        assert isinstance(result, dict)
        assert result.get("ok") is False or "error" in result

    def test_omitting_ttl_and_tier_must_not_grant_immortality(self):
        """The failure mode a naive ``_compute_valid_until`` reuse creates.

        ``_compute_valid_until(None, None, None, settings)`` returns ``None``, so
        passing the caller's bare arguments straight through would make
        ``anchor_renew(id, reason="r")`` produce an anchor that NEVER expires — the
        exact opposite of this tool's purpose, and invisible to every exit criterion
        that only checks the explicit paths.
        """
        mid = _insert_anchor(tier="conditional")

        server.anchor_renew(mid, reason="still needed")

        row = _stored(mid)
        assert row["vu_is_none"] is False and row["valid_until"] is not None, (
            "omitting ttl_days AND tier must fall back to the row's tier (conditional), "
            "NOT grant immortality"
        )
        got = datetime.fromisoformat(row["valid_until"])
        assert got > datetime.now(UTC) + timedelta(days=1), "renewed expiry must be in the future"

    def test_ttl_days_conflicts_with_semantic_immortal(self):
        """A finite TTL and 'never expires' are contradictory — reject rather than
        silently letting one win."""
        mid = _insert_anchor()
        result = server.anchor_renew(mid, ttl_days=30, tier="semantic_immortal", reason="r")
        assert result.get("ok") is False or result.get("stored") is False

    def test_ttl_days_conflicts_with_stored_semantic_immortal_tier(self):
        """Same conflict as above, but the tier comes from the STORED row, not the
        ``tier`` argument (which is ``None`` here).

        Before the fix, ``_validate_anchor_renew_args`` only ever tests the raw
        ``tier`` argument — never the row's own tier — so this call sailed through
        and ``_resolve_anchor_renew_target`` resolved ``effective_tier =
        "semantic_immortal"`` from the stored row while ``_compute_valid_until``
        still honored ``ttl_days`` (its resolution order puts the ``ttl_days``
        branch before the ``semantic_immortal`` branch). The result was a STORED
        row carrying both ``tier="semantic_immortal"`` AND a finite ``valid_until``
        — the exact zombie shape this tool exists to repair, freshly manufactured.

        The row is inserted with ``valid_until`` absent (a coherent immortal state,
        not a pre-existing zombie) so the assertion below is discriminating in only
        one direction: it fails pre-fix (the call writes a finite valid_until) and
        passes post-fix (the call is rejected and the row is untouched).
        """
        mid = _insert_anchor(tier="semantic_immortal", valid_until=None)

        result = server.anchor_renew(mid, ttl_days=30, reason="r")

        row = _stored(mid)
        assert not (row["tier"] == "semantic_immortal" and row["valid_until"] is not None), (
            f"stored row must never carry tier='semantic_immortal' together with a "
            f"finite valid_until, got tier={row['tier']!r} valid_until={row['valid_until']!r}"
        )
        assert result.get("ok") is False, (
            f"ttl_days against an effective (stored) semantic_immortal tier must be "
            f"rejected, got {result!r}"
        )
        # And the row must be provably untouched, not merely "not both set at once".
        assert row["vu_is_none"] is True, "rejected renew must not mutate the row's valid_until"
        assert row["tier"] == "semantic_immortal", "rejected renew must not mutate the row's tier"

    def test_bare_renew_preserves_immortality_of_stored_semantic_immortal_row(self):
        """A bare ``anchor_renew(mid, reason="r")`` on a stored ``semantic_immortal``
        row must keep it immortal — this is CORRECT inheritance of the row's own
        tier (mirroring the ``conditional``/``ephemeral`` fallback tested above),
        not "immortality granted by omission" in the sense the tool guards against
        (which is: a NORMAL row spontaneously becoming immortal because no tier was
        named). Must not regress when the ttl_days-conflict fix lands.
        """
        mid = _insert_anchor(tier="semantic_immortal", valid_until=None)

        result = server.anchor_renew(mid, reason="still needed")

        row = _stored(mid)
        assert row["vu_is_none"] is True, (
            f"bare renew on a stored semantic_immortal row must preserve valid_until "
            f"IS NONE, got {row['valid_until']!r}"
        )
        assert row["tier"] == "semantic_immortal"
        assert result.get("ok") is True
        assert result.get("valid_until") is None

    def test_invalid_tier_is_rejected(self):
        mid = _insert_anchor()
        result = server.anchor_renew(mid, tier="forever", reason="r")
        assert result.get("ok") is False or result.get("stored") is False


@pytest.mark.usefixtures("admin_backend_bypass")
class TestGraceZombieRegression:
    """Exit criterion #5 — THE regression that motivates the whole car."""

    def test_expired_grace_row_surfaces_again_after_renew(self):
        """A ``migration_grace=true`` row with a PAST ``valid_until`` is invisible to
        every anchor surfacing query.  Renewing it must bring it back.

        Both directions are asserted in one test, so a no-op implementation fails:
        the row must be ABSENT before the renew and PRESENT after.
        """
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        mid = _insert_anchor(valid_until=past, migration_grace=True)

        storage = server._get_storage()

        # BEFORE: invisible. This is the zombie state — undeleted, unsurfaced.
        before_ids = {m["id"] for m in storage.get_anchored_memories(limit=100)}
        assert mid not in before_ids, (
            "fixture is wrong: an expired grace row must NOT surface before the renew "
            "(otherwise the test proves nothing)"
        )

        server.anchor_renew(mid, ttl_days=30, reason="user reviewed; keep")

        # AFTER: surfaces again.
        after_ids = {m["id"] for m in storage.get_anchored_memories(limit=100)}
        assert mid in after_ids, (
            "renewed anchor must be returned by the anchor surfacing query again"
        )

        # And it is no longer in migration grace — the cliff is gone, not moved.
        assert _stored(mid)["migration_grace"] in (None, False)

    def test_memory_update_still_rejects_valid_until(self):
        """The gap this car closes, and proof it was closed the RIGHT way.

        Before this car, ``memory_update`` was the only field-patch surface and it
        REJECTS ``valid_until`` — which is precisely why the 15 keep-listed memories
        could not be renewed.  This assertion pins that the safety boundary was NOT
        widened: ``anchor_renew`` is a dedicated tool, not a hole in the allowlist.
        """
        mid = _insert_anchor()
        with pytest.raises(ValueError, match="valid_until"):
            server.memory_update(mid, {"valid_until": "2027-01-01T00:00:00+00:00"})
        with pytest.raises(ValueError, match="migration_grace"):
            server.memory_update(mid, {"migration_grace": False})
