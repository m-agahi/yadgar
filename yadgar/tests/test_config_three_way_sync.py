"""I25: Config three-way-sync invariant test.

Every Settings field in yadgar/config.py MUST be either:
  (a) present in FIELD_META (config_yaml.py) AND
      present in _REGISTRY (config_registry.py, via list_config()), OR
  (b) listed in yadgar/tests/config_env_only_allowlist.txt as either:
      - an intentional env-only knob (secrets, infra-wiring, container paths), OR
      - a grandfathered backlog entry (pre-existing drift, tracked for follow-up PRs).

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
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWLIST_PATH = Path(__file__).parent / "config_env_only_allowlist.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_allowlist() -> set[str]:
    """Return set of YADGAR_* env names present in the allowlist file.

    Lines starting with '#' and blank lines are ignored.
    """
    if not _ALLOWLIST_PATH.exists():
        return set()
    lines = _ALLOWLIST_PATH.read_text().splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


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
