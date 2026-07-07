"""Unit tests for _PatternsMixin — no database dependency required."""

import pytest

from yadgar._shared.cls_store.patterns import _PatternsMixin


class _ConcretePatterns(_PatternsMixin):
    """Minimal concrete class to expose mixin methods for testing."""


@pytest.fixture
def mixin():
    return _ConcretePatterns()


class TestAbstractToSchemaUnit:
    """Characterization tests for abstract_to_schema — pure-Python, no DB."""

    def test_empty_cluster(self, mixin):
        assert mixin.abstract_to_schema([]) == ""

    def test_meaningful_words_included(self, mixin):
        cluster = [
            {"id": 1, "content": "Set up JWT auth for API", "tags": ["auth"]},
            {"id": 2, "content": "Added JWT verification middleware", "tags": ["auth"]},
            {"id": 3, "content": "JWT token refresh endpoint implemented", "tags": ["auth"]},
        ]
        schema = mixin.abstract_to_schema(cluster)
        assert "jwt" in schema.lower()
        assert schema.startswith("Recurring pattern across 3 observations:")

    def test_common_tags_included(self, mixin):
        cluster = [
            {"id": 1, "content": "Deploy with Docker containers", "tags": ["devops"]},
            {"id": 2, "content": "Docker deployment pipeline setup", "tags": ["devops"]},
            {"id": 3, "content": "Container deployment using Docker", "tags": ["devops"]},
        ]
        schema = mixin.abstract_to_schema(cluster)
        assert "devops" in schema.lower()

    def test_fallback_no_meaningful_words(self, mixin):
        """When all common tokens are stop-words, fallback returns shortest memory."""
        # "the", "and", "for" are all stop-words; no meaningful word survives.
        short = "the and for"
        cluster = [
            {"id": 1, "content": short, "tags": []},
            {"id": 2, "content": "the and for with that", "tags": []},
        ]
        schema = mixin.abstract_to_schema(cluster)
        assert schema == f"Recurring pattern: {short}"

    def test_single_memory(self, mixin):
        """Single-element cluster with unique content should produce a schema."""
        cluster = [{"id": 1, "content": "Use dependency injection for services", "tags": []}]
        schema = mixin.abstract_to_schema(cluster)
        assert isinstance(schema, str)
        assert len(schema) > 0
