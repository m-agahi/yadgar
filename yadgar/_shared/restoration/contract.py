"""Checkpoint contract — the dataclass shape shared across layers.

T2 Car C (layer-boundary train): extracted from the flat restoration.py so
contract-only consumers (e.g. yadgar.backend.write_exec.checkpoint_impl) can
import the payload shape without loading the CheckpointRestore impl.
"""

from dataclasses import dataclass, field


@dataclass
class CheckpointContext:
    """Optional context fields for create_checkpoint.

    Bundles the 7 optional checkpoint payload params so the method signature
    stays within the I13 PLR0913 cap (≤8 non-self args).

    resume_hint: if provided, stored verbatim; otherwise derived as
        restore(directory="<directory>").
    """

    current_task: str = ""
    files_being_edited: list[str] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    active_errors: list[str] = field(default_factory=list)
    custom_context: str = ""
    resume_hint: str = ""
