"""Secret-gate allowlist + audit trail (v5.13.0).

Provides context-aware bypass for the secret-gate pattern scanner.  When
gate_or_reject() is called with matching tags and the content matches an
allowlisted pattern prefix, the write is allowed through — but every hit is
recorded to an append-only JSONL audit log.

Configuration (env vars):
  YADGAR_SECRET_GATE_ALLOWLIST_PATH   Path to user YAML allowlist.
                                      Default: ~/.config/yadgar/secret-gate-allowlist.yaml
                                      Set to /nonexistent or unset → default-deny
                                      (zero allowlisted patterns).
  YADGAR_SECRET_GATE_AUDIT_DIR        Directory for JSONL audit logs.
                                      Default: ~/.local/state/yadgar/secret-gate-audit/
                                      Logs named YYYY-MM-DD.jsonl (date-based rotation).

Allowlist YAML schema (version 1):
  allowlist:
    - tags: ["test-fixture"]          # list of tags that must be present
      patterns: ["ghp_*"]             # glob-style prefix patterns to bypass
      reason: "human-readable reason"

Schema rules:
  - Top-level key must be "allowlist".
  - Each entry must have: tags (list), patterns (list), reason (str).
  - Pattern matching: if content_field starts-with or contains a token that
    starts-with the pattern prefix (stripping trailing "*").
  - Tag match: ALL tags in the entry must be present in the call-site tags list.
    (Entry tags = required subset; call tags = superset.)

Audit log entry fields (JSONL):
  ts              ISO-8601 UTC timestamp
  matched_pattern Pattern string that caused the bypass
  tags            Tags that were present at call site
  reason          Reason string from the allowlist entry
  source          Call-site name (inferred from inspect.stack())
  content_preview First 80 chars of the first content field that was scanned
"""

from __future__ import annotations

import fnmatch
import inspect
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yadgar.paths as _paths
from yadgar.observability.observe import observe

_log = logging.getLogger(__name__)

_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowlistEntry:
    tags: frozenset[str]
    patterns: tuple[str, ...]
    reason: str


# Module-level state — reloaded on demand via _reload_allowlist()
_allowlist: list[AllowlistEntry] = []
_allowlist_loaded: bool = False


@observe(tier="stage")
def _get_allowlist_path() -> Path:
    env = os.environ.get("YADGAR_SECRET_GATE_ALLOWLIST_PATH", "")
    if env:
        return Path(env)
    return _paths.SECRET_GATE_ALLOWLIST_PATH


@observe(tier="stage")
def _get_audit_dir() -> Path:
    env = os.environ.get("YADGAR_SECRET_GATE_AUDIT_DIR", "")
    if env:
        return Path(env)
    return _paths.SECRET_GATE_AUDIT_DIR


@observe(tier="stage")
def _reload_allowlist() -> None:
    """Load (or reload) the allowlist from disk.  Thread-safe.

    Raises ValueError if the file exists but is malformed or has wrong schema.
    If the file does not exist, loads an empty allowlist (default-deny).
    """
    global _allowlist, _allowlist_loaded
    path = _get_allowlist_path()

    if not path.exists():
        with _LOCK:
            _allowlist = []
            _allowlist_loaded = True
        return

    try:
        from ruamel.yaml import YAML  # noqa: PLC0415

        y = YAML()
        with open(path) as fh:
            raw = y.load(fh)
    except Exception as exc:
        raise ValueError(f"allowlist YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict) or "allowlist" not in raw:
        raise ValueError(
            f"allowlist YAML at {path} must have top-level 'allowlist' key, "
            f"got keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__}"
        )

    entries: list[AllowlistEntry] = []
    for i, item in enumerate(raw["allowlist"] or []):
        if not isinstance(item, dict):
            raise ValueError(f"allowlist entry {i} must be a dict, got {type(item).__name__}")
        missing = {"tags", "patterns", "reason"} - set(item.keys())
        if missing:
            raise ValueError(f"allowlist entry {i} missing required keys: {missing}")
        entries.append(
            AllowlistEntry(
                tags=frozenset(str(t) for t in item["tags"]),
                patterns=tuple(str(p) for p in item["patterns"]),
                reason=str(item["reason"]),
            )
        )

    with _LOCK:
        _allowlist = entries
        _allowlist_loaded = True

    _log.info(
        "secret-gate allowlist loaded: %d entries from %s",
        len(entries),
        path,
    )


@observe(tier="stage")
def _ensure_loaded() -> None:
    """Lazy-load allowlist on first use."""
    if not _allowlist_loaded:
        _reload_allowlist()


@observe(tier="stage")
def _detect_source() -> str:
    """Walk the call stack and return a short name for the call site.

    Heuristic:
    - Frame file contains '/tests/' → "test:<basename>"
    - Frame file contains '/server/tools/' → "tool:<basename>"
    - Frame file contains '/curation/' or 'ingest' → "doc-ingest"
    - Else "unknown"

    Only walks frames when allowlist is non-empty (perf: skip on default-deny).
    """
    try:
        frames = inspect.stack()
        for frame_info in frames:
            filename = frame_info.filename or ""
            # Normalise to forward slashes for cross-platform matching
            fname = filename.replace("\\", "/")
            if "/tests/" in fname:
                return f"test:{Path(filename).name}"
            if "/server/tools/" in fname:
                return f"tool:{Path(filename).stem}"
            if "/curation/" in fname or "ingest" in fname:
                return "doc-ingest"
    except Exception:
        pass
    return "unknown"


@observe(tier="stage")
def _content_matches_pattern(content: str, pattern: str) -> bool:
    """Return True if content contains a token matching the glob pattern.

    Pattern "ghp_*" matches any occurrence of a substring starting with "ghp_".
    Uses fnmatch for full glob support on the whole content string (simpler and
    accurate enough for the prefix-style patterns we use).
    """
    # Fast path: strip trailing "*" and check prefix containment
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return prefix in content
    # General glob match on entire content
    return fnmatch.fnmatch(content, f"*{pattern}*")


@observe(tier="stage")
def _write_audit(
    *,
    matched_pattern: str,
    tags: list[str],
    reason: str,
    source: str,
    content_preview: str,
) -> None:
    """Append one JSONL entry to the audit log for today."""
    audit_dir = _get_audit_dir()
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        log_path = audit_dir / f"{today}.jsonl"
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "matched_pattern": matched_pattern,
            "tags": list(tags),
            "reason": reason,
            "source": source,
            "content_preview": content_preview[:80],
        }
        with _LOCK:
            with open(log_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:
        _log.exception("LOUD: failed to write allowlist audit entry — this must not be silenced")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@observe(tier="boundary")
def is_allowlisted(
    content: str,
    tags: list[str] | None,
    source: str,
) -> tuple[bool, AllowlistEntry | None]:
    """Check whether content+tags combo is covered by an allowlist entry.

    Args:
        content:  The text field being scanned.
        tags:     Call-site tags (from gate_or_reject tags= kwarg).
        source:   Call-site identifier (from _detect_source()).

    Returns:
        (True, entry) if allowlisted — caller must write audit and return clean.
        (False, None) if not allowlisted — caller must proceed with pattern scan.
    """
    _ensure_loaded()
    if not _allowlist or not tags:
        return False, None

    tag_set = frozenset(tags)
    for entry in _allowlist:
        # Entry tags must be a SUBSET of call-site tags
        if not entry.tags.issubset(tag_set):
            continue
        # Content must match at least one pattern in the entry
        for pat in entry.patterns:
            if _content_matches_pattern(content, pat):
                return True, entry

    return False, None
