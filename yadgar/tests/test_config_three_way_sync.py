"""I25: Config three-way-sync invariant test.

Every Settings field in yadgar/config.py MUST be either:
  (a) present in FIELD_META (config_yaml.py) AND
      present in _REGISTRY (config_registry.py, via list_config()), OR
  (b) listed in yadgar/tests/config_env_only_allowlist.txt as either:
      - a Tier-1 intentional env-only knob with a structured reason=<category>
        annotation (secrets, infra-wiring, container paths, etc.), OR
      - a Tier-2 grandfathered backlog entry (pre-existing drift, no reason
        required; separated by the GRANDFATHERED marker comment).

Naming conventions (used to normalise across all three surfaces):
  - Settings.model_fields keys:  uppercase, no prefix    (e.g. HEAVY_RERANK_ENABLED)
  - FIELD_META keys:             lowercase, no prefix    (e.g. heavy_rerank_enabled)
  - ConfigEntry.name:            uppercase, YADGAR_ prefix (e.g. YADGAR_HEAVY_RERANK_ENABLED)

The canonical env-name form is YADGAR_<FIELD_UPPER>.  All comparisons use that form.

Ratchet behaviour:
  - test_all_settings_fields_covered: FAILS if any field is uncovered
    (not in yaml+registry AND not in allowlist).  GREEN once allowlist is complete.
    Also FAILS if a new Settings field is added without updating yaml+registry or
    allowlist.  This is the forward-looking ratchet.
  - test_allowlist_entries_have_yadgar_prefix: FAILS if allowlist has entries
    missing the YADGAR_ prefix (catches typos).
  - test_tier1_entries_have_valid_reason: FAILS if any Tier-1 entry is missing
    a reason=<category> annotation or uses an unrecognised category.
"""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWLIST_PATH = Path(__file__).parent / "config_env_only_allowlist.txt"

# Approved reason categories for Tier-1 (env-only) allowlist entries.
# New knobs MUST default to yaml-backed (three-way registered); env-only is the
# exception and requires a reviewer-visible justification via one of these categories.
VALID_REASONS = {
    "secret",  # credentials/tokens, never persist to yaml on disk
    "infra-wiring",  # URL/path differs per deploy target
    "bootstrap-path",  # chicken-and-egg (yaml file location itself)
    "deployment-flag",  # context marker, not user config
    "downstream-process",  # env for a forked subprocess, not yadgar Python
    # Note: dead-env-pending-removal is validated separately via _DEAD_ENV_VERSION_RE
    # (requires :vX.Y.Z suffix) and is NOT included in this set.
}

# Marker comment that separates Tier-1 entries from Tier-2 grandfathered entries.
_TIER2_MARKER = "GRANDFATHERED"

# Pattern for the dead-env-pending-removal version suffix (must have :vX.Y.Z).
_DEAD_ENV_VERSION_RE = re.compile(r"^dead-env-pending-removal:v\d+\.\d+\.\d+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_allowlist() -> tuple[dict[str, str], set[str]]:
    """Parse allowlist into (tier1, tier2).

    Returns:
        tier1: dict mapping env-name -> reason string (e.g. "secret")
        tier2: set of raw env-names (no reason required)

    Lines starting with '#' and blank lines are ignored.
    Once the GRANDFATHERED marker comment is seen, subsequent plain key-only
    lines go into tier2 instead of tier1.
    """
    tier1: dict[str, str] = {}
    tier2: set[str] = set()
    if not _ALLOWLIST_PATH.exists():
        return tier1, tier2

    in_tier2 = False
    for raw_line in _ALLOWLIST_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if _TIER2_MARKER in line:
                in_tier2 = True
            continue

        parts = line.split()
        key = parts[0]
        reason_token = parts[1] if len(parts) > 1 else None

        if reason_token and reason_token.startswith("reason="):
            # Explicit reason= → always Tier-1 regardless of position
            reason = reason_token[len("reason=") :]
            tier1[key] = reason
        elif in_tier2:
            tier2.add(key)
        else:
            # Pre-marker, no reason= → also Tier-1 (will fail reason validation)
            tier1[key] = ""

    return tier1, tier2


def _load_allowlist() -> set[str]:
    """Return set of YADGAR_* env names present in the allowlist file.

    Lines starting with '#' and blank lines are ignored.
    Returns all entries (both tiers) as a flat set for coverage checks.
    """
    tier1, tier2 = _parse_allowlist()
    return set(tier1.keys()) | tier2


def _compute_gaps() -> tuple[list[str], list[str]]:
    """Return (missing_yaml, missing_registry) for all non-allowlisted Settings fields."""
    from yadgar.config import Settings
    from yadgar.config_registry import list_config
    from yadgar.config_yaml import FIELD_META

    allowlist = _load_allowlist()
    registry_names = {e.name for e in list_config()}
    yaml_keys = set(FIELD_META.keys())

    missing_yaml: list[str] = []
    missing_registry: list[str] = []

    for field_upper in Settings.model_fields:
        env_name = f"YADGAR_{field_upper}"
        if env_name in allowlist:
            continue
        if field_upper.lower() not in yaml_keys:
            missing_yaml.append(env_name)
        if env_name not in registry_names:
            missing_registry.append(env_name)

    return sorted(missing_yaml), sorted(missing_registry)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigThreeWaySync:
    """I25: every Settings field must be three-way registered or allowlisted."""

    def test_allowlist_file_exists(self) -> None:
        """Allowlist file must exist for the ratchet to function."""
        assert _ALLOWLIST_PATH.exists(), (
            f"Missing: {_ALLOWLIST_PATH}\n"
            "Create with intentional env-only + grandfathered backlog entries "
            "(one YADGAR_* per line)."
        )

    def test_all_settings_fields_covered(self) -> None:
        """Every Settings field is in yaml+registry OR in the allowlist.

        GREEN once allowlist fully covers the pre-existing backlog.
        Any new field added to Settings without yaml+registry coverage turns this RED.
        """
        missing_yaml, missing_registry = _compute_gaps()

        errors: list[str] = []
        if missing_yaml:
            errors.append(
                f"{len(missing_yaml)} Settings field(s) missing from FIELD_META "
                f"(config_yaml.py) and not in allowlist:\n  " + "\n  ".join(missing_yaml)
            )
        if missing_registry:
            errors.append(
                f"{len(missing_registry)} Settings field(s) missing from _REGISTRY "
                f"(config_registry.py) and not in allowlist:\n  " + "\n  ".join(missing_registry)
            )

        assert not errors, (
            "I25 config three-way-sync violation(s):\n\n"
            + "\n\n".join(errors)
            + "\n\nFix options:\n"
            "  1. Add missing entries to FIELD_META (config_yaml.py) and/or _REGISTRY "
            "(config_registry.py).\n"
            "  2. Add to yadgar/tests/config_env_only_allowlist.txt if intentionally "
            "env-only or backlog-tracked."
        )

    def test_allowlist_entries_have_yadgar_prefix(self) -> None:
        """Every non-comment allowlist entry must start with YADGAR_.

        Catches typos like missing prefix or wrong casing.
        """
        allowlist = _load_allowlist()
        invalid = [name for name in sorted(allowlist) if not name.startswith("YADGAR_")]
        assert not invalid, "Allowlist entries must start with YADGAR_:\n  " + "\n  ".join(invalid)

    def test_tier1_entries_have_valid_reason(self) -> None:
        """Every Tier-1 allowlist entry must carry a valid reason=<category> annotation.

        Tier-1 entries are those before the GRANDFATHERED marker comment.
        A missing or unrecognised reason category is a hard failure — new env-only
        knobs must justify themselves with a reviewer-visible category.

        Valid categories: {valid}

        For dead-env-pending-removal, a version suffix is required:
          reason=dead-env-pending-removal:vX.Y.Z
        """.format(valid=", ".join(sorted(VALID_REASONS)))
        tier1, _tier2 = _parse_allowlist()

        errors: list[str] = []
        for key, reason in sorted(tier1.items()):
            if not reason:
                errors.append(
                    f"Tier-1 entry '{key}' has invalid reason ''."
                    f" Must be one of {VALID_REASONS} or 'dead-env-pending-removal:vX.Y.Z'"
                )
                continue

            base_reason = reason.split(":")[0] if ":" in reason else reason

            if base_reason == "dead-env-pending-removal":
                if not _DEAD_ENV_VERSION_RE.match(reason):
                    errors.append(
                        f"Tier-1 entry '{key}' has invalid reason '{reason}'."
                        f" Must be one of {VALID_REASONS} or 'dead-env-pending-removal:vX.Y.Z'"
                    )
            elif base_reason not in VALID_REASONS:
                errors.append(
                    f"Tier-1 entry '{key}' has invalid reason '{reason}'."
                    f" Must be one of {VALID_REASONS} or 'dead-env-pending-removal:vX.Y.Z'"
                )

        assert not errors, (
            "I25 Tier-1 allowlist entries with missing/invalid reason= annotation:\n\n"
            + "\n".join(errors)
            + "\n\nNew env-only knobs must default to yaml-backed (three-way registered)."
            " Add reason= only when env-only is genuinely required."
        )
