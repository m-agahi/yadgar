"""Structured, fail-loud errors shared by core and backend — C5 of the 0047 train.

ADR-0227:

> A missing or unresolved ``project_id`` FAILS LOUD with a structured error at
> the boundary — it is never defaulted, never inferred, never silently
> substituted.

**Why this module lives in ``_shared``.** Both halves of the system must raise
the same type: ``core/server/tools/_project_param.py`` raises it when the
resolver chain ends without a value, and
``_shared/storage/_project_id_writer.py`` raises it at the storage chokepoint.
The storage module cannot import ``yadgar.core`` (import-linter contract 1), so
a core-side home for the class would force the storage side onto a different
type — and two error types for one failure is how a boundary stops being one.

**Why the payload is an attribute, not just a message.** The reader of this
error is an agent, not a human: a bare "unresolved project" tells it that
something failed, not what to change. Every raise therefore names the TOOL that
could not resolve and the FIX that would make the same call succeed. Tool
boundaries that already return an error envelope surface ``exc.payload``
verbatim; the rest let it propagate, where ``str(exc)`` carries the same two
facts inline.

**Relationship to ``core.hooks._identity_mint.UnresolvableProjectError``.**
Deliberately two classes, not one. That one is the HOST-side mint failing to
derive an identity from a working tree (a git/config problem, fixed by adding a
remote or writing ``.yadgar/project-id``). ``UnresolvedProjectError`` here is a
CALL that arrived without one (an API problem, fixed by passing ``project=``).
Merging them would produce an error whose remedy text is right half the time.
"""

from __future__ import annotations

from typing import Any

#: The one remedy sentence. Kept as a module constant so the payload, the
#: message and the tests cannot drift into three different phrasings.
PROJECT_FIX_HINT = 'pass project="owner/repo"'


class YadgarError(Exception):
    """Base for yadgar's structured, agent-readable errors.

    Carries a ``payload`` dict so a tool boundary can return the structured
    form directly instead of re-deriving it from the message text.
    """

    #: Machine-readable discriminator; subclasses override.
    error_code: str = "yadgar_error"

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload: dict[str, Any] = (
            payload if payload is not None else {"error": self.error_code}
        )


class UnresolvedProjectError(YadgarError):
    """Raised when a scoped operation has no project_id and none can be given.

    ADR-0227 deleted every fallback that used to answer this case: no
    ``local/<basename>``, no ``"global"`` tier, no ``"unresolved"`` catch. The
    identity is minted host-side at SessionStart and travels as an explicit
    caller parameter; a call that arrives without one is a caller defect, and
    surfacing it is the entire point of the flip.
    """

    error_code = "unresolved_project"

    def __init__(self, tool: str, detail: str = "") -> None:
        payload: dict[str, Any] = {
            "error": self.error_code,
            "tool": tool,
            "fix": PROJECT_FIX_HINT,
        }
        if detail:
            payload["detail"] = detail
        message = (
            f"{tool}: no project_id was supplied and none can be derived "
            f"(ADR-0227: yadgar never guesses an identity). Fix: {PROJECT_FIX_HINT}."
        )
        if detail:
            message = f"{message} {detail}"
        super().__init__(message, payload)
        self.tool = tool


class UnresolvedPatternError(YadgarError):
    """Raised when an agent-prompt slug named by the caller cannot be read.

    Same defect class as the deleted project fallbacks, in a different
    subsystem: ``agent_dispatch_prelude`` used to answer an unknown ``pattern``
    with a prelude containing the contract and NO prompt, which the caller reads
    as "no pattern exists for this task-shape" and which therefore licenses a
    bespoke dispatch. The TOC carries exact slugs and the agent reads BY slug —
    an unavailable slug must fail loud rather than let the agent invent one.
    """

    error_code = "unresolved_pattern"

    def __init__(self, slug: str, detail: str = "") -> None:
        fix = (
            'wiki_read("agent-prompt-toc") for the exact slugs, or pass '
            'pattern="" to skip the prompt lookup deliberately'
        )
        payload: dict[str, Any] = {
            "error": self.error_code,
            "tool": "agent_dispatch_prelude",
            "slug": slug,
            "fix": fix,
        }
        if detail:
            payload["detail"] = detail
        message = f"agent_dispatch_prelude: cannot read agent-prompt slug {slug!r}. Fix: {fix}."
        if detail:
            message = f"{message} {detail}"
        super().__init__(message, payload)
        self.slug = slug


__all__ = [
    "PROJECT_FIX_HINT",
    "UnresolvedPatternError",
    "UnresolvedProjectError",
    "YadgarError",
]
