"""Server-mode retry for SurrealDB's retryable transaction-write conflict.

Sibling of ``test_c9_write_retry.py``, which pins the same contract for the
EMBEDDED transport (``_q_embedded`` retries once on a transient transaction
failure, task #323).  The HTTP transport (``_q_server``) had no equivalent, so
a conflict SurrealDB itself labels ``"This transaction can be retried"``
surfaced to the caller as a hard ``RuntimeError``.

Measured mechanism (car H, 2026-08-27): ``test_audit_anchors.py`` /
``test_anchor_hygiene_signals.py`` failed a *different* test on 3 of 7 runs at
the default ``-n 4``, and still failed at ``-n 0``.  Instrumenting every
``httpx.Client.post`` showed ZERO other in-flight requests and an all-MainThread
10-second window at conflict time — the contending writer is SurrealDB's own
internal work over the key range the per-test wipe had just deleted, not another
application thread.  The error is transient by construction and the server says
so in the message.

REPLAY SAFETY is the whole contract here, because SurrealDB's ``/sql`` endpoint
executes each ``;``-separated statement in its OWN transaction unless the body
is wrapped in ``BEGIN``/``COMMIT``.  Re-POSTing a body whose earlier statements
already committed would double-apply them.  So a retry fires only when nothing
non-idempotent can have committed:

  * the body is an explicit ``BEGIN … COMMIT`` transaction (a conflict rolls the
    WHOLE body back), or
  * the failing entry is the FIRST non-``LET`` statement (index == the number of
    bound params), so only ``LET`` bindings — which have no persistent effect —
    preceded it.

Any other response shape falls through to the raise, which is the safe default.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yadgar._shared.storage import StorageEngine

_CONFLICT = "Transaction conflict: Transaction write conflict. This transaction can be retried"
# What every OTHER statement in a failed BEGIN…COMMIT body reports. It names no
# conflict, so a retry decision taken on the FIRST error alone misses the real one.
_COMPANION = "The query was not executed due to a failed transaction"


@pytest.fixture()
def storage():
    s = StorageEngine.__new__(StorageEngine)
    s._db_url = "http://127.0.0.1:9999"  # force server path
    s._http = MagicMock()
    return s


def _resp(entries: list[dict]) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = entries
    return r


def _responses(storage, sequence: list[list[dict]]) -> dict:
    """Wire _http.post to return each entry-list in turn; count the posts."""
    calls = {"n": 0}

    def _post(*_a, **_kw):
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return _resp(sequence[idx])

    storage._http.post.side_effect = _post
    return calls


def test_conflict_then_ok_retries_and_returns(storage):
    """A retryable conflict on the first non-LET statement is replayed."""
    calls = _responses(
        storage,
        [
            [{"status": "OK", "result": None}, {"status": "ERR", "detail": _CONFLICT}],
            [{"status": "OK", "result": None}, {"status": "OK", "result": [{"id": 1}]}],
        ],
    )

    # UPDATE, not CREATE: only statements that are idempotent under replay are
    # retried (see _is_idempotent_under_replay). A CREATE is refused — proved by
    # test_a_create_is_never_replayed below.
    out = storage._q_server("UPDATE type::record('memory', $id) SET heat = 0.5", {"id": 1})

    assert out == [{"id": 1}]
    assert calls["n"] == 2, f"expected one retry (2 posts), got {calls['n']}"


def test_clean_write_posts_once(storage):
    """Anti-regression: a clean write must not pay any retry overhead."""
    calls = _responses(storage, [[{"status": "OK", "result": [{"id": 1}]}]])

    storage._q_server("UPDATE memory:1 SET heat = 0.5", None)

    assert calls["n"] == 1, f"clean write should post once, got {calls['n']}"


def test_conflict_every_attempt_raises(storage):
    """The last attempt's failure surfaces — never swallowed, never silent."""
    calls = _responses(storage, [[{"status": "ERR", "detail": _CONFLICT}]])

    with pytest.raises(RuntimeError, match="Transaction write conflict"):
        storage._q_server("UPDATE memory:1 SET heat = 0.5", None)

    assert calls["n"] > 1, "a retryable conflict must be retried at least once"


def test_conflict_behind_a_companion_message_is_still_retried(storage):
    """The retry decision scans EVERY error entry, not just the first.

    Measured 2026-08-27 on `insert_wiki_page` (a 23-param BEGIN body): the
    statement that conflicted reported the conflict, every other statement
    reported the companion "not executed due to a failed transaction", and the
    companion landed first. Judging retryability on the first error alone made
    the replay fire or not fire on statement order — i.e. at random.
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},  # LET $pid
                {"status": "ERR", "detail": _COMPANION},  # BEGIN, collateral
                {"status": "ERR", "detail": _CONFLICT},  # the real cause
            ],
            [
                {"status": "OK", "result": None},
                {"status": "OK", "result": None},
                {"status": "OK", "result": [{"ok": True}]},
            ],
        ],
    )

    out = storage._q_server(
        "BEGIN TRANSACTION;\nCREATE type::record('wiki_page', $pid) SET title = 'x';\n"
        "COMMIT TRANSACTION",
        {"pid": 1},
    )

    assert out == [{"ok": True}]
    assert calls["n"] == 2, f"expected one retry (2 posts), got {calls['n']}"


def test_companion_message_alone_is_not_retried(storage):
    """No entry names a conflict — the transaction failed for some other reason."""
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},
                {"status": "ERR", "detail": _COMPANION},
                {"status": "ERR", "detail": "Parse error: unexpected token"},
            ]
        ],
    )

    with pytest.raises(RuntimeError, match="failed transaction"):
        storage._q_server(
            "BEGIN TRANSACTION;\nCREATE type::record('wiki_page', $pid) SET;\nCOMMIT TRANSACTION",
            {"pid": 1},
        )

    assert calls["n"] == 1, f"no conflict named — must not replay, got {calls['n']} posts"


def test_non_conflict_error_is_not_retried(storage):
    """A genuine query error is NOT transient — raise on the first response."""
    calls = _responses(storage, [[{"status": "ERR", "detail": "Parse error: unexpected token"}]])

    with pytest.raises(RuntimeError, match="Parse error"):
        storage._q_server("CREATE memory:1 SET", None)

    assert calls["n"] == 1, f"a permanent error must not be replayed, got {calls['n']} posts"


def test_multi_statement_conflict_after_a_commit_is_not_retried(storage):
    """Replay safety: an earlier statement already committed, so do NOT re-POST.

    One bound param (one ``LET``), then two statements.  The conflict lands on
    the SECOND statement (entry index 2 > 1 LET), which means the first
    statement's own transaction already committed.  Replaying the body would
    apply it twice.
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},
                {"status": "OK", "result": []},
                {"status": "ERR", "detail": _CONFLICT},
            ]
        ],
    )

    with pytest.raises(RuntimeError, match="Transaction write conflict"):
        storage._q_server("DELETE memory; UPSERT counter:memory SET val = $v", {"v": 1})

    assert calls["n"] == 1, (
        f"a body with an already-committed statement must NOT be replayed, got {calls['n']} posts"
    )


def test_multi_statement_conflict_on_the_FIRST_statement_is_not_retried(storage):
    """The other half: `/sql` keeps going after a failure, so what FOLLOWS committed.

    `err_index == n_lets` alone reads this as safe — the conflict is on the
    first non-``LET`` statement, nothing with a persistent effect preceded it.
    But ``/sql`` runs each ``;``-separated statement in its own transaction and
    does NOT stop at the first error, so the SECOND statement ran and committed
    while the first rolled back. A replay re-applies it.

    This is the exact shape of the test-suite wipe
    (``DELETE memory; UPSERT counter:memory SET val = (val ?? 0) + 1``), where a
    replay would double-increment the id counter — a desync that blocks every
    subsequent write.

    No production caller is exposed (all 17 multi-statement ``_q`` sites wrap in
    ``BEGIN``, audited 2026-08-27), but the rule failed OPEN, so the next
    multi-statement caller would have inherited a silent double-apply.
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},  # LET $v
                {"status": "ERR", "detail": _CONFLICT},  # DELETE — conflicted, rolled back
                {"status": "OK", "result": []},  # UPSERT — RAN ANYWAY, committed
            ]
        ],
    )

    with pytest.raises(RuntimeError, match="Transaction write conflict"):
        storage._q_server("DELETE memory; UPSERT counter:memory SET val = $v", {"v": 1})

    assert calls["n"] == 1, (
        "a non-atomic multi-statement body must NOT be replayed even when the conflict is "
        f"on the first statement — a later statement already committed; got {calls['n']} posts"
    )


def test_single_statement_conflict_is_still_retried(storage):
    """The single-statement path must survive the multi-statement guard.

    Guards that over-refuse are their own failure: this is the common case the
    retry exists for, and it must keep working.
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},  # LET $v
                {"status": "ERR", "detail": _CONFLICT},
            ],
            [
                {"status": "OK", "result": None},
                {"status": "OK", "result": [{"ok": True}]},
            ],
        ],
    )

    out = storage._q_server("UPSERT counter:memory SET val = $v", {"v": 1})

    assert out == [{"ok": True}]
    assert calls["n"] == 2, f"expected one retry (2 posts), got {calls['n']}"


def test_begin_transaction_statement_is_retried_even_late_in_the_batch(storage):
    """An explicit BEGIN…COMMIT statement rolls back whole — replay is safe.

    Two things this shape pins, and BOTH are needed to make it discriminating:

    * Bound params.  EVERY production ``BEGIN … COMMIT`` write binds them
      (``wiki.py``'s page+version write, ``queue.py``'s file-hash write), so the
      wire body starts with ``LET $…``.  A BEGIN rule that read the body instead
      of the caller's statement would be dead exactly where multi-statement
      atomicity is the point.
    * The conflict lands LATE — on the commit, past every inner statement — so
      ``err_index > n_lets`` and the first-non-LET clause does NOT cover it.
      With the conflict at ``err_index == n_lets`` the test passes either way
      and proves nothing about the BEGIN rule.
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},  # LET $pid
                {"status": "OK", "result": None},  # LET $vid
                {"status": "OK", "result": None},  # BEGIN
                {"status": "OK", "result": []},  # CREATE wiki_page
                {"status": "OK", "result": []},  # CREATE wiki_page_version
                {"status": "ERR", "detail": _CONFLICT},  # COMMIT
            ],
            [
                {"status": "OK", "result": None},
                {"status": "OK", "result": None},
                {"status": "OK", "result": None},
                {"status": "OK", "result": []},
                {"status": "OK", "result": []},
                {"status": "OK", "result": [{"ok": True}]},
            ],
        ],
    )

    out = storage._q_server(
        "BEGIN TRANSACTION;\n"
        "CREATE type::record('wiki_page', $pid) SET title = 'x';\n"
        "CREATE type::record('wiki_page_version', $vid) SET page_id = $pid;\n"
        "COMMIT TRANSACTION",
        {"pid": 1, "vid": 2},
    )

    assert out == [{"ok": True}]
    assert calls["n"] == 2, f"expected one retry (2 posts), got {calls['n']}"


def test_a_create_is_never_replayed(storage):
    """A CREATE is refused even on a conflict that IS retryable.

    OBSERVED 2026-08-28 in a full-suite run: a single-statement CREATE
    reported a retryable conflict, the replay fired, and the second attempt
    failed with ``Database record `wiki_page:1` already exists``. The first
    attempt had COMMITTED while reporting a conflict, so the replay applied
    it twice — the exact case the module's own docstring had called
    hypothetical.

    Re-applying a CREATE is never harmless, so it is no longer replayed at
    all. This costs one caller-visible error on a genuine transient conflict;
    the alternative is silent duplication.
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},  # LET $id
                {"status": "ERR", "detail": _CONFLICT},
            ]
        ],
    )

    with pytest.raises(RuntimeError, match="Transaction write conflict"):
        storage._q_server("CREATE type::record('memory', $id) SET heat = 0.5", {"id": 1})

    assert calls["n"] == 1, f"a CREATE must never be replayed, got {calls['n']} posts"


def test_a_self_referencing_upsert_is_never_replayed(storage):
    """`val = (val ?? 0) + 1` reads its own prior value, so a replay double-counts.

    This is the id-counter shape (`UPSERT counter:memory SET val = (val ?? 0) + 1`).
    A double-increment puts the counter ahead of MAX(id), which is the desync
    that makes every subsequent insert fail with "already exists".
    """
    calls = _responses(
        storage,
        [
            [
                {"status": "OK", "result": None},
                {"status": "ERR", "detail": _CONFLICT},
            ]
        ],
    )

    with pytest.raises(RuntimeError, match="Transaction write conflict"):
        storage._q_server("UPSERT counter:memory SET val = (val ?? 0) + $n", {"n": 1})

    assert calls["n"] == 1, (
        f"a self-referencing UPSERT must never be replayed, got {calls['n']} posts"
    )
