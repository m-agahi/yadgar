"""Rules-file generator — Car 2.

Renders the canonical yadgar rules body (``AGENTS.md.template`` + per-client
addenda) and writes it idempotently into each client's rules file.  Generalises
the find/replace-section mechanic from ``sync_instructions`` (``misc.py:459``)
so the same safety property — never clobbers surrounding user content — works
for every client, not just Claude Code.

Three bridge strategies handle non-AGENTS.md-native clients (design §3.3):

  ``IMPORT``          Claude Code — add ``@AGENTS.md`` import inside CLAUDE.md
                      (D4; idempotent).
  ``SETTINGS_ALIAS``  Gemini — set ``context.fileName:"AGENTS.md"`` in
                      ``settings.json`` via ``merge_json`` (D3).
  ``SYMLINK``         not yet emitted by any registry entry; reserved for future
                      use; ``write_rules`` raises ``NotImplementedError`` when
                      called with ``SYMLINK`` so any future descriptor that
                      forgets to implement the bridge fails fast.

Addenda (design §3.1):  per-client addendum keys in
``descriptor.rules_addendum`` map to files under
``install_assets/rules/addenda/<key>.md``.  The CC descriptor carries
``["compaction_shield", "auto_capture"]``; hook-less clients carry ``[]``.

Generalization of ``sync_instructions``:  the public ``section_replace`` helper
is the load-bearing find/replace-section primitive.  ``misc.py:sync_instructions``
delegates here so setup-time (``yadgar-setup.sh`` step 9 → Car 3 reroute, noted
below) and session-time agree on the same body.

NOTE — fragment + shell reroute deferred to Car 3:
The ``install_assets/CLAUDE.md.fragment`` and ``yadgar-setup.sh`` step 9's
``append_claude_rules.sh`` consumer are NOT retired here.  Rerouting them
through the generator cleanly requires the unified ``yadgar install --rules``
CLI surface (Car 3).  The fragment stays in place; this module is the new
canonical source and Car 3 will make step 9 call ``write_rules`` instead.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe

if TYPE_CHECKING:
    from yadgar.core.install.clients.descriptor import ClientDescriptor


# ── Template + addenda loading ────────────────────────────────────────────────

_RULES_DIR = Path(__file__).parents[2] / "install_assets" / "rules"
_ADDENDA_DIR = _RULES_DIR / "addenda"
_TEMPLATE_PATH = _RULES_DIR / "AGENTS.md.template"


@observe(tier="stage")
def load_template() -> str:
    """Return the raw content of ``AGENTS.md.template`` (no interpolation)."""
    return _TEMPLATE_PATH.read_text()


@observe(tier="stage")
def load_addendum(key: str) -> str:
    """Return content for the named addendum key, or an empty string when missing."""
    path = _ADDENDA_DIR / f"{key}.md"
    if not path.exists():
        return ""
    return path.read_text()


@observe(tier="stage")
def render_body(descriptor: ClientDescriptor, version: str) -> str:
    """Render the canonical rules body for *descriptor*.

    Composes the agnostic core template with per-client addenda, then
    substitutes the ``{__version__}`` placeholder using ``str.replace`` (not
    ``.format()`` — the body may contain ``${...}`` env-ref literals that
    would crash ``.format()``).

    Args:
        descriptor: drives the header + addenda list.
        version:    ``yadgar.__version__`` string.

    Returns:
        Fully-rendered body string, NOT yet including the section header
        (the header comes from ``section_replace`` / the caller).
    """
    core = load_template()
    # Addenda are appended in declaration order (deterministic).
    addenda_parts = [load_addendum(k) for k in descriptor.rules_addendum if k]
    parts = [core] + [a for a in addenda_parts if a]
    body = "\n\n".join(p.rstrip("\n") for p in parts)
    # Safe substitution: only the literal placeholder, never .format()
    body = body.replace("{__version__}", version)
    return body


# ── Find/replace-section (generalised sync_instructions mechanic) ─────────────


@observe(tier="stage")
def section_replace(
    content: str,
    section_header: str,
    new_body: str,
) -> str:
    """Replace (or append) the *section_header* block inside *content*.

    The section extends from *section_header* to the next ``## `` h2 header
    or end-of-string — identical to the ``sync_instructions`` pattern.
    Idempotent: calling twice with the same *new_body* produces byte-identical
    output.

    Args:
        content:        existing file text.
        section_header: the exact ``## …`` delimiter (e.g. ``"## Yadgar"``).
                        The header occupies its own line; the rest of the
                        section follows.
        new_body:       the rendered section body (without the header line
                        itself — this function prepends the header + newline).

    Returns:
        Updated file text with the section inserted/replaced.

    Note:
        *new_body* MUST NOT contain a line starting with ``## `` (h2) — that
        would confuse the stop-pattern.  The canonical template uses ``###``+
        subheaders only, which is enforced by design.
    """
    # Full replacement block = header + body + trailing blank line
    escaped = re.escape(section_header)
    replacement = f"{section_header}\n{new_body}\n\n"

    # Pattern: section_header line + all non-h2 lines until the next h2 or EOF
    # re.MULTILINE so ^ / $ match per line; DOTALL not needed.
    pattern = rf"^{escaped}[^\n]*\n(?:(?!^## )[^\n]*\n)*"
    match = re.search(pattern, content, re.MULTILINE)
    if match:
        # Splice literally — NEVER re.sub with `replacement` as the template
        # arg: backslash sequences in *new_body* (e.g. "\1", "\A", "\0") would
        # be interpreted as regex replacement escapes and crash or corrupt.
        return content[: match.start()] + replacement + content[match.end() :]

    # Section absent — append after "## Global Rules" if present, else at end.
    if "## Global Rules" in content:
        return content.replace(
            "## Global Rules\n",
            "## Global Rules\n\n" + replacement,
            1,
        )
    return content.rstrip("\n") + "\n\n" + replacement


@observe(tier="stage")
def _atomic_write_text(target: Path, text: str) -> None:
    """Write *text* to *target* atomically via a temp file in the same dir."""
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=directory, prefix=".yadgar_rules_tmp_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(text)
        os.replace(tmp_path_str, target)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


# ── Bridge helpers ────────────────────────────────────────────────────────────


@observe(tier="stage")
def _ensure_import_line(claude_md_path: Path, import_target: str = "AGENTS.md") -> None:
    """Idempotently add ``@<import_target>`` import line to *claude_md_path* (D4).

    The import line is prepended to the file (before any existing content) so
    Claude Code sees AGENTS.md content at the top.  If the line is already
    present, no change is made.
    """
    import_line = f"@{import_target}"
    if claude_md_path.exists():
        text = claude_md_path.read_text()
        if import_line in text:
            return
        new_text = import_line + "\n" + text
    else:
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        new_text = import_line + "\n"
    _atomic_write_text(claude_md_path, new_text)


@observe(tier="stage")
def _ensure_settings_alias(settings_path: Path) -> None:
    """Set ``context.fileName:"AGENTS.md"`` in Gemini settings.json (D3).

    Uses ``merge_json`` for format-preserving, atomic, idempotent merge so no
    other keys in the user's ``settings.json`` are touched.
    """
    # Lazy import to avoid circular dependency (merge.py is in the same package).
    from yadgar.core.install.clients.merge import merge_json  # noqa: PLC0415

    merge_json(
        settings_path,
        root_key=("context",),
        entry_key="fileName",
        value="AGENTS.md",
    )


# ── Public entry point ────────────────────────────────────────────────────────


@observe(tier="boundary")
def write_rules(
    descriptor: ClientDescriptor,
    version: str,
    scope: str = "global",
    project_dir: Path | None = None,
) -> dict:
    """Render and write the yadgar rules file for *descriptor*.

    Idempotent — re-running with the same inputs replaces only the Yadgar
    section; surrounding user-authored content is preserved.  Atomic write
    ensures a crash cannot truncate the target file.

    Bridge strategies (``descriptor.rules_bridge``):

    * ``IMPORT`` (Claude Code): write ``AGENTS.md`` with the canonical body,
      then add an ``@AGENTS.md`` line inside the client's ``rules_path`` target
      (typically ``CLAUDE.md``).
    * ``SETTINGS_ALIAS`` (Gemini): write ``AGENTS.md`` alongside the rules
      file, then merge ``context.fileName:"AGENTS.md"`` into ``settings.json``.
    * ``None`` (AGENTS.md-native clients): write ``rules_path`` directly —
      one file, no bridge.
    * ``SYMLINK``: reserved; raises ``NotImplementedError``.

    Args:
        descriptor:   the client descriptor.
        version:      ``yadgar.__version__`` string.
        scope:        ``"global"`` (default) or ``"project"``.
        project_dir:  required when *scope* is ``"project"``.

    Returns:
        ``{"written": str, "bridge": str | None, "section_length": int}``

    Raises:
        ValueError: if *scope* is ``"project"`` and *project_dir* is None,
            or if the resolved rules path is None.
        NotImplementedError: for ``SYMLINK`` bridge (not yet implemented).
    """
    from yadgar.core.install.clients.descriptor import RulesBridge  # noqa: PLC0415

    if scope == "project":
        if project_dir is None:
            raise ValueError("project_dir required when scope='project'")
        rules_path = descriptor.rules_path.resolve_project(project_dir)
    else:
        rules_path = descriptor.rules_path.resolve_global()

    if rules_path is None:
        raise ValueError(f"Client {descriptor.name!r} has no rules path for scope={scope!r}")

    body = render_body(descriptor, version)
    bridge_result: str | None = None

    if descriptor.rules_bridge is None:
        # AGENTS.md-native: write directly to rules_path (e.g. AGENTS.md).
        section_header = descriptor.rules_header
        existing = rules_path.read_text() if rules_path.exists() else ""
        new_content = section_replace(existing, section_header, body)
        _atomic_write_text(rules_path, new_content)

    elif descriptor.rules_bridge is RulesBridge.IMPORT:
        # D4 — Claude Code: write AGENTS.md sibling, then ensure @AGENTS.md import.
        agents_md = rules_path.parent / "AGENTS.md"
        section_header = "## Yadgar"
        existing = agents_md.read_text() if agents_md.exists() else ""
        new_content = section_replace(existing, section_header, body)
        _atomic_write_text(agents_md, new_content)
        # Ensure the import line in CLAUDE.md (idempotent).
        _ensure_import_line(rules_path, import_target="AGENTS.md")
        bridge_result = "import"

    elif descriptor.rules_bridge is RulesBridge.SETTINGS_ALIAS:
        # D3 — Gemini: write AGENTS.md alongside GEMINI.md; alias settings.
        agents_md = rules_path.parent / "AGENTS.md"
        section_header = "## Yadgar"
        existing = agents_md.read_text() if agents_md.exists() else ""
        new_content = section_replace(existing, section_header, body)
        _atomic_write_text(agents_md, new_content)
        # Alias context.fileName → AGENTS.md in settings.json.
        settings_path = rules_path.parent / "settings.json"
        _ensure_settings_alias(settings_path)
        bridge_result = "settings_alias"

    elif descriptor.rules_bridge is RulesBridge.SYMLINK:
        raise NotImplementedError(
            "SYMLINK bridge is reserved but not yet implemented; "
            "use IMPORT or SETTINGS_ALIAS instead."
        )

    else:
        raise ValueError(f"Unknown rules_bridge value: {descriptor.rules_bridge!r}")

    return {
        "written": str(rules_path),
        "bridge": bridge_result,
        "section_length": len(body),
    }
