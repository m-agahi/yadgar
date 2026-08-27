"""Invoking a write-path guard from a PREVIEW, without its side effects.

``adr_seed`` and ``identity_stamp`` both carry a ``_preflight_write_guards``
that runs the write path's registry guard on the dry-run path, so a preview
reaches the same verdict the apply would (Car 19 / ledger task 176). Both
iterate a ``_WRITE_PATH_GUARDS`` tuple of METHOD NAMES and reach the method
through ``getattr``, which is what makes the AST parity tests possible — and
also what makes calling the guard with a keyword awkward enough to be worth
stating once rather than twice.

WHY A SEPARATE MODULE
---------------------
The two preflights are deliberately written to "mirror each other exactly",
so the obvious home was a copy in each. ``adr_seed.py`` is at I13's HARD
1000-LOC file cap and the second copy pushed it over — the same forcing
function that split ``sql/registry.py`` out of ``mariadb.py``. One shared
helper is better than two copies anyway: a probe that drifted between the two
would let one preview stamp and the other not, which is the harder bug to see.
"""

from __future__ import annotations

import inspect
from typing import Any

from yadgar._shared.observability.observe import observe

#: Keyword the write-path guards accept to mean "check, but do not stamp".
#: ``MariaStorageEngine.assert_project_registered`` bumps
#: ``project.last_validated_at`` on a present row (ledger task 384), so a dry
#: run that called it unqualified would MUTATE the registry — the same defect
#: ledger task 385 fixed in ``verify-hooks``, which advertised "read-only" and
#: POSTed an ``action_log`` row on every invocation.
GUARD_NO_WRITE_KW = "refresh"


@observe(tier="stage")
def preview_guard_kwargs(guard: Any) -> dict[str, Any]:
    """``{"refresh": False}`` when *guard* accepts it, else ``{}``.

    Signature-probed rather than passed blind. Both preflights report EVERY
    exception from a guard as "the guard rejected project_id", so handing the
    keyword to a future guard that does not take it would surface a
    ``TypeError`` as a FALSE rejection of a perfectly good project — "could not
    call" read as "checked and refused", the exact confusion the task-168
    structural-fault branch exists to prevent.

    Parity is unaffected by the suppression: the guard still RUNS on the
    preview and still refuses identically. Only the side effect is withheld,
    and a side effect is precisely what a preview must not have.
    """
    try:
        params = inspect.signature(guard).parameters
    # ``# fmt: skip`` is load-bearing: ruff-format 0.16.x rewrites
    # ``except (A, B):`` to PEP 758's unparenthesized form, which is a
    # SyntaxError on the 3.13 interpreter pre-commit's system hooks run.
    except (TypeError, ValueError):  # fmt: skip
        return {}
    return {GUARD_NO_WRITE_KW: False} if GUARD_NO_WRITE_KW in params else {}
