"""Regression tests for SurrealQL bind-parameter NAME validation.

The HTTP `/sql` query paths interpolate the param name directly into the query
text (`LET $<name> = <json-value>`) because SurrealDB's `/sql` endpoint has no
JSON bind-var body. Values are JSON-escaped (breakout-safe); names are guarded
by ``_validate_param_keys`` so a hostile/malformed name cannot inject SurrealQL.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.client import _validate_param_keys


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"id": 1},
        {"upd_content": "x", "ver_tags": [], "pid": 3},
        {"_leading_underscore": 1},
        {"p0_id": 1, "p1_content": "y"},
        {"CamelCase123": 1},
    ],
)
def test_valid_param_names_accepted(params) -> None:
    _validate_param_keys(params)  # must not raise


@pytest.mark.parametrize(
    "bad_key",
    [
        "id = 1; DEFINE USER hacker",  # statement injection via the name
        "x $y",  # whitespace
        "1leading_digit",  # not an identifier
        "has-hyphen",
        "has.dot",
        'quote"break',
        "",  # empty
        "with;semicolon",
    ],
)
def test_injection_shaped_names_rejected(bad_key) -> None:
    with pytest.raises(ValueError, match="Invalid SurrealQL parameter name"):
        _validate_param_keys({bad_key: 1})


def test_non_string_key_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid SurrealQL parameter name"):
        _validate_param_keys({123: "v"})
