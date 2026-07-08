"""Shared write-path helpers (R3 Car 1 write-half).

Relocated here from ``yadgar.core.server._helpers`` /
``yadgar.core.server.tools.{memorize,project}`` so the backend write-exec
phases import only ``_shared`` + backend (no ``core.*`` edge on the write path).

Imports ``_shared`` only — never ``yadgar.core.*``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import math
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yadgar._shared.runtime.state as _st
from yadgar._shared.config import get_settings
from yadgar._shared.observability.observe import observe

logger = logging.getLogger(__name__)

# Strong decision patterns for auto-protection
_DECISION_STRONG_RE = re.compile(
    r"\b(chose .+ over|decided to use|switched from .+ to|migrated from|"
    r"will use .+ instead|going with|opted for|selected .+ because|"
    r"choosing .+ approach|picking .+ strategy)\b",
    re.IGNORECASE,
)


@observe(tier="stage")
def _has_unpaired_surrogate(s: str) -> bool:
    """Return True if the string contains unpaired UTF-16 surrogate code points,
    which cannot be encoded as UTF-8 and would crash the storage pipeline."""
    if not s:
        return False
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


@observe(tier="stage")
def _push_event(event: dict) -> None:
    """Append an event to the ring buffer with a monotonic sequence number."""
    with _st._event_lock:
        _st._event_seq += 1
        _st._event_queue.append({"seq": _st._event_seq, **event})


@observe(tier="stage")
def _file_hash(filepath: str) -> str | None:
    """Compute SHA-256 hash of a file if it is under a registered project root.

    §4 security requirements:
    - Only hashes files under directories registered via seed_project.
    - Skips files larger than YADGAR_MAX_HASH_BYTES (default 10 MiB).
    - Streams in 64 KiB chunks — never reads the full file into memory.
    """
    try:
        p = Path(filepath).expanduser().resolve()
    except Exception:
        return None
    if not p.is_file():
        return None

    # Whitelist: only hash files under a registered project root.
    str_path = str(p)
    if _st._project_roots:
        allowed = any(
            str_path == root or str_path.startswith(root + os.sep) for root in _st._project_roots
        )
        if not allowed:
            logger.debug("_file_hash: %s outside registered project roots — skipped", str_path)
            return None

    # Size cap — skip files larger than YADGAR_MAX_HASH_BYTES.
    max_bytes = get_settings().MAX_HASH_BYTES
    try:
        if p.stat().st_size > max_bytes:
            logger.debug(
                "_file_hash: %s exceeds MAX_HASH_BYTES (%d) — skipped", str_path, max_bytes
            )
            return None
    except OSError:
        return None

    # Stream-hash in 64 KiB chunks — no read_bytes().
    h = hashlib.sha256()
    try:
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


# ── valid_until computation (was tools.memorize._compute_valid_until) ──


@observe(tier="hot", metric="tools.memorize._compute_valid_until")
def _compute_valid_until(
    tier: str | None,
    valid_until: str | None,
    ttl_days: int | None,
    settings,
) -> str | None:
    """Compute the valid_until ISO-8601 UTC string from tier/valid_until/ttl_days.

    Resolution order:
      1. valid_until provided → validate timezone + return as-is.
      2. ttl_days provided → now + ttl_days.
      3. tier=semantic_immortal → None (no expiry).
      4. tier=conditional → now + ANCHOR_CONDITIONAL_TTL_DAYS.
      5. tier=ephemeral → now + ANCHOR_EPHEMERAL_TTL_DAYS.
      6. tier=None → None (non-anchor memory, no expiry logic).
    """
    if valid_until is not None:
        try:
            dt = datetime.fromisoformat(valid_until)
        except ValueError as exc:
            raise ValueError(f"invalid valid_until format: {valid_until!r}") from exc
        if dt.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware UTC (naive datetime rejected)")
        return valid_until
    if ttl_days is not None:
        return (datetime.now(UTC) + timedelta(days=int(ttl_days))).isoformat()
    if tier == "semantic_immortal":
        return None
    if tier == "conditional":
        days = int(getattr(settings, "ANCHOR_CONDITIONAL_TTL_DAYS", 90))
        return (datetime.now(UTC) + timedelta(days=days)).isoformat()
    if tier == "ephemeral":
        days = int(getattr(settings, "ANCHOR_EPHEMERAL_TTL_DAYS", 14))
        return (datetime.now(UTC) + timedelta(days=days)).isoformat()
    return None


# ── git-root resolution + structural epoch bump ──
# (was tools.project._git_safe_env / _GIT_SAFE_ARGS / _resolve_project_root /
#  _bump_epoch_for_context — relocated so the backend write path does not import
#  yadgar.core.server.tools.project.)


def _git_safe_env() -> dict:
    """Return an env dict that prevents .git/config code-execution attacks (H-10).

    A malicious .git/config in a user-supplied directory can set
    core.fsmonitor or core.sshCommand to execute arbitrary commands on
    the next git invocation.  Passing GIT_CONFIG_NOSYSTEM + pointing
    GIT_CONFIG_GLOBAL at /dev/null plus disabling network protocols
    eliminates all git-config-driven execution paths.
    """
    return {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


# Extra argv to pass to every git invocation — disable network protocols
# so a crafted .git/config cannot trigger remote fetch/clone helpers.
_GIT_SAFE_ARGS = [
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "uploadpack.allowFilter=false",
]


@observe(tier="stage", metric="tools.project._resolve_project_root")
@functools.lru_cache(maxsize=1024)
def _resolve_project_root(directory: str) -> str:
    """Resolve the git project root for a directory (walk-up via git rev-parse).

    Falls back to the given directory if not inside a git repo or git is unavailable.

    Cached with no TTL: a directory's git-root is process-stable (unlike branch,
    which uses a 30s bucket because it changes). Caching keeps the memorize/forget
    write-path epoch normalization (which resolves via this helper) off a git
    subprocess after the first touch per directory — within the I9 ≤5ms budget.
    """
    try:
        out = (
            subprocess.check_output(
                ["git", *_GIT_SAFE_ARGS, "-C", directory, "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
                timeout=2,
                env=_git_safe_env(),
            )
            .decode()
            .strip()
        )
        if out:
            return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):  # fmt: skip
        pass
    return directory


@observe(tier="hot", metric="tools.project._bump_epoch_for_context")
def _bump_epoch_for_context(context: str | None) -> None:
    """Advance the structural epoch for `context`, normalized to its git-root.

    The single shared bump helper for every structural write (memorize, forget).
    Normalizing to git-root here — the SAME resolution project_brief's key uses —
    is what makes the epoch actually bust the cached brief instead of landing on a
    different, never-read _DIR_EPOCH key (the decorative-epoch bug). Fully guarded:
    instrumentation must never break or block the write path."""
    try:
        from yadgar._shared.runtime.cache_epoch import bump_epoch  # noqa: PLC0415

        resolved = _resolve_project_root(context) if context else None
        bump_epoch(resolved)
    except Exception:  # pragma: no cover - must never break writes
        pass


# ── Anchor hygiene helpers ──
# (was tools.project._ANCHOR_PROMOTE_TAGS / _FENCED_CODE_BLOCK_RE / _MD_HEADER_RE /
#  _count_markdown_headers / _cosine_similarity — relocated so the backend anchor
#  audit exec (yadgar.backend.admin_exec.audit) does not import
#  yadgar.core.server.tools.project. project.py re-exports these for its own use.)

# Tag set that qualifies an anchor for promote-to-wiki detection.
# Triple AND: word_count > ANCHOR_PROMOTE_WORDS AND header_count >= ANCHOR_PROMOTE_HEADERS
# AND tags ∩ _ANCHOR_PROMOTE_TAGS ≠ ∅.
_ANCHOR_PROMOTE_TAGS: frozenset[str] = frozenset(
    {"rule", "pattern", "convention", "playbook", "workflow", "recipe"}
)

# Compiled regex to strip fenced code blocks before counting markdown headers.
# Handles ``` and ~~~ delimiters; DOTALL so . matches newlines inside blocks.
_FENCED_CODE_BLOCK_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)

# Compiled regex for markdown headers (after code-block stripping).
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


@observe(tier="stage", metric="tools.project._count_markdown_headers")
def _count_markdown_headers(content: str) -> int:
    """Count level-1..6 markdown headers in content, excluding fenced code blocks.

    Strips ``` and ~~~ fenced blocks first to avoid counting # comments inside
    code blocks (e.g. Python # comment, shell ## heading).
    """
    stripped = _FENCED_CODE_BLOCK_RE.sub("", content)
    return len(_MD_HEADER_RE.findall(stripped))


@observe(tier="hot", metric="tools.project._cosine_similarity")
def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two float vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── query-with-timeout (was tools/core _helpers._q_with_timeout) ──
# Relocated so the backend invariants exec (yadgar.backend.admin_exec.invariants)
# imports only _shared. core.server._helpers re-exports it for existing callers.


@observe(tier="stage", metric="server._helpers._q_with_timeout")
def _q_with_timeout(
    storage, surql: str, params: dict | None = None, timeout_seconds: int = 60
) -> list:  # noqa: E501
    """Run a storage query with an optional per-request timeout.

    In server (httpx) mode the httpx Client timeout is temporarily widened to
    *timeout_seconds*.  In embedded mode _q handles its own retry.  Always routes
    through storage._q so test stubs patching _q remain effective.
    """
    http = getattr(storage, "_http", None)
    if http is not None:
        try:
            import httpx as _httpx  # noqa: PLC0415
        except ImportError:
            return storage._q(surql, params)
        old_timeout = http.timeout
        try:
            http.timeout = _httpx.Timeout(float(timeout_seconds))
            return storage._q(surql, params)
        finally:
            http.timeout = old_timeout
    return storage._q(surql, params)
