"""``project`` registry error classes — STDLIB ONLY, importable without engine #2.

C6 of the 0047 spine train. Three failures the registry can produce, kept as
three distinct classes because the call sites act on them differently:

  ``UnknownProjectError``
      The project_id is not in the registry. The write is REJECTED. This is
      the FAIL-LOUD half of ADR-0202/ADR-0223 — a free-string ``project=`` is
      how a phantom namespace gets minted, and the registry is the only thing
      between a typo and a silently-created project.

  ``DuplicateProjectError``
      The seed tried to insert a key that already exists. Also an error, and
      deliberately so: ``INSERT OR IGNORE`` was rejected in ADR-0202's
      consequences because it converts every typo into a new project.

  ``ProjectRegistryUnavailableError``
      The registry could not be CONSULTED at all (engine #2 absent). Distinct
      from ``UnknownProjectError`` on purpose — collapsing the two would let
      "cannot check" read as "checked and rejected" at the call site, and the
      correct response differs (fix the deployment vs fix the project_id).

WHY THIS MODULE EXISTS AT ALL
-----------------------------
The classes are raised by ``mariadb.py`` (which reaches sqlalchemy) and
caught/re-exported by ``yadgar/backend/admin_exec/project_registry.py``, whose
own docstring promises it stays importable on hosts that never install the
``sql`` extra. If the guard imported them from ``mariadb`` directly, that
promise would depend on ``mariadb``'s module-level imports staying stdlib
forever — a property no test in an extra-carrying venv can observe breaking.
A separate stdlib-only module makes the guarantee structural, and
``test_errors_module_is_stdlib_only`` asserts it at the source level.

NOTHING may be imported here beyond ``__future__``.
"""

from __future__ import annotations

from yadgar._shared.refusal import AdminRefusal


class UnknownProjectError(AdminRefusal, RuntimeError):
    """The given project_id is not present in the ``project`` registry.

    Carries the offending ``project_id`` verbatim so the structured-error
    path can surface it in the response payload, and so the caller logs the
    typo at the right call site rather than chasing a foreign-key error
    later. Subclasses ``AdminRefusal`` so the ``/admin`` route renders the
    rejection as a structured 409 with ``reason="unknown_project"`` instead
    of a generic 500 (task #346). ``RuntimeError`` is kept as a base so
    existing ``except RuntimeError`` callers continue to match.
    """

    reason = "unknown_project"

    def __init__(self, project_id: str) -> None:
        super().__init__(f"unknown project_id: {project_id!r}")
        self.project_id = project_id


class DuplicateProjectError(AdminRefusal, RuntimeError):
    """A ``project`` row with this key already exists.

    Raised by the registry writer instead of swallowing the collision. The
    seed inserts many rows in one pass, so the key is carried on the
    exception — "a duplicate" without saying which one is not actionable.
    Subclasses ``AdminRefusal`` so the ``/admin`` route renders the
    rejection as a structured 409 with ``reason="duplicate_project"``
    instead of a bare ``{"ok": False, ...}`` (task #346). ``RuntimeError``
    is kept as a base so existing ``except RuntimeError`` callers continue
    to match.
    """

    reason = "duplicate_project"

    def __init__(self, project_id: str) -> None:
        super().__init__(f"project already registered: {project_id!r}")
        self.project_id = project_id


class ProjectRegistryUnavailableError(RuntimeError):
    """Engine #2 is absent, so the registry check could not run.

    Raised rather than returned. The pre-C6 guard returned silently here,
    which made it a no-op even once wired: every write would pass the
    "check" on a deployment where the registry does not exist, and the
    first symptom would be a phantom namespace nobody could trace back.
    """

    def __init__(self, project_id: str) -> None:
        super().__init__(
            f"project registry unavailable (engine #2 not composed); "
            f"cannot verify project_id: {project_id!r}"
        )
        self.project_id = project_id


__all__ = [
    "DuplicateProjectError",
    "ProjectRegistryUnavailableError",
    "UnknownProjectError",
]
