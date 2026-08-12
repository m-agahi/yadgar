"""``describe_dbapi_error`` — make an engine-#2 failure name itself.

THE FAILURE THIS EXISTS BECAUSE OF. A migration that could not run logged

    {"level":"ERROR","event":"engine #2 migration FAILED — the relational
     schema is not at head","error":"OperationalError","traceback":"… [truncated]"}

and that was the whole record. ``OperationalError`` is the same class for
"access denied", "server has gone away" and "unknown database"; the string
that says which — ``(1142, "CREATE command denied to user
'yadgar_app'@'localhost' for table `yadgar`.`task`")`` — sits on
``exc.orig.args`` and was never written down. The traceback would have carried
it, except the JSON formatter truncated from the FRONT and a DBAPI message is
the LAST line. Diagnosing one instance cost a full cycle.

NO ``sql`` EXTRA HERE, deliberately. ``sql/migrate.py`` keeps every
alembic/sqlalchemy import function-local, so this helper is importable on the
yadgar-ci image, which bakes no sqlalchemy — and these tests run there. The
exception shapes are reproduced with stand-ins rather than imported: what is
under test is the EXTRACTION, and a stand-in with ``.orig.args`` is the same
shape SQLAlchemy presents.

(It sits in ``migrate.py`` rather than the more obvious ``errors.py`` because
that module is stdlib-only by contract and I33 wants an ``@observe``
decorator on it — the decorator's import would break the stdlib guarantee.)
"""

from __future__ import annotations

from yadgar._shared.storage.sql.migrate import describe_dbapi_error


class _DriverError(Exception):
    """Stand-in for ``asyncmy.errors.OperationalError`` — ``args`` is (errno, msg)."""


class _WrapperError(Exception):
    """Stand-in for ``sqlalchemy.exc.DBAPIError`` — carries ``.orig``."""

    def __init__(self, message: str, orig: BaseException) -> None:
        super().__init__(message)
        self.orig = orig


_DENIED = _DriverError(
    1142,
    "CREATE command denied to user 'yadgar_app'@'localhost' for table `yadgar`.`task`",
)


def test_the_driver_errno_and_message_are_extracted():
    described = describe_dbapi_error(_WrapperError("(asyncmy…) (1142, …)", _DENIED))
    assert described["error_code"] == 1142
    assert described["error_message"] == (
        "CREATE command denied to user 'yadgar_app'@'localhost' for table `yadgar`.`task`"
    )


def test_the_wrapper_and_the_driver_class_are_both_named():
    """Both halves matter: the wrapper is what the caller caught, the driver
    class is what actually raised."""
    described = describe_dbapi_error(_WrapperError("boom", _DENIED))
    assert described["error"] == "_WrapperError"
    assert described["error_class"] == "_DriverError"


def test_a_bare_driver_error_needs_no_wrapper():
    described = describe_dbapi_error(_DENIED)
    assert described["error_code"] == 1142
    assert described["error"] == "_DriverError"


def test_the_access_denied_variant_is_distinguishable():
    """1044 and 1142 are both ``OperationalError`` and mean different things.

    1044 is the fresh-install shape (the grant heredoc aborted, so the account
    holds USAGE only); 1142 is the long-lived-host shape (the account can open
    the database but cannot create a table). Same class, opposite repair.
    """
    denied_db = _WrapperError(
        "x",
        _DriverError(1044, "Access denied for user 'yadgar_app'@'localhost' to database 'yadgar'"),
    )
    assert describe_dbapi_error(denied_db)["error_code"] == 1044
    assert describe_dbapi_error(_WrapperError("x", _DENIED))["error_code"] == 1142


def test_a_plain_exception_still_produces_a_record():
    """A logging helper that raises inside ``except`` replaces the failure it
    was called to describe. Anything at all must come back."""
    described = describe_dbapi_error(ValueError("no errno here"))
    assert described["error"] == "ValueError"
    assert described["error_message"] == "no errno here"
    assert "error_code" not in described


def test_an_argless_exception_is_survivable():
    described = describe_dbapi_error(RuntimeError())
    assert described["error"] == "RuntimeError"
    assert "error_code" not in described


def test_a_non_integer_first_arg_is_not_mistaken_for_an_errno():
    described = describe_dbapi_error(_WrapperError("x", _DriverError("not-an-errno", "detail")))
    assert "error_code" not in described
    assert described["error_message"] == "not-an-errno"


def test_the_failing_statement_is_not_included():
    """``str(exc)`` on a SQLAlchemy error appends the statement AND, on a DML
    path, its bound parameters. The DBAPI args carry neither, which is why the
    extraction reads them rather than the wrapper's message."""
    wrapper = _WrapperError(
        "(asyncmy.errors.OperationalError) (1142, 'denied')\n"
        "[SQL: INSERT INTO config (key, value) VALUES (%s, %s)]\n"
        "[parameters: ('some-knob', 'some-value')]",
        _DENIED,
    )
    rendered = " ".join(str(v) for v in describe_dbapi_error(wrapper).values())
    assert "parameters" not in rendered
    assert "INSERT INTO" not in rendered
