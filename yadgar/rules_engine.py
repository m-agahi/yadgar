"""Neuro-symbolic rules engine for hard constraints and soft preferences over retrieval."""

import fnmatch
import logging
import re

# S3 (H-6): import the third-party `regex` library for use in write-path redact calls.
# `regex` is a drop-in replacement for stdlib `re` and supports a timeout= kwarg that
# raises regex.TimeoutError when a catastrophic-backtracking pattern exceeds the budget.
# Option A (regex library) chosen over:
#   B - AST pre-flight checker (incomplete; misses all patterns in practice)
#   C - subprocess per-call (prohibitive overhead on the write hot-path)
try:
    import regex as _regex_lib

    _REGEX_TIMEOUT_S = 1.0  # wall-clock seconds per redact call
except ImportError:  # pragma: no cover — only hits if package missing from env
    _regex_lib = None  # type: ignore[assignment]
    _REGEX_TIMEOUT_S = 0.0

from typing import Any

from yadgar.config import Settings
from yadgar.storage import StorageEngine

logger = logging.getLogger(__name__)

# Valid condition operators
VALID_OPERATORS = {
    "==",
    "!=",
    "contains",
    "not_contains",
    ">",
    "<",
    ">=",
    "<=",
    "matches",
}

# Fields that are numeric (use float comparison)
NUMERIC_FIELDS = {
    "heat",
    "importance",
    "surprise_score",
    "confidence",
    "emotional_valence",
    "plasticity",
    "stability",
    "excitability",
    "narrative_weight",
    "access_count",
    "useful_count",
    "compression_level",
    "reconsolidation_count",
}


def _parse_condition(condition: str) -> tuple[str, str, str]:
    """Parse a condition string into (field, operator, value).

    Supports conditions like:
        "language == typescript"
        "tag contains architecture"
        "importance > 0.7"
        "content not_contains password"
        "directory_context matches /project/*"
    """
    # Try multi-word operators first (not_contains)
    for op in ("not_contains",):
        if f" {op} " in condition:
            parts = condition.split(f" {op} ", 1)
            return parts[0].strip(), op, parts[1].strip()

    # Try two-char operators (>=, <=, ==, !=)
    for op in (">=", "<=", "==", "!="):
        if f" {op} " in condition:
            parts = condition.split(f" {op} ", 1)
            return parts[0].strip(), op, parts[1].strip()

    # Single-char operators (>, <)
    for op in (">", "<"):
        if f" {op} " in condition:
            parts = condition.split(f" {op} ", 1)
            return parts[0].strip(), op, parts[1].strip()

    # Word operators (contains, matches)
    for op in ("contains", "matches"):
        if f" {op} " in condition:
            parts = condition.split(f" {op} ", 1)
            return parts[0].strip(), op, parts[1].strip()

    raise ValueError(f"Cannot parse condition: {condition!r}")


def _parse_write_action(action: str) -> tuple[str, str, str]:
    """Parse a write-path action string.

    "filter"                               -> ("filter", "", "")
    "redact:password=[A-Za-z]+:password=***" -> ("redact", "password=[A-Za-z]+", "password=***")

    The pattern portion cannot contain a bare colon (use character classes instead).
    """
    if action == "filter":
        return "filter", "", ""
    if action.startswith("redact:"):
        rest = action[len("redact:") :]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid redact action {action!r}. Expected 'redact:pattern:replacement'"
            )
        return "redact", parts[0], parts[1]
    raise ValueError(
        f"Invalid write-path action: {action!r}. Expected 'filter' or 'redact:pattern:replacement'"
    )


def _parse_action(action: str) -> tuple[str, float]:
    """Parse an action string into (action_type, value).

    "filter" -> ("filter", 0.0)
    "boost:0.3" -> ("boost", 0.3)
    "penalty:0.2" -> ("penalty", 0.2)
    """
    import math

    if action == "filter":
        return "filter", 0.0
    if action.startswith("boost:"):
        val = float(action.split(":", 1)[1])
        if not math.isfinite(val):
            raise ValueError(f"boost value must be finite, got {val!r}")
        return "boost", val
    if action.startswith("penalty:"):
        val = float(action.split(":", 1)[1])
        if not math.isfinite(val):
            raise ValueError(f"penalty value must be finite, got {val!r}")
        return "penalty", val
    raise ValueError(f"Invalid action: {action!r}")


def _get_field_value(memory: dict, field: str) -> Any:
    """Get a field value from a memory dict.

    Supports direct memory fields plus special 'tag' field which checks tags list.
    """
    if field == "tag" or field == "tags":
        return memory.get("tags", [])
    if field in memory:
        return memory[field]
    # Check if the field matches a tag name (e.g., "language" might be a tag prefix)
    tags = memory.get("tags", [])
    for tag in tags:
        if ":" in tag:
            key, val = tag.split(":", 1)
            if key.strip() == field:
                return val.strip()
        elif "=" in tag:
            key, val = tag.split("=", 1)
            if key.strip() == field:
                return val.strip()
    return None


def _coerce_none_field(field_value: Any, operator: str) -> Any:
    """Coerce None field values to sensible defaults for comparison operators."""
    if field_value is not None:
        return field_value
    if operator in (">", "<", ">=", "<="):
        return 0.0
    if operator in ("==", "!=", "contains", "not_contains", "matches"):
        return ""
    return field_value


def _compare_numeric(field_value: Any, value: str, operator: str) -> bool:
    """Compare field_value to value using a numeric comparison operator.

    Returns False if either side cannot be cast to float.
    """
    try:
        num_field = float(field_value) if not isinstance(field_value, (int, float)) else field_value
        num_value = float(value)
    except (ValueError, TypeError):  # fmt: skip
        return False
    if operator == ">":
        return num_field > num_value
    if operator == "<":
        return num_field < num_value
    if operator == ">=":
        return num_field >= num_value
    # operator == "<="
    return num_field <= num_value


def _compare_equality(field_value: Any, value: str, field: str, operator: str) -> bool:
    """Compare field_value to value using == or != (numeric-aware, case-insensitive)."""
    if field in NUMERIC_FIELDS:
        try:
            numeric_match = float(field_value) == float(value)
            return numeric_match if operator == "==" else not numeric_match
        except (ValueError, TypeError):  # fmt: skip
            pass
    str_match = str(field_value).lower() == str(value).lower()
    return str_match if operator == "==" else not str_match


def _compare_contains(field_value: Any, value: str, operator: str) -> bool:
    """Evaluate contains / not_contains operator (case-insensitive, list-aware)."""
    if isinstance(field_value, list):
        found = any(value.lower() in str(item).lower() for item in field_value)
    else:
        found = value.lower() in str(field_value).lower()
    return found if operator == "contains" else not found


def _apply_score_delta(memory: dict, action_type: str, action_value: float) -> None:
    """Apply a boost or penalty delta to memory's _retrieval_score in-place."""
    score = memory.get("_retrieval_score", 0.0)
    if action_type == "boost":
        memory["_retrieval_score"] = score + action_value
    elif action_type == "penalty":
        memory["_retrieval_score"] = score - action_value


class RulesEngine:
    """Neuro-symbolic rules engine combining hard logical constraints
    and soft preferences with neural retrieval results."""

    def __init__(self, storage: StorageEngine, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings
        self._applicable_rules_cache: dict[str, list[dict]] = {}

    def add_rule(
        self,
        rule_type: str,
        scope: str,
        condition: str,
        action: str,
        priority: int = 0,
        scope_value: str | None = None,
    ) -> int:
        """Add a new rule.

        Args:
            rule_type: "hard" (must satisfy) or "soft" (preference)
            scope: "global", "directory", or "file"
            condition: Condition string (e.g., "importance > 0.7")
            action: Action string (e.g., "filter", "boost:0.3")
            priority: Higher = applied first (default 0)
            scope_value: Directory path or file pattern for scoped rules

        Returns:
            Rule ID
        """
        _READ_TYPES = ("hard", "soft")
        _WRITE_TYPES = ("write_block", "write_redact")
        if rule_type not in _READ_TYPES + _WRITE_TYPES:
            raise ValueError(
                f"rule_type must be one of {_READ_TYPES + _WRITE_TYPES!r}, got {rule_type!r}"
            )
        if scope not in ("global", "directory", "file"):
            raise ValueError(f"scope must be 'global', 'directory', or 'file', got {scope!r}")

        # Validate condition parses correctly
        _parse_condition(condition)

        if rule_type in _READ_TYPES:
            # Validate read-path action
            _parse_action(action)
            # Hard rules must use "filter" action
            if rule_type == "hard":
                action_type, _ = _parse_action(action)
                if action_type != "filter":
                    raise ValueError("Hard rules must use 'filter' action")
        else:
            # Validate write-path action
            _parse_write_action(action)
            if rule_type == "write_block":
                action_type, _, _ = _parse_write_action(action)
                if action_type != "filter":
                    raise ValueError("write_block rules must use 'filter' action")

        rule_id = self._storage.insert_rule(
            {
                "rule_type": rule_type,
                "scope": scope,
                "scope_value": scope_value,
                "condition": condition,
                "action": action,
                "priority": priority,
                "is_active": True,
            }
        )
        self._applicable_rules_cache.clear()
        return rule_id

    def get_applicable_rules(self, directory: str) -> list[dict]:
        """Get all active rules that apply to the given directory.

        Returns rules where:
        - scope == "global", OR
        - scope == "directory" AND scope_value is a prefix of directory, OR
        - scope == "file" AND scope_value matches directory as a glob pattern

        Sorted by priority descending (highest first).

        Results are cached per directory for the lifetime of this process.
        The cache is invalidated whenever rules are mutated (add_rule,
        delete_rule).  Use this method in hot paths; skip it only when
        freshness is required immediately after a rule mutation.
        """
        if directory in self._applicable_rules_cache:
            return self._applicable_rules_cache[directory]

        # Get global rules
        global_rules = self._storage.get_rules_for_scope("global")

        # Get directory-scoped rules: need to check prefix match
        # Query all active directory rules and filter by prefix
        dir_rules = []
        for rule in self._storage.get_all_active_rules_by_scope("directory"):
            sv = rule.get("scope_value") or ""
            if directory.startswith(sv):
                dir_rules.append(rule)

        # Get file-scoped rules: glob pattern matching
        file_rules = []
        for rule in self._storage.get_all_active_rules_by_scope("file"):
            sv = rule.get("scope_value") or ""
            if fnmatch.fnmatch(directory, sv):
                file_rules.append(rule)

        # Combine and sort by priority descending
        all_rules = global_rules + dir_rules + file_rules
        all_rules.sort(key=lambda r: r.get("priority", 0), reverse=True)
        self._applicable_rules_cache[directory] = all_rules
        return all_rules

    def apply_rules(self, memories: list[dict], directory: str) -> list[dict]:
        """Apply rules to filter and re-rank memories.

        Hard rules filter out non-matching memories.
        Soft rules adjust _retrieval_score via boost/penalty.

        Args:
            memories: List of memory dicts (must have _retrieval_score)
            directory: Current directory context

        Returns:
            Filtered and re-ranked list of memories
        """
        rules = self.get_applicable_rules(directory)
        if not rules:
            return memories

        result = list(memories)

        for rule in rules:
            result = self._apply_single_rule(result, rule)

        # Re-sort by score after soft rule adjustments
        result.sort(key=lambda m: m.get("_retrieval_score", 0.0), reverse=True)
        return result

    def _apply_single_rule(self, memories: list[dict], rule: dict) -> list[dict]:
        """Apply one rule to the memory list; return updated list."""
        rule_type = rule["rule_type"]
        condition = rule["condition"]
        action = rule["action"]

        if rule_type == "hard":
            return [m for m in memories if self.evaluate_condition(condition, m)]

        if rule_type == "soft":
            action_type, action_value = _parse_action(action)
            for m in memories:
                if self.evaluate_condition(condition, m):
                    _apply_score_delta(m, action_type, action_value)

        return memories

    def evaluate_condition(self, condition: str, memory: dict) -> bool:
        """Evaluate a condition against a memory.

        Args:
            condition: Condition string like "importance > 0.7"
            memory: Memory dict

        Returns:
            True if the condition is satisfied
        """
        try:
            field, operator, value = _parse_condition(condition)
        except ValueError:
            # Q19: fail-closed — unparseable hard-rule conditions must NOT silently pass
            logger.warning("Failed to parse condition (fail-closed): %s", condition)
            return False

        field_value = _coerce_none_field(_get_field_value(memory, field), operator)

        if operator in (">", "<", ">=", "<="):
            return _compare_numeric(field_value, value, operator)

        if operator in ("==", "!="):
            return _compare_equality(field_value, value, field, operator)

        if operator in ("contains", "not_contains"):
            return _compare_contains(field_value, value, operator)

        if operator == "matches":
            return fnmatch.fnmatch(str(field_value), value)

        return True  # Unknown operator passes by default

    def delete_rule(self, rule_id: int) -> bool:
        """Deactivate a rule (soft delete).

        Returns True if the rule existed and was deactivated.
        """
        if self._storage.get_rule(rule_id) is None:
            return False
        self._storage.update_rule(rule_id, {"is_active": False})
        self._applicable_rules_cache.clear()
        return True

    def get_all_rules(self) -> list[dict]:
        """Return all active rules, sorted by scope then priority."""
        return self._storage.get_all_active_rules()

    def check_write_policy(
        self, content: str, context: str, tags: list[str]
    ) -> tuple[bool, str, str | None]:
        """Apply write-path rules to content before storage.

        Evaluates write_block and write_redact rules in priority order.
        Secrets check is separate (secrets.check_secrets) and runs first.

        Args:
            content: The text about to be stored.
            context: Directory path (used for scope matching).
            tags: Memory tags.

        Returns:
            (blocked, reason, modified_content) where:
            - blocked=True: a write_block rule matched → don't store
            - modified_content: redacted text if a write_redact rule fired, else None
        """
        mem: dict = {"content": content, "directory_context": context, "tags": tags}

        all_rules = self.get_applicable_rules(context)
        write_rules = [r for r in all_rules if r["rule_type"] in ("write_block", "write_redact")]

        if not write_rules:
            return False, "", None

        modified = content
        was_redacted = False

        for rule in write_rules:
            if not self.evaluate_condition(rule["condition"], mem):
                continue

            if rule["rule_type"] == "write_block":
                return True, rule["condition"], None

            # write_redact
            action_type, pattern, replacement = _parse_write_action(rule["action"])
            if action_type == "redact" and pattern:
                try:
                    if _regex_lib is not None:
                        # S3 (H-6): use third-party `regex` library with a hard timeout.
                        # regex.TimeoutError is raised when CPU time exceeds _REGEX_TIMEOUT_S,
                        # preventing catastrophic-backtracking (ReDoS) patterns from hanging
                        # the write hot-path indefinitely.
                        modified = _regex_lib.sub(
                            pattern, replacement, modified, timeout=_REGEX_TIMEOUT_S
                        )
                    else:
                        # Fallback: stdlib re (no timeout) — only if regex pkg is absent.
                        modified = re.sub(pattern, replacement, modified)
                    was_redacted = True
                    mem = {**mem, "content": modified}
                except re.error as exc:
                    logger.warning("Bad redact pattern %r: %s", pattern, exc)
                except Exception as exc:
                    # Catches regex.TimeoutError and any other runtime error from the
                    # `regex` library. Log and continue without applying this redact rule.
                    logger.warning(
                        "Redact pattern %r timed out or errored (%s: %s) — skipping rule",
                        pattern,
                        type(exc).__name__,
                        exc,
                    )

        return False, "", modified if was_redacted else None

    def export_rules(self) -> list[dict]:
        """Serialize all active rules to a list of plain dicts (YAML/JSON-safe)."""
        return [
            {
                "rule_type": r["rule_type"],
                "scope": r["scope"],
                "condition": r["condition"],
                "action": r["action"],
                "priority": r.get("priority", 0),
                "scope_value": r.get("scope_value"),
            }
            for r in self.get_all_rules()
        ]

    def import_rules(self, rules: list[dict]) -> int:
        """Import rules from a list of dicts. Returns count of successfully imported rules."""
        count = 0
        for rule in rules:
            try:
                self.add_rule(
                    rule_type=rule["rule_type"],
                    scope=rule["scope"],
                    condition=rule["condition"],
                    action=rule["action"],
                    priority=rule.get("priority", 0),
                    scope_value=rule.get("scope_value"),
                )
                count += 1
            except (ValueError, KeyError) as exc:
                logger.warning("Skipping invalid rule during import: %s — %s", rule, exc)
        return count
