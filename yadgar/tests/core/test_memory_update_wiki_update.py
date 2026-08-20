"""§17 memory_update / wiki_update — patch by integer ID.

Tests:
- memory_update patches allowed fields (content, tags, is_protected, is_stale,
  importance, tier, project_id)
- memory_update rejects unknown/disallowed keys (heat, embedding, id, created_at)
- memory_update preserves heat, access_count, created_at
- memory_update restamps project_id and validates its shape (ledger task 262)
- wiki_update patches allowed fields (content, tags, category, confidence)
- wiki_update rejects unknown/disallowed keys (slug, id, created_at)
- Updates persist (re-fetch confirms change)
"""

import pytest

from yadgar.core import server
from yadgar.tests.core.conftest import TEST_PROJECT_ID


@pytest.fixture(autouse=True, scope="module")
def _engines(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("memory_update_wiki_updat")
    server.init_engines(
        db_path=str(tmp_path / "test_update.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    yield
    server.shutdown()


@pytest.mark.usefixtures("admin_backend_bypass")
class TestMemoryUpdate:
    def _insert_memory(self, content="initial content"):
        return server._get_storage().insert_memory(
            {
                "content": content,
                "tags": ["original-tag"],
                "store_type": "episodic",
                "heat": 0.7,
                "directory_context": "/tmp/test",
                "project_id": TEST_PROJECT_ID,
            }
        )

    def test_memory_update_content(self):
        """memory_update patches content and persists the change."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"content": "updated content"})
        assert result["content"] == "updated content"
        refetched = server.memory_get(mid)
        assert refetched is not None
        assert refetched["content"] == "updated content"

    def test_memory_update_tags(self):
        """memory_update patches tags."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"tags": ["new-tag", "another"]})
        assert "new-tag" in result["tags"]
        refetched = server.memory_get(mid)
        assert refetched is not None
        assert "new-tag" in refetched["tags"]

    def test_memory_update_is_protected(self):
        """memory_update patches is_protected flag."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"is_protected": True})
        assert result["is_protected"] is True

    def test_memory_update_is_stale(self):
        """memory_update patches is_stale flag."""
        mid = self._insert_memory()
        result = server.memory_update(mid, {"is_stale": True})
        assert result["is_stale"] is True

    def test_memory_update_rejects_heat(self):
        """memory_update must reject 'heat' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="heat"):
            server.memory_update(mid, {"heat": 999.0})

    def test_memory_update_rejects_embedding(self):
        """memory_update must reject 'embedding' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="embedding"):
            server.memory_update(mid, {"embedding": b"\x00" * 16})

    def test_memory_update_rejects_id(self):
        """memory_update must reject 'id' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="id"):
            server.memory_update(mid, {"id": 42})

    def test_memory_update_rejects_created_at(self):
        """memory_update must reject 'created_at' key."""
        mid = self._insert_memory()
        with pytest.raises(ValueError, match="created_at"):
            server.memory_update(mid, {"created_at": "2020-01-01"})

    def test_memory_update_rejects_unknown_key(self):
        """memory_update must reject completely unknown keys."""
        mid = self._insert_memory()
        with pytest.raises(ValueError):
            server.memory_update(mid, {"totally_unknown_field": "bad"})

    def test_memory_update_preserves_heat(self):
        """memory_update must not change heat."""
        mid = self._insert_memory()
        before = server.memory_get(mid)
        assert before is not None
        original_heat = before.get("heat")
        server.memory_update(mid, {"content": "changed content"})
        after = server.memory_get(mid)
        assert after is not None
        assert after.get("heat") == original_heat

    def test_memory_update_preserves_created_at(self):
        """memory_update must not change created_at."""
        mid = self._insert_memory()
        before = server.memory_get(mid)
        assert before is not None
        original_ca = before.get("created_at")
        server.memory_update(mid, {"content": "changed"})
        after = server.memory_get(mid)
        assert after is not None
        assert after.get("created_at") == original_ca

    def test_memory_update_returns_updated_record(self):
        """memory_update returns the updated dict."""
        mid = self._insert_memory("old")
        result = server.memory_update(mid, {"content": "new"})
        assert isinstance(result, dict)
        assert result["content"] == "new"


# ── memory_update(fields={"project_id": …}) — ledger task 262 ────────────────


@pytest.mark.usefixtures("admin_backend_bypass")
class TestMemoryUpdateProjectId:
    """``project_id`` is patchable — it is the sole memory scoping key.

    Before task 262 ``_MEMORY_UPDATE_ALLOWED`` was
    ``{content, tags, is_protected, is_stale, importance, tier}``, so
    ``memory.project_id`` had NO restamp path through any MCP surface at all:
    a row stamped with the wrong project was unreachable from every
    project-scoped read (``build_project_scope_clause`` narrows on
    ``project_id = $p OR <global-reach-tag> IN tags``) and nothing could
    correct it. This is the memory half of the wiki fix in ledger task 246.
    """

    def _insert_memory(self, project_id: str, content: str = "restamp probe") -> int:
        return server._get_storage().insert_memory(
            {
                "content": content,
                "tags": ["original-tag"],
                "store_type": "episodic",
                "heat": 0.7,
                "directory_context": "/tmp/test",
                "project_id": project_id,
            }
        )

    def _stored_project_id(self, mid: int):
        """Re-READ the row from storage rather than trusting the return value.

        Deliberate: yadgar write tools have a documented history of reporting
        success for writes they dropped, so asserting on ``memory_update``'s
        own echo would pass against a no-op.
        """
        row = server._get_storage().get_memory(mid)
        assert row is not None
        return row.get("project_id")

    def test_restamp_project_id_changes_the_stored_row(self):
        """The discriminating test: the stored row actually carries the new id."""
        mid = self._insert_memory("wrong-owner/wrong-repo")
        assert self._stored_project_id(mid) == "wrong-owner/wrong-repo"

        result = server.memory_update(mid, {"project_id": "m-agahi/yadgar"})

        assert self._stored_project_id(mid) == "m-agahi/yadgar", (
            "memory_update reported success but the stored row was not restamped"
        )
        assert result["project_id"] == "m-agahi/yadgar"

    def test_restamped_row_is_reachable_from_the_new_project_scope(self):
        """The restamp is what makes the row reachable — the point of the fix.

        Asserts against the real scope predicate (``build_project_scope_clause``)
        rather than the column value alone, because the column is only
        interesting insofar as the project-scoped read finds the row.
        """
        from yadgar._shared.storage.directory import build_project_scope_clause

        mid = self._insert_memory("wrong-owner/wrong-repo", content="scope probe row")
        clause, params = build_project_scope_clause("m-agahi/yadgar")

        def _found() -> bool:
            rows = server._get_storage()._q(
                f"SELECT id FROM memory:{int(mid)} WHERE {clause}", params
            )
            return bool(rows)

        # Guards against a vacuous probe: the SAME query with the row's OWN id
        # must find it, so a False below means the scope predicate excluded the
        # row rather than the record-id selector being malformed.
        own_clause, own_params = build_project_scope_clause("wrong-owner/wrong-repo")
        assert server._get_storage()._q(
            f"SELECT id FROM memory:{int(mid)} WHERE {own_clause}", own_params
        ), "probe is malformed — the row is not found even under its own project"
        assert not _found(), "fixture is vacuous — the row was already in scope"
        server.memory_update(mid, {"project_id": "m-agahi/yadgar"})
        assert _found(), "restamped row is still unreachable from its project scope"

    def test_project_id_empty_rejects(self):
        """Empty string names no project — and would write the NONE literal."""
        mid = self._insert_memory("m-agahi/yadgar")
        with pytest.raises(ValueError, match="must be a non-empty string"):
            server.memory_update(mid, {"project_id": ""})
        assert self._stored_project_id(mid) == "m-agahi/yadgar"

    def test_project_id_none_rejects(self):
        """``None`` nulls the column, reproducing the unreachability being fixed."""
        mid = self._insert_memory("m-agahi/yadgar")
        with pytest.raises(ValueError, match="must be a non-empty string"):
            server.memory_update(mid, {"project_id": None})
        assert self._stored_project_id(mid) == "m-agahi/yadgar"

    def test_project_id_non_string_rejects(self):
        """A non-string value is rejected rather than coerced."""
        mid = self._insert_memory("m-agahi/yadgar")
        with pytest.raises(ValueError, match="must be a non-empty string"):
            server.memory_update(mid, {"project_id": 42})
        assert self._stored_project_id(mid) == "m-agahi/yadgar"

    @pytest.mark.parametrize("sentinel", ["global", "unresolved", "system"])
    def test_project_id_sentinel_rejects(self, sentinel):
        """The ADR-0227 manufactured identities are not settable values.

        Global REACH travels as the Car C7 tag, never as
        ``project_id='global'`` — writing the sentinel here would mint exactly
        the phantom identity ADR-0227 deletes.
        """
        mid = self._insert_memory("m-agahi/yadgar")
        with pytest.raises(ValueError, match="names no project"):
            server.memory_update(mid, {"project_id": sentinel})
        assert self._stored_project_id(mid) == "m-agahi/yadgar"

    def test_disallowed_field_message_names_project_id_as_allowed(self):
        """The rejection message must name the CURRENT allowed set.

        Asserting on the message text because a stale message is part of this
        defect class: the docstring's hand-written "Allowed keys" list had been
        wrong since v5.158 (it omitted ``importance`` / ``tier``), and a caller
        told ``project_id`` is not allowed has no other way to discover that it
        now is. The message renders ``sorted(_MEMORY_UPDATE_ALLOWED)``, so this
        also pins the render and the allowlist together.
        """
        mid = self._insert_memory("m-agahi/yadgar")
        with pytest.raises(ValueError) as exc:
            server.memory_update(mid, {"directory_context": "/tmp/elsewhere"})
        message = str(exc.value)
        assert "directory_context" in message
        assert "project_id" in message
        for field in ("content", "tags", "is_protected", "is_stale", "importance", "tier"):
            assert field in message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("content", "regression content"),
            ("tags", ["regression-tag"]),
            ("is_protected", True),
            ("is_stale", True),
            ("importance", 0.5),
            ("tier", "ephemeral"),
        ],
    )
    def test_pre_existing_allowed_fields_still_work(self, field, value):
        """Regression guard: widening the allowlist must not drop a member."""
        mid = self._insert_memory("m-agahi/yadgar")
        server.memory_update(mid, {field: value})
        row = server._get_storage().get_memory(mid)
        assert row is not None
        assert row[field] == value

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("heat", 999.0),
            ("embedding", b"\x00" * 16),
            ("id", 42),
            ("created_at", "2020-01-01"),
        ],
    )
    def test_safety_boundary_fields_still_rejected(self, field, value):
        """The allowlist exists to reject these — widening must not erode it."""
        mid = self._insert_memory("m-agahi/yadgar")
        with pytest.raises(ValueError, match=field):
            server.memory_update(mid, {field: value})


@pytest.mark.usefixtures("admin_backend_bypass")
class TestWikiUpdate:
    def _insert_wiki(self, slug="test-wiki-update", content="initial content"):
        return server._get_storage().insert_wiki_page(
            {
                "slug": slug,
                "title": "Test Wiki Update",
                "content": content,
                "tags": ["original"],
                "category": "test",
                "status": "approved",
                "confidence": 0.8,
                "project_id": TEST_PROJECT_ID,
            }
        )

    def test_wiki_update_content(self):
        """wiki_update patches content and persists the change."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"content": "updated wiki content"})
        assert result["content"] == "updated wiki content"
        refetched = server.wiki_get(pid)
        assert refetched is not None
        assert refetched["content"] == "updated wiki content"

    def test_wiki_update_tags(self):
        """wiki_update patches tags."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"tags": ["new-tag"]})
        assert "new-tag" in result["tags"]

    def test_wiki_update_category(self):
        """wiki_update patches category."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"category": "architecture"})
        assert result["category"] == "architecture"

    def test_wiki_update_confidence(self):
        """wiki_update patches confidence."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"confidence": 0.99})
        assert abs(result["confidence"] - 0.99) < 0.001

    def test_wiki_update_rejects_slug(self):
        """wiki_update must reject 'slug' key."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError, match="slug"):
            server.wiki_update(pid, {"slug": "new-slug"})

    def test_wiki_update_rejects_id(self):
        """wiki_update must reject 'id' key."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError, match="id"):
            server.wiki_update(pid, {"id": 99})

    def test_wiki_update_rejects_created_at(self):
        """wiki_update must reject 'created_at' key."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError, match="created_at"):
            server.wiki_update(pid, {"created_at": "2020-01-01"})

    def test_wiki_update_rejects_unknown_key(self):
        """wiki_update must reject completely unknown keys."""
        pid = self._insert_wiki()
        with pytest.raises(ValueError):
            server.wiki_update(pid, {"not_a_real_field": "bad"})

    def test_wiki_update_preserves_created_at(self):
        """wiki_update must not change created_at."""
        pid = self._insert_wiki()
        before = server.wiki_get(pid)
        assert before is not None
        original_ca = before.get("created_at")
        server.wiki_update(pid, {"content": "changed"})
        after = server.wiki_get(pid)
        assert after is not None
        assert after.get("created_at") == original_ca

    def test_wiki_update_returns_updated_record(self):
        """wiki_update returns the updated dict."""
        pid = self._insert_wiki()
        result = server.wiki_update(pid, {"content": "new content"})
        assert isinstance(result, dict)
        assert result["content"] == "new content"

    # v5.41.0: version regression guards
    def test_wiki_update_produces_version_row(self):
        """Every wiki_update call produces a new wiki_page_version row (v5.41.0)."""
        from yadgar._shared.storage.migrations import (
            _migration_013_wiki_page_version,  # noqa: PLC0415
        )

        storage = server._get_storage()
        _migration_013_wiki_page_version(storage)  # DDL + seed for existing pages
        pid = self._insert_wiki("ver-update-test", "version update check")
        # pid already has version=1 from insert_wiki_page hook
        server.wiki_update(pid, {"content": "updated once"})
        server.wiki_update(pid, {"content": "updated twice"})

        rows = storage._q(
            "SELECT * FROM wiki_page_version WHERE page_id = $p ORDER BY version ASC",
            {"p": pid},
        )
        # insert: version=1; update 1: version=2; update 2: version=3
        assert len(rows) >= 3, f"Expected ≥3 version rows, got {len(rows)}"
        versions = [r["version"] for r in rows]
        assert 1 in versions
        assert 2 in versions
        assert 3 in versions
