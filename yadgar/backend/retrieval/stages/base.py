"""RetrievalStage abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod

from yadgar.backend.retrieval.state import RetrievalState


class RetrievalStage(ABC):
    """Abstract base for all retrieval pipeline stages.

    Each stage receives the current RetrievalState, applies its logic,
    and returns the (mutated) state. Stages must not cross async/sync
    boundaries (I5) and must not perform SurrealDB writes (YADGAR invariant).

    Subclasses override:
    - ``name``  — unique string identifier used in profiles and metrics.
    - ``apply`` — core stage logic.
    - ``is_enabled`` (optional) — opt-in/opt-out per profile.
    """

    #: Unique stage identifier; used as label in Prometheus metrics and profiles.
    name: str

    @abstractmethod
    def apply(self, state: RetrievalState) -> RetrievalState:
        """Apply this stage to the retrieval state.

        Mutate ``state`` in place (or replace specific fields) and return it.
        Returning the same object is preferred for clarity; stages must NOT
        stash state references across calls (not thread-safe).
        """

    def is_enabled(self, profile: str, config: dict) -> bool:
        """Return True if this stage should run for the given profile + config.

        Default: enabled for every profile.
        Stages that are opt-in (e.g. NLI, heavy ML) override this.
        Per-call ``stage_overrides`` in config may further disable a stage:
        ``config.get("stage_overrides", {}).get(self.name)`` — if explicitly
        False, the pipeline runner skips this stage regardless of this method.
        """
        return True
