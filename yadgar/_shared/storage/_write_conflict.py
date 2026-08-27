"""Replay of SurrealDB transaction-write conflicts on the HTTP transport.

SurrealDB's optimistic concurrency control rejects a transaction whose key range
another transaction touched first, and labels the rejection in the message
itself: ``"Transaction conflict: Transaction write conflict. This transaction
can be retried"``. The EMBEDDED transport has retried that class since task
#323; ``_q_server`` — the transport every containerised deployment actually
uses — did not, so a transient conflict reached the caller as a hard
``RuntimeError``.

The contending writer is not always another yadgar caller. Measured 2026-08-27
(car H): the anchor-hygiene / audit-anchor suites hit this at ``-n 0`` with ZERO
other in-flight HTTP requests and an all-MainThread window, i.e. against
SurrealDB's own internal work over a key range the per-test wipe had just
deleted. Whatever the contender, the failed transaction rolled back and a replay
is the sanctioned response.

REPLAY SAFETY is the whole design here. SurrealDB's ``/sql`` endpoint runs each
``;``-separated statement in its OWN transaction unless the body is wrapped in
``BEGIN``/``COMMIT``, so re-POSTing a body whose earlier statements already
committed would double-apply them. See ``_replay_is_safe`` for the exact rule.

This lives in its own module rather than inside ``client.py`` because that file
sits 10 lines under the I30 ``file_loc`` cap, and because the safety rule is a
cohesive concept that deserves to be readable on its own.
"""

from __future__ import annotations

import logging
import random
import re
import time

from yadgar._shared.observability.observe import observe

_log = logging.getLogger(__name__)

# Module constants, NOT knobs: a config key would drag in the I25 registration
# surface (config_default_mismatch_allowlist / config_env_only_allowlist) for a
# transport detail no operator tunes.
ATTEMPTS: int = 3
BACKOFF_S: float = 0.02

_RETRYABLE_CONFLICT_RE = re.compile(
    r"transaction (?:write )?conflict|can be retried", re.IGNORECASE
)


@observe(tier="hot")
def _first_err_entry(results: object) -> tuple[int, dict | None]:
    """Return ``(index, entry)`` of the first ``status == "ERR"`` result entry.

    ``(-1, None)`` when every entry is clean, or when the payload is not a list
    of dicts — which the caller then treats as success, matching the pre-retry
    behaviour whose ``for entry in results`` loop simply matched nothing.
    """
    if not isinstance(results, list):
        return -1, None
    for i, entry in enumerate(results):
        if isinstance(entry, dict) and entry.get("status") == "ERR":
            return i, entry
    return -1, None


@observe(tier="hot")
def _any_entry_is_retryable(results: object) -> bool:
    """True when ANY ``ERR`` entry names a conflict SurrealDB calls retryable.

    Deliberately a scan and not a look at the FIRST error. Inside a
    ``BEGIN … COMMIT`` body, the statement that actually conflicted reports the
    conflict while every OTHER statement reports the companion message ``"The
    query was not executed due to a failed transaction"``. Which of those lands
    at the lowest index is an accident of statement order, so judging
    retryability by the first error alone made the retry fire or not fire at
    random — measured 2026-08-27 on ``insert_wiki_page`` (a 23-param BEGIN body),
    where the companion message came first and the conflict was never seen.

    The first error still supplies the raised message, so the failure a caller
    sees is unchanged.
    """
    if not isinstance(results, list):
        return False
    return any(
        isinstance(e, dict)
        and e.get("status") == "ERR"
        and _RETRYABLE_CONFLICT_RE.search(str(e.get("detail") or e.get("result") or ""))
        for e in results
    )


@observe(tier="hot")
def _statement_count(surql: str) -> int:
    """Number of ``;``-separated statements in *surql*, trailing ``;`` ignored.

    Deliberately naive. A ``;`` inside a string literal inflates the count,
    which makes ``_replay_is_safe`` refuse a replay it could technically have
    allowed — the safe direction. A parser that got this "right" would be
    trading a real double-apply risk for a retry that was never guaranteed.
    """
    return len([s for s in surql.rstrip().rstrip(";").split(";") if s.strip()])


@observe(tier="hot")
def _replay_is_safe(surql: str, results: object, err_index: int, n_lets: int) -> bool:
    """True when re-POSTing the request after this error cannot double-apply anything.

    Two conditions, and nothing else — an unexpected response shape must fall
    through to the raise rather than to a replay:

    1. SOME entry names an error SurrealDB itself labels retryable (a
       transaction-write conflict) — see ``_any_entry_is_retryable`` for why the
       scan is over every entry and not just the first. A parse error or a
       constraint violation never becomes true by being asked again.
    2. Nothing with a persistent effect can already have committed:
         * an explicit ``BEGIN … COMMIT`` statement rolled back whole — safe; or
         * the failing entry is the FIRST non-``LET`` statement
           (``err_index == n_lets``) AND ``surql`` holds only that one
           statement, so nothing with a persistent effect ran before it and
           nothing follows it that a replay could re-apply.
       A conflict at a LATER index means an earlier statement's own transaction
       already committed, and replaying would apply it twice — refuse.

    The single-statement clause closes a hole the ``err_index == n_lets`` test
    leaves open on its own. ``/sql`` keeps executing after a statement fails, so
    in a NON-atomic ``A; B`` body where A conflicts, B still runs and commits —
    yet the failure sits at ``err_index == n_lets`` and would read as safe. A
    replay then applies B twice. No production caller is exposed today (all 17
    multi-statement ``_q`` sites wrap in ``BEGIN``, audited 2026-08-27), but the
    rule failed OPEN rather than closed, and the obvious next multi-statement
    caller would have inherited a silent double-apply. The test-suite wipe
    (``DELETE memory; UPSERT counter:memory SET val = (val ?? 0) + 1``) is
    exactly that shape — it would have double-incremented the id counter.

    The BEGIN test reads ``surql`` — the caller's statement — and NOT the wire
    body, which is what makes it reachable at all: every production
    ``BEGIN … COMMIT`` write binds params, so the body it sends starts with
    ``LET $…``. Testing the body would have made this branch dead exactly where
    multi-statement atomicity is the point (``wiki.py``'s page+version writes,
    ``queue.py``'s file-hash writes).

    NOTE on the counter hazard: ``UPSERT counter:memory SET val = (val ?? 0) + 1``
    is a single non-``LET`` statement and is therefore replayable under this
    rule. That is correct ONLY because a conflicted transaction rolls back —
    which is what OCC guarantees and what the "can be retried" wording asserts.
    Were a conflict ever reported for a PARTIALLY applied statement, this rule
    would double-increment the id counter.
    """
    if not _any_entry_is_retryable(results):
        return False
    if surql.lstrip().upper().startswith("BEGIN"):
        return True
    if _statement_count(surql) > 1:
        return False
    return err_index == n_lets


@observe(tier="stage")
def post_sql_with_conflict_retry(http: object, body: str, surql: str, n_lets: int) -> object:
    """POST *body* to ``/sql``, replaying a retryable conflict when it is safe.

    *body* is what goes on the wire (``LET`` bindings + *surql*); *surql* is the
    caller's own statement, which is what the BEGIN test in ``_replay_is_safe``
    must read. ``n_lets`` is the number of prepended ``LET`` statements — the
    index of the first statement with a persistent effect. Returns the LAST
    result entry's ``result`` (LET entries precede it), or raises
    ``RuntimeError`` with the same message shape the non-retrying transport used.
    """
    encoded = body.encode()
    for attempt in range(ATTEMPTS):
        resp = http.post("/sql", content=encoded, headers={"Content-Type": "text/plain"})  # type: ignore[attr-defined]
        resp.raise_for_status()
        results = resp.json()
        err_index, err_entry = _first_err_entry(results)
        if err_entry is None:
            return results[-1].get("result") if results else None
        if attempt < ATTEMPTS - 1 and _replay_is_safe(surql, results, err_index, n_lets):
            _log.debug(
                "Retryable SurrealDB write conflict (attempt %d/%d), replaying",
                attempt + 1,
                ATTEMPTS,
            )
            time.sleep(BACKOFF_S * (attempt + 1) * (1.0 + random.random()))
            continue
        raise RuntimeError(
            f"SurrealDB error: {err_entry.get('detail') or err_entry.get('result') or err_entry}"
        )
    raise RuntimeError("SurrealDB error: write-conflict retry loop exhausted")  # unreachable
