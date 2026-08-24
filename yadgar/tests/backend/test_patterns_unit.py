"""Unit tests for _PatternsMixin — no database dependency required."""

import pytest

from yadgar.backend.cls_store.patterns import _PatternsMixin


class _ConcretePatterns(_PatternsMixin):
    """Minimal concrete class to expose mixin methods for testing."""


@pytest.fixture
def mixin():
    return _ConcretePatterns()


class TestAbstractToSchemaUnit:
    """Characterization tests for abstract_to_schema — pure-Python, no DB."""

    def test_empty_cluster(self, mixin):
        # C7c (task #339): empty cluster returns None, consistent with the
        # word-salad gate. The caller (promotion._promote_pattern) handles
        # ``not schema`` so empty string and None are equivalent.
        assert mixin.abstract_to_schema([]) is None

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
        """When all common tokens are stop-words, the gate drops the salad.

        C7c (task #339): a body that survives the degenerate-and-thin
        guards but carries NO identifier / ADR / file signal AND has no
        shared tag is a word salad. The gate discards it before it can
        reach the auto-promotion path. Pre-C7c this asserted the raw
        fallback string was returned; the new contract is None.
        """
        # "the", "and", "for" are all stop-words; no meaningful word survives.
        short = "the and for"
        cluster = [
            {"id": 1, "content": short, "tags": []},
            {"id": 2, "content": "the and for with that", "tags": []},
        ]
        schema = mixin.abstract_to_schema(cluster)
        assert schema is None, "word salad fallback must be dropped by C7c gate"

    def test_single_memory(self, mixin):
        """Single-element cluster with no identifier signal → word salad → None.

        C7c (task #339): "Use dependency injection for services" carries no
        backtick identifier, no ADR, no file ref, no shared tag. Pre-C7c
        the schema string was returned and immediately auto-promoted to
        an anchor; the new contract is None.
        """
        cluster = [{"id": 1, "content": "Use dependency injection for services", "tags": []}]
        schema = mixin.abstract_to_schema(cluster)
        assert schema is None
