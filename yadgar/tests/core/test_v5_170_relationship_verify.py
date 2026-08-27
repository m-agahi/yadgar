"""Car C11-#89: insert_relationship must reject rows pointing at missing entities.

``check_invariants`` reported a relationship row whose endpoint referenced a
non-existent entity id. The bug: ``insert_relationship`` at
``yadgar/_shared/storage/entity.py:134`` and the typed path at line 341 (the
private ``_insert_typed_relationship_impl``) accepted ANY from_id / to_id
without checking the entity table. The fix adds a helper
``_assert_entity_ids_exist`` at the top of the mixin and calls it at both
write sites so a phantom FK is rejected before the CREATE.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def storage(module_storage):
    """Module-scoped StorageEngine (matches _shared/test_knowledge_graph.py)."""
    return module_storage


def _make_entity(storage, name: str) -> int:
    return storage.insert_entity({"name": name, "type": "file"})


class TestInsertRelationshipEndpointCheck:
    """insert_relationship (entity.py:134) must reject missing endpoints."""

    def test_insert_relationship_rejects_missing_from(self, storage):
        to_id = _make_entity(storage, "real_to_89")
        with pytest.raises(ValueError, match="99999"):
            storage.insert_relationship(
                {
                    "source_entity_id": 99999,
                    "target_entity_id": to_id,
                    "relationship_type": "co_occurrence",
                }
            )

    def test_insert_relationship_rejects_missing_to(self, storage):
        from_id = _make_entity(storage, "real_from_89")
        with pytest.raises(ValueError, match="88888"):
            storage.insert_relationship(
                {
                    "source_entity_id": from_id,
                    "target_entity_id": 88888,
                    "relationship_type": "co_occurrence",
                }
            )

    def test_insert_relationship_accepts_real_pair(self, storage):
        from_id = _make_entity(storage, "ok_from_89")
        to_id = _make_entity(storage, "ok_to_89")
        rid = storage.insert_relationship(
            {
                "source_entity_id": from_id,
                "target_entity_id": to_id,
                "relationship_type": "co_occurrence",
            }
        )
        assert rid > 0


class TestTypedRelationshipEndpointCheck:
    """insert_typed_relationship (entity.py:322 → 341) must reject too."""

    def test_second_site_rejects_missing(self, storage):
        real_id = _make_entity(storage, "typed_real_89")
        # 77777 was never inserted; the typed path must raise before the CREATE.
        with pytest.raises(ValueError, match="77777"):
            storage.insert_typed_relationship(
                source_entity_id=77777,
                target_entity_id=real_id,
                relationship_type="co_occurrence",
            )
        # Confirm the real entity was not mutated by the rejected call.
        real_row = storage.get_entity_by_id(real_id)
        assert real_row is not None
        assert real_row["name"] == "typed_real_89"
