"""A refusal is not a crash — the shared marker for DELIBERATE rejections.

WHY THIS EXISTS
---------------
The ``/admin`` route caught ``KeyError`` and nothing else, so every backend op
that refused BY DESIGN was rendered by FastAPI as a bare HTTP 500 with a
traceback, and ``_forward_admin``'s ``raise_for_status()`` flattened that into an
untyped ``httpx.HTTPStatusError``. Two independent gates were riding that same
seam — the restore-verification gate (ledger task 80) and Car J's wiki
mutability lock (ledger task 294) — which is why the fix is a shared base rather
than two patched call sites.

The cost was not cosmetic. ``core/backup/quiesce.py``'s
``if verification.get("status") != "ok"`` could never run, because the forward
raised before the body was read: the ONE place that inspected the tri-state
report was dead code. And to every automated caller a correct refusal and a
genuine backend fault were byte-identical, so a working gate paged as a bug.

OPT-IN IS PER TYPE, NEVER PER MODULE
------------------------------------
An exception is a refusal only when its class says so. That is deliberate and it
cuts both ways:

* ``ProjectRegistryUnavailableError`` lives beside two refusal types in
  ``storage/sql/errors.py`` and is explicitly NOT one — its own docstring
  argues that collapsing "cannot check" into "checked and rejected" is the
  defect. It stays a 500.
* Builtin ``PermissionError`` is not a refusal either. A filesystem EACCES is a
  fault; only ``WikiImmutableError`` — which subclasses BOTH ``PermissionError``
  and this base — is the intentional outcome. Blanket-classifying the builtin
  would re-file a real fault as a deliberate decision, which is this same defect
  pointed the other way.

Any op whose exceptions are not opted in behaves exactly as before.

THE TRI-STATE LIVES IN THE PAYLOAD, NOT IN THE STATUS CODE
----------------------------------------------------------
``refusal_report()`` is spliced into the envelope at TOP LEVEL, the same way the
success path splices it, so ``status`` / ``checks`` / ``violations`` /
``unavailable`` read identically whether the restore passed or was refused.
``unavailable`` in particular stays a refusal: ``restore_sql``'s "FAIL CLOSED"
rule is that a verification which could not run proves nothing about the
artifact, so the gate refuses on it. Splitting it out as a 500 would contradict
ADR-0195/0196.
"""

from __future__ import annotations

from yadgar._shared.observability.observe import observe

__all__ = ["REFUSAL_STATUS", "AdminRefusal", "parse_refusal", "refusal_envelope"]

# 409 Conflict: the request was well-formed and understood, and the current
# state of the system is what refuses it. Deliberately in the 4xx band — the
# whole point is that it is NOT a 5xx.
REFUSAL_STATUS = 409


class AdminRefusal(Exception):
    """Marker base: this exception is a decision, not a failure.

    Subclasses declare a machine-readable ``reason`` and MAY override
    ``refusal_report`` to carry structured evidence. Mixed in alongside the
    concrete builtin a call site already expects (``RuntimeError``,
    ``PermissionError``) so existing ``except`` clauses and ``pytest.raises``
    assertions keep working unchanged.
    """

    #: Machine-readable rejection code. Never collapsed across distinct causes —
    #: an operator (and an automated caller) must be able to tell them apart.
    reason = "refused"

    def refusal_report(self) -> dict:
        """Structured evidence spliced into the envelope's top level."""
        return {}


@observe(tier="hot", span=False)
def refusal_envelope(exc: AdminRefusal, op: str) -> dict:
    """Render *exc* as the wire envelope the ``/admin`` refusal path returns.

    The report is spliced FIRST so the envelope's own keys cannot be shadowed by
    an op's report, and so the tri-state keys sit exactly where the 200 path puts
    them.

    ``ok: False`` is this codebase's existing success discriminator and is the
    guardrail for a caller that inspects neither ``refused`` nor ``reason``: a
    refusal can never read as a success.
    """
    return {
        **exc.refusal_report(),
        "ok": False,
        "refused": True,
        "op": op,
        "reason": exc.reason,
        "error": str(exc),
    }


@observe(tier="hot", span=False)
def parse_refusal(status_code: int, body: object) -> dict | None:
    """Return the refusal envelope a backend response carries, else ``None``.

    The READ half of ``refusal_envelope``, kept in the same module so the wire
    contract has one owner rather than a writer and a hopeful reader.

    Detection keys on the ``refused`` marker, NOT on the status code alone: a
    ``409`` minted by anything else (a future conflict route, a proxy) must keep
    raising, because a caller that mistook it for a refusal would read an
    envelope that has no ``reason`` and no report.

    FastAPI wraps ``HTTPException(detail=...)`` as ``{"detail": {...}}``, so the
    envelope is nested one level deeper than the success path's ``result``.
    """
    if status_code != REFUSAL_STATUS or not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict) and detail.get("refused") is True:
        return detail
    return None
