"""Compact, operator-facing reasons for DB-driver failures.

Ledger tasks #241 / #381. Lives in its own module rather than inside
``ledger.py`` for two reasons: ``ledger.py`` is at its I13 HARD file cap, and
every admin op that reports a driver failure wants the same extraction — the
seed path is simply the first caller to have needed it.
"""

from __future__ import annotations

from typing import Any

from yadgar._shared.observability.observe import observe

__all__ = ["driver_error_detail"]


@observe(tier="stage", metric="backend.admin.driver_error_detail")
def driver_error_detail(exc: BaseException) -> tuple[str, int | None]:
    """Return ``(reason, db_errno)`` for a DB-driver failure.

    ``str(exc)`` on a SQLAlchemy ``DBAPIError`` is the wrong thing to put in
    an operator-facing envelope: it appends ``[SQL: ...]`` and
    ``[parameters: ...]``, so the value that was too long for the column is
    echoed back in full — a 200-char ``display_name`` note lands verbatim —
    and it buries the one actionable line (the server's own ``1406: Data too
    long for column 'display_name'``) behind that noise and a "Background on
    this error at" URL.

    SQLAlchemy keeps the driver's exception on ``exc.orig``, whose ``args``
    are ``(errno, message)`` for every MySQL/MariaDB DBAPI. An exception with
    no ``orig`` is not a wrapped driver error and degrades to
    ``(str(exc), None)`` — the pre-existing behaviour.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return str(exc), None
    args: tuple[Any, ...] = tuple(getattr(orig, "args", ()) or ())
    errno = args[0] if args and isinstance(args[0], int) else None
    message = str(args[1]) if len(args) > 1 else str(orig)
    driver = type(orig).__name__
    if errno is None:
        return f"{driver}: {message}", None
    return f"{driver} {errno}: {message}", errno
