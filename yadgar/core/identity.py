"""Pure identity-parsing helpers — Car A0 of the 0047 spine train, gutted by C5.

**There is no ``derive_project_id`` here any more, and none may be added.**
C5 deleted it along with ``_local_fallback``, its no-remote arm. ADR-0227:

> ``derive_project_id`` is removed from every code path reachable by core or
> backend. […] ALL fallbacks are deleted: no ``_local_fallback``, no
> ``local/<basename>``, no ``GLOBAL_FALLBACK`` ``"global"`` tier, no directory
> tier in the resolver.

What survives is a set of pure readers with no identity POLICY in them: parse a
remote URL, apply an insteadOf table, read a ``.yadgar/project-id`` file, shell
out for ``remote.origin.url``. The policy — which sources count, in what order,
and what happens when none resolve — is the composition, and it lives in
the host-side mint under ``core/hooks/``, where only the hook entry points
can reach it. That module raises rather than guessing. (Named obliquely: C2's
boundary test text-scans for its module name outside the hook entry points.)

The ``owner/repo`` key those helpers build is intentionally a path (not just
``repo``): a ``group/sub/repo`` triple stays one opaque path (§16.9). Splitting
on the last ``/`` would have collapsed every nested namespace into a single
repo, so a Codeberg group with 30 subprojects would all collide.

Does NOT touch the DB (§15: core never imports ``yadgar._shared.storage``).
"""

from __future__ import annotations

import functools
import logging
import re
import subprocess
from pathlib import Path

from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)


# ── insteadOf resolver (§16.4) ──────────────────────────────────────────────
#
# ``git config --get-regexp '^url\\..*\\.insteadof$')`` produces lines like
#     url.<REWRITE>.insteadof <SOURCE>
# where the resolver should rewrite URLs that start with <SOURCE> into
# <REWRITE>. Multiple rules may chain (a rewrite may match another
# rule's source) — iterate until a fixed point.
#
# Pulled out of ``derive_project_id`` as a pure function so it is
# testable in isolation: a subprocess mock returning a hard-coded config
# table reproduces the chain without touching the real gitconfig.


@observe(tier="stage", metric="core.identity._insteadof_rules")
@functools.lru_cache(maxsize=64)
def _insteadof_rules() -> dict[str, str]:
    """Return the parsed insteadOf table from the live git config.

    Cached at process scope: the table rarely changes inside a session,
    and ``derive_project_id`` is on the hot path for memory writes.
    Failures are swallowed — no insteadOf is a normal state, not a bug.
    """
    try:
        out = subprocess.check_output(
            ["git", "config", "--get-regexp", r"^url\..*\.insteadof$"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):  # fmt: skip — I33 obs-coverage parser rejects the PEP 758 bare form
        return {}
    return _insteadof_rules_from_lines(out)


@observe(tier="stage", metric="core.identity._insteadof_rules_from_lines")
def _insteadof_rules_from_lines(raw: str) -> dict[str, str]:
    """Parse a ``git config --get-regexp '^url\\..*\\.insteadof$'`` dump.

    Returns ``{rewrite_target: source_pattern}``. Lines without the
    ``.insteadof`` suffix (e.g. ``.pushInsteadOf``) are dropped — they
    describe push rewrites, not fetches. Pulled out as a pure helper so
    tests can drive the parsing without subprocess.
    """
    rules: dict[str, str] = {}
    for line in raw.splitlines():
        if ".insteadof" not in line:
            continue
        key, _, value = line.partition(" ")
        if not value:
            continue
        # The key is ``url.<REWRITE>.insteadof`` — the rewrite target is the
        # middle segment. e.g. ``url.git@github-personal:.insteadof`` →
        # rewrite target = ``git@github-personal:``.
        prefix = "url."
        suffix = ".insteadof"
        if not (key.startswith(prefix) and key.endswith(suffix)):
            continue
        rewrite_target = key[len(prefix) : -len(suffix)]
        if rewrite_target:
            rules[rewrite_target] = value
    return rules


@observe(tier="stage", metric="core.identity._parse_insteadof_map")
def _parse_insteadof_map(rules: dict[str, str], url: str) -> str:
    """Apply *rules* to *url* until a fixed point.

    Pure (no subprocess, no I/O) — accepts the table as a parameter so
    tests can drive it without git. Iteration is bounded to avoid
    pathological self-rewriting tables (a rule that maps ``alpha`` to
    ``beta`` and another that maps ``beta`` to ``alpha`` must NOT spin
    forever).
    """
    current = url
    for _ in range(16):  # bounded — a 16-deep chain is already absurd
        changed = False
        for target, source in rules.items():
            if current.startswith(source) and current != target:
                current = target + current[len(source) :]
                changed = True
                break  # restart the rule scan after each rewrite
        if not changed:
            break
    return current


# ── remote normaliser (§16.4) ──────────────────────────────────────────────


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# SSH-style "scheme" (user@host:path, OR bare host:path) — strip up to
# the first ':' when the prefix has NO '/' (a real remote URL never has
# a slash before the host colon). The bare-host variant matters because
# an insteadOf rewrite TARGET can be just ``codeberg-agent:`` (a
# bare SSH alias, not ``user@codeberg-agent:``), and after resolution
# the live remote URL is ``codeberg-agent:foo/bar.git``. A path like
# ``m-agahi/yadgar.io`` has a slash BEFORE the colon, so it is NOT
# mistaken for an SSH remote.
_SSH_SCP_RE = re.compile(r"^(?:[^/@:]+@)?[^@/:]+:")


@observe(tier="stage", metric="core.identity._normalise_remote")
def _normalise_remote(url: str) -> str:
    """Reduce a git remote URL to its ``owner/repo`` (or ``group/sub/repo``) form.

    Host is excluded. Trailing ``.git`` is stripped. Casing is folded to
    lowercase. The path is preserved verbatim — nested namespaces are NOT
    collapsed (§16.9): splitting on the last ``/`` would lose a
    ``group/sub/repo`` triple's middle segment.
    """
    stripped = url.strip()

    # Branch on URL shape:
    #
    # * ``scheme://host/path``  — strip everything up to and including
    #   the third ``/`` (after scheme://host). Example:
    #   ``https://github.com/m-agahi/yadgar.git`` → ``m-agahi/yadgar.git``.
    # * ``user@host:path``       — strip ``user@host:`` (scp-like SSH).
    #   Example: ``git@github.com:foo/bar.git`` → ``foo/bar.git``.
    # * bare path                — pass through (rare in the wild; the
    #   ``origin`` remote is usually one of the above two shapes).
    if _SCHEME_RE.match(stripped):
        stripped = _SCHEME_RE.sub("", stripped)
        # Strip the host: everything up to and including the next ``/``.
        host_end = stripped.find("/")
        if host_end >= 0:
            stripped = stripped[host_end + 1 :]
    else:
        stripped = _SSH_SCP_RE.sub("", stripped)

    # Trailing ``.git`` ONLY — never mid-path. A repo named ``yadgar.io``
    # must keep its ``.io``.
    if stripped.endswith(".git"):
        stripped = stripped[: -len(".git")]

    return stripped.lower()


# ── .yadgar/project-id upward walk (§16.1) ─────────────────────────────────


@observe(tier="stage", metric="core.identity._walk_project_id_file")
def _walk_project_id_file(start: str) -> str | None:
    """Walk UP from *start* looking for ``.yadgar/project-id``.

    Return the file's content (whitespace-trimmed), or ``None`` when no
    file is found before the filesystem root. A file at any ancestor
    directory overrides remote derivation.
    """
    p = Path(start).resolve()
    for candidate in (p, *p.parents):
        project_id_file = candidate / ".yadgar" / "project-id"
        if project_id_file.is_file():
            return project_id_file.read_text(encoding="utf-8").strip()
    return None


# ── remote reader ──────────────────────────────────────────────────────────


@observe(tier="stage", metric="core.identity._origin_remote")
def _origin_remote(git_root: str) -> str:
    """Read ``remote.origin.url`` from *git_root*, or return empty.

    Subprocess failures (no git, no origin, timeout) collapse to ``""``
    so the caller can fall through to the local fallback without an
    explicit branch on every error class.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", git_root, "config", "remote.origin.url"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip()
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):  # fmt: skip — I33 obs-coverage parser rejects the PEP 758 bare form
        return ""
