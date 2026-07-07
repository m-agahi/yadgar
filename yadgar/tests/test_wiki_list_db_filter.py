"""Roadmap: wiki_list LIMIT + slug_prefix pushed to SurrealQL.

After the refactor, list_wiki_pages accepts limit + slug_prefix and
embeds them into the SurrealQL query instead of slicing in Python.

Tests verify via SQL inspection (mock the _q method).
"""

from unittest.mock import MagicMock

import pytest

from yadgar._shared.storage import StorageEngine


@pytest.fixture()
def storage(tmp_path):
    s = StorageEngine.__new__(StorageEngine)
    s._q = MagicMock(return_value=[])
    s._rows_to_dicts = MagicMock(return_value=[])
    return s


def test_limit_pushed_to_surreal_query(storage):
    """LIMIT must appear in the SurrealQL, not in Python slice."""
    storage.list_wiki_pages(limit=5)

    assert storage._q.called
    call_args = storage._q.call_args
    sql = call_args[0][0]
    assert "LIMIT" in sql.upper(), f"Expected LIMIT in SurrealQL, got: {sql!r}"


def test_limit_param_bound(storage):
    """LIMIT value must be passed as a bound param, not interpolated."""
    storage.list_wiki_pages(limit=7)

    call_args = storage._q.call_args
    params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
    # Params dict must contain the limit value
    assert 7 in params.values(), f"Expected limit=7 in bound params, got {params!r}"


def test_slug_prefix_pushed_to_surreal_query(storage):
    """slug_prefix filter must appear in SurrealQL, not Python list comp."""
    storage.list_wiki_pages(slug_prefix="arch-")

    call_args = storage._q.call_args
    sql = call_args[0][0]
    # Either string::starts_with or LIKE in the query
    assert (
        "string::starts_with" in sql.lower()
        or "starts_with" in sql.lower()
        or "LIKE" in sql.upper()
    ), f"Expected DB-side prefix filter in SurrealQL, got: {sql!r}"


def test_category_filter_still_works(storage):
    """category= filter must still generate a WHERE clause."""
    storage.list_wiki_pages(category="architecture")

    call_args = storage._q.call_args
    sql = call_args[0][0]
    assert "category" in sql.lower(), f"Expected category in SurrealQL, got: {sql!r}"
    params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
    assert "architecture" in params.values(), (
        f"Expected 'architecture' in bound params, got {params!r}"
    )


def test_no_slug_prefix_omits_filter(storage):
    """Without slug_prefix, the query must not contain a redundant WHERE."""
    storage.list_wiki_pages()
    call_args = storage._q.call_args
    sql = call_args[0][0]
    # Should not contain starts_with or slug filter when prefix is None
    assert "starts_with" not in sql.lower(), (
        f"Unexpected prefix filter without slug_prefix: {sql!r}"
    )
