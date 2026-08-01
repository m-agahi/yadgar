"""An ordered systemd unit-file model (task:0110 Stage A, ADR-0190).

The convergence car makes one Python renderer the single source of truth for the
nine units ``scripts/install/*.in`` currently render with ``sed``. Two of those
nine cannot be expressed by a dict-keyed model, which is why directives here are
an **ordered list of pairs** rather than a mapping:

* ``yadgar.target.in`` writes ``Wants=`` on TWO separate lines (``:3`` and
  ``:19``) and systemd UNIONS repeated directives. A dict silently keeps one —
  and the one it would drop (``:19``) is the sole activation mechanism for the
  vacuum timer, the nightly timer and the vacuum-trigger path. The unit still
  renders, still passes every "contains ``Wants=``" assertion, and background
  maintenance never starts.
* ``yadgar-vacuum-trigger.service.in:14-15`` carries TWO ``ExecStart=`` lines,
  legal only under ``Type=oneshot``, and their ORDER is load-bearing (the
  trigger file is removed BEFORE the vacuum starts, so a failing vacuum cannot
  pin the ``.path`` unit active and stop it firing again).

Comments are part of the model, not decoration: the parity harness diffs the
rendered text against committed fixtures of the ``sed`` render, and those
fixtures carry every template comment. A renderer that dropped them would show
~60 unexplained diff lines per unit.

Rendering rule: sections are separated by exactly one blank line, and the file
ends with a newline — the shape ``sed`` produces from the templates today.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yadgar._shared.observability.observe import observe

__all__ = [
    "Blank",
    "Comment",
    "Directive",
    "Entry",
    "Section",
    "UnitFile",
    "comments",
    "render_unit",
]


@dataclass(frozen=True)
class Directive:
    """One ``Key=Value`` line. Duplicates of *key* within a section are legal."""

    key: str
    value: str

    def line(self) -> str:
        return f"{self.key}={self.value}"


@dataclass(frozen=True)
class Comment:
    """One ``# text`` line. Empty *text* renders as a bare ``#``."""

    text: str = ""

    def line(self) -> str:
        return f"# {self.text}" if self.text else "#"


@dataclass(frozen=True)
class Blank:
    """One empty line inside a section (the templates use these before trailers)."""

    def line(self) -> str:
        return ""


Entry = Directive | Comment | Blank


@dataclass(frozen=True)
class Section:
    """``[Name]`` plus its ordered entries. ``[Install]`` is simply omitted."""

    name: str
    entries: tuple[Entry, ...] = ()

    def text(self) -> str:
        body = "".join(f"{e.line()}\n" for e in self.entries)
        return f"[{self.name}]\n{body}"

    def values(self, key: str) -> list[str]:
        """Every value for *key*, in order — the duplicate-preserving accessor."""
        return [e.value for e in self.entries if isinstance(e, Directive) and e.key == key]


@dataclass(frozen=True)
class UnitFile:
    """A named unit file: ``yadgar.target``, ``yadgar-vacuum.timer``, …"""

    name: str
    sections: tuple[Section, ...] = field(default=())

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)


def comments(lines: tuple[str, ...], /, **fmt: object) -> tuple[Comment, ...]:
    """Turn a block of comment bodies into ``Comment`` entries, ``str.format``-ing each.

    Comment blocks are held as module-level tuples so the unit builders stay
    inside the function-LOC cap; several of them interpolate the runtime binary
    or the state dir, exactly as the ``sed`` render does.
    """
    return tuple(Comment(line.format(**fmt) if fmt else line) for line in lines)


@observe(tier="hot")
def render_unit(unit: UnitFile) -> str:
    """Render *unit* to unit-file text: sections separated by one blank line."""
    return "\n".join(s.text() for s in unit.sections)
