"""Pattern classification, schema abstraction, and degenerate-detection helpers."""

import re
from collections import defaultdict

from yadgar.observability.observe import observe

# Decision/convention keywords → semantic candidate
_DECISION_KEYWORDS = re.compile(
    r"\b(always|never|prefer|standard|convention|rule|guideline|best practice|"
    r"must|should always|should never)\b",
    re.IGNORECASE,
)

# Architecture/pattern keywords → semantic candidate
_ARCHITECTURE_KEYWORDS = re.compile(
    r"\b(pattern|architecture|design|principle|paradigm|framework|methodology|"
    r"approach|strategy|abstraction|interface|protocol)\b",
    re.IGNORECASE,
)

# Tags that indicate semantic content
_SEMANTIC_TAGS = frozenset(
    {
        "rule",
        "convention",
        "preference",
        "standard",
        "architecture",
        "principle",
        "guideline",
        "best-practice",
        "design-pattern",
    }
)

# Prefix pattern emitted by abstract_to_schema — stripped before subject check
_RECURRING_PREFIX_RE = re.compile(
    r"^Recurring pattern(?: across \d+ observations)?:\s*", re.IGNORECASE
)

# Tags suffix appended by abstract_to_schema — stripped before subject check
_TAGS_SUFFIX_RE = re.compile(r"\s*\[tags:[^\]]*\]\s*$", re.IGNORECASE)

# File-extension indicator (path/to/file.py, config.json, readme.md, …)
_FILE_EXTENSION_RE = re.compile(r"\b\w[\w./]*\.\w{2,6}\b")

# Stop-words that carry no identifier signal — superset of abstract_to_schema's list
_SUBJECT_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "was",
        "were",
        "are",
        "has",
        "had",
        "have",
        "been",
        "will",
        "would",
        "could",
        "should",
        "can",
        "may",
        "might",
        "its",
        "but",
        "not",
        "all",
        "any",
        "each",
        "also",
        "into",
        "than",
        "then",
        "when",
        "which",
        "who",
        "how",
        "what",
        "where",
        "there",
        "here",
        "does",
        "frequently",
        "modified",
        "together",
        "often",
        "always",
        "never",
        "sometimes",
        "usually",
        "across",
        "between",
        "observations",
        "recurring",
        "pattern",
        # Yadgar-internal noise: entity-namespace words that appear in
        # _memify_derive placeholders but carry no project-code signal
        "memory",
        "entity",
        "used",
        "using",
        "uses",
    }
)


# Canonical degenerate body — exact known-bad pattern from _memify_derive clusters
_DEGENERATE_BODY_RE = re.compile(r"^frequently\s+modified\s+together$", re.IGNORECASE)

# Stop-words used by abstract_to_schema when filtering common words.
# Intentionally smaller than _SUBJECT_STOP_WORDS — do NOT merge them.
_SCHEMA_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "was",
        "were",
        "are",
        "has",
        "had",
        "have",
        "been",
        "will",
        "would",
        "could",
        "should",
        "can",
        "may",
        "might",
        "its",
        "but",
        "not",
        "all",
        "any",
        "each",
        "also",
        "into",
        "than",
        "then",
        "when",
        "which",
        "who",
        "how",
        "what",
        "where",
        "there",
        "here",
        "does",
    }
)

# Punctuation characters stripped from tokens during word-frequency analysis.
_PUNCT_STRIP = ".,;:!?()[]{}\"'`"

# Unicode-aware token: any run of 2+ Unicode letters (covers ASCII, Cyrillic,
# Arabic, CJK, Greek, etc.).  [^\W\d_] means: not (non-word | digit | underscore)
# i.e. any Unicode letter character.
_MEANINGFUL_TOKEN_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _has_meaningful_token(text: str) -> bool:
    """Return True when *text* contains at least one meaningful token.

    Unicode-aware: matches any 2+ consecutive Unicode letter characters
    (ASCII identifiers, Cyrillic, Arabic, CJK, Greek, Japanese, etc.)
    that is not a pure stop-word.

    Accepts when text is at least 20 chars long AND at least one token is
    a Unicode letter run of >=2 chars that is NOT in _SUBJECT_STOP_WORDS.
    """
    if len(text) < 20:
        return False
    tokens = _MEANINGFUL_TOKEN_RE.findall(text.lower())
    return any(t not in _SUBJECT_STOP_WORDS for t in tokens)


# Backward-compat alias — existing code and tests that import _has_ascii_identifier_token
# still work; the implementation is now Unicode-aware.
_has_ascii_identifier_token = _has_meaningful_token


def _is_degenerate_auto_abstracted(content: str) -> bool:
    """Return True when *content* is a degenerate auto-abstracted memory.

    Two conditions trigger degenerate detection (OR):
    1. Body (after stripping the Recurring prefix and [tags:…] suffix) exactly
       matches the known-bad "_memify_derive cluster" pattern.
    2. Body fails _has_meaningful_token (Unicode-aware) AND is at least 20 chars.
       ONLY fires when the Recurring prefix was actually present.

    Conservative: only deletes when we are highly confident the content is noise.
    """
    content = content[:4096]  # cap to bound regex backtracking
    body, n_prefix = _RECURRING_PREFIX_RE.subn("", content)
    body = _TAGS_SUFFIX_RE.sub("", body).strip()

    # Condition 1: exact match of the canonical degenerate body
    if _DEGENERATE_BODY_RE.match(body):
        return True

    # Condition 2: only fires when the Recurring-pattern prefix was present
    if n_prefix > 0 and len(body) >= 20 and not _has_meaningful_token(body):
        return True

    return False


# Specific-content indicators (file paths, line numbers, error traces)
_SPECIFIC_INDICATORS = re.compile(
    r"(?:"
    r"(?:\.{0,2}/)?(?:[\w@.-]+/)+[\w@.-]+\.\w+"  # file paths
    r"|line \d+"  # line numbers
    r"|Traceback \(most recent call last\)"  # tracebacks
    r"|(?:Error|Exception):\s"  # error messages
    r"|0x[0-9a-fA-F]+"  # memory addresses
    r")"
)


def _collect_word_freq(
    cluster_memories: list[dict],
) -> tuple[dict[str, int], list[str]]:
    """Return (word_freq, all_contents) from *cluster_memories*.

    word_freq maps each cleaned token (len > 2, punctuation stripped) to the
    number of distinct memories it appears in.  all_contents preserves order.
    """
    word_freq: dict[str, int] = defaultdict(int)
    all_contents: list[str] = []
    for mem in cluster_memories:
        content = mem.get("content", "")
        all_contents.append(content)
        for w in set(content.lower().split()):
            clean = w.strip(_PUNCT_STRIP)
            if len(clean) > 2:
                word_freq[clean] += 1
    return word_freq, all_contents


def _build_ordered_common(first_content: str, meaningful: set[str]) -> list[str]:
    """Return *meaningful* words ordered by first appearance in *first_content*.

    Words absent from *first_content* are appended in arbitrary order.
    """
    ordered: list[str] = []
    for word in first_content.lower().split():
        clean = word.strip(_PUNCT_STRIP)
        if clean in meaningful and clean not in ordered:
            ordered.append(clean)
    for word in meaningful:
        if word not in ordered:
            ordered.append(word)
    return ordered


def _collect_common_tags(cluster_memories: list[dict], threshold: float) -> list[str]:
    """Return tags that appear in at least *threshold* memories."""
    all_tags: dict[str, int] = defaultdict(int)
    for mem in cluster_memories:
        for tag in mem.get("tags", []):
            if isinstance(tag, str):
                all_tags[tag] += 1
    return [t for t, c in all_tags.items() if c >= threshold]


class _PatternsMixin:
    """Mixin: classify_memory, abstract_to_schema, check_consistency."""

    # ── Classification ────────────────────────────────────────────────────

    @observe(tier="boundary", name="consolidation.cls.classify_memory")
    def classify_memory(self, content: str, tags: list[str], directory: str) -> str:
        """Classify incoming memory as 'episodic' or 'semantic'.

        Rules for semantic classification:
        - Contains decision keywords (always, never, prefer, standard, convention)
        - Contains architecture keywords (pattern, architecture, design, principle)
        - Tags include semantic indicators (rule, convention, preference, standard)
        - Content describes a general pattern (not specific to one file/line)
        """
        # Check tags first (cheapest)
        tag_set = {t.lower() for t in tags}
        if tag_set & _SEMANTIC_TAGS:
            return "semantic"

        has_decision = bool(_DECISION_KEYWORDS.search(content))
        has_architecture = bool(_ARCHITECTURE_KEYWORDS.search(content))
        has_specific = bool(_SPECIFIC_INDICATORS.search(content))

        # If content has specific indicators (file paths, line numbers, error traces),
        # it's likely a specific instance → episodic, even with keyword matches
        if has_specific:
            return "episodic"

        # General content with decision or architecture keywords → semantic
        if has_decision or has_architecture:
            return "semantic"

        return "episodic"

    # ── Consistency Check ─────────────────────────────────────────────────

    @observe(tier="hot", name="consolidation.cls.check_consistency")
    def check_consistency(self, cluster_memories: list[dict]) -> dict:
        """Verify cluster members don't contradict each other.

        Uses negation-pattern detection similar to curation.detect_contradictions.
        """
        _negation_re = re.compile(
            r"\b(not|don't|doesn't|didn't|won't|can't|cannot|isn't|aren't|"
            r"wasn't|weren't|no longer|instead of|rather than|replaced|"
            r"switched from|stopped using|removed|deprecated|dropped|never)\b",
            re.IGNORECASE,
        )

        contradictions = []
        for i, mem_a in enumerate(cluster_memories):
            content_a = mem_a.get("content", "")
            has_neg_a = bool(_negation_re.search(content_a))

            for mem_b in cluster_memories[i + 1 :]:
                content_b = mem_b.get("content", "")
                has_neg_b = bool(_negation_re.search(content_b))

                # One has negation, the other doesn't → potential contradiction
                if has_neg_a != has_neg_b:
                    contradictions.append(
                        f"Conflict between memory {mem_a.get('id')} and {mem_b.get('id')}: "
                        f"negation mismatch"
                    )

        return {
            "consistent": len(contradictions) == 0,
            "contradictions": contradictions,
        }

    # ── Schema Abstraction ────────────────────────────────────────────────

    @observe(tier="hot", name="consolidation.cls.abstract_to_schema")
    def abstract_to_schema(self, cluster_memories: list[dict]) -> str:
        """Abstract multiple episodic memories into a semantic schema.

        Extracts common words and entities across all memories,
        then builds a generalized statement.
        """
        if not cluster_memories:
            return ""

        word_freq, all_contents = _collect_word_freq(cluster_memories)
        n_memories = len(cluster_memories)

        # Words that appear in the majority of memories (>= 50%)
        common_threshold = max(n_memories / 2, 2)
        common_words = {w for w, count in word_freq.items() if count >= common_threshold}
        meaningful = common_words - _SCHEMA_STOP_WORDS

        if not meaningful:
            # Fallback: use the shortest memory as representative
            shortest = min(all_contents, key=len)
            return f"Recurring pattern: {shortest}"

        ordered_common = _build_ordered_common(all_contents[0], meaningful)
        key_phrase = " ".join(ordered_common[:15])  # Cap at 15 words

        common_tags = _collect_common_tags(cluster_memories, common_threshold)
        schema = f"Recurring pattern across {n_memories} observations: {key_phrase}"
        if common_tags:
            schema += f" [tags: {', '.join(common_tags[:5])}]"

        return schema
