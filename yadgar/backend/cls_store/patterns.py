"""Pattern classification, schema abstraction, and degenerate-detection helpers."""

import re
from collections import defaultdict

from yadgar._shared.observability.observe import observe

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


# ── C4.3 / S1: thin (meta-token-dense) auto-abstracted guard ─────────────────
#
# Yadgar-internal plumbing tokens that carry NO project-code signal. An
# auto-abstracted schema dominated by these (with too few distinct REAL domain
# tokens left over) is noise: it wins meta-queries about the memory system
# itself by construction (ADR-0142 concern-2, H2-corpus). These are the graph /
# entity / consolidation-namespace words, distinct from _SUBJECT_STOP_WORDS
# (which are ordinary English function words).
_META_PLUMBING_TOKENS = frozenset(
    {
        "entity",
        "entities",
        "edge",
        "edges",
        "graph",
        "node",
        "nodes",
        "viz",
        "connection",
        "connections",
        "derived",
        "co_occurrence",
        "cooccurrence",
        "occurrence",
        "weight",
        "dead",
        "prior",
        "cofire",
        "spreading",
        "cluster",
        "clusters",
        "via",
        "earlier",
        "later",
        "observation",
    }
)

# Namespace / synthetic tokens that are always meta regardless of surrounding
# text: entity:4551, memory:12, 0-edge, derived_from, an ISO date, a bare number.
_META_NAMESPACE_RE = re.compile(
    r"^(?:"
    r"[a-z]+:\w+"  # entity:4551, memory:12
    r"|\d+-\w+"  # 0-edge, 3-node
    r"|derived_from"  # relationship-type token
    r"|\d{4}-\d{2}-\d{2}"  # ISO date
    r"|\d[\d.]*"  # bare numbers (500, 4551)
    r")$",
    re.IGNORECASE,
)

# Minimum distinct REAL domain tokens an auto-abstracted schema must carry to be
# worth promoting. Below this it is a meta-token bag. Chosen so a 3-token real
# abstraction (e.g. "jwt auth middleware") survives while an entity/graph
# plumbing bag does not. Mutation-tested (C4.3 hardening).
_THIN_MIN_REAL_TOKENS = 3


def _distinct_real_tokens(body: str) -> set[str]:
    """Return the distinct real domain tokens in *body*.

    Real = a meaningful (Unicode-letter) token that is NOT an ordinary
    stop-word, NOT yadgar meta-plumbing, and NOT a namespace/synthetic token.
    """
    real: set[str] = set()
    for raw in body.lower().split():
        tok = raw.strip(_PUNCT_STRIP)
        if not tok:
            continue
        if _META_NAMESPACE_RE.match(tok):
            continue
        if tok in _SUBJECT_STOP_WORDS or tok in _META_PLUMBING_TOKENS:
            continue
        # Must contain a Unicode-letter run of >=2 (drops "500", punctuation).
        if not _MEANINGFUL_TOKEN_RE.search(tok):
            continue
        real.add(tok)
    return real


def _is_thin_auto_abstracted(content: str) -> bool:
    """Return True when *content* is a thin (meta-token-dense) auto-abstracted schema.

    Fires ONLY when the "Recurring pattern[...]:" prefix is present (i.e. the
    content is an auto-abstracted schema, not arbitrary user text). Thin means:
    after stripping the prefix + [tags:...] suffix and removing stop-words,
    yadgar meta-plumbing tokens (entity:/graph/derived_from/co_occurrence/…) and
    namespace/synthetic tokens, FEWER than ``_THIN_MIN_REAL_TOKENS`` distinct
    real domain tokens remain.

    This targets meta-token DENSITY (ADR-0142 concern-2 H2-corpus), NOT verbosity
    — a long topical schema with real anchors (jwt, docker, longmemeval, dataset)
    keeps enough distinct real tokens to pass. It is deliberately conservative:
    an over-broad guard would suppress genuinely useful abstractions, a worse
    outcome than the thin noise it removes (plan §5 gate G2, corpus edition).
    """
    content = content[:4096]  # cap to bound regex backtracking
    body, n_prefix = _RECURRING_PREFIX_RE.subn("", content)
    if n_prefix == 0:
        # Not an auto-abstracted schema — the thin gate does not apply.
        return False
    body = _TAGS_SUFFIX_RE.sub("", body).strip()
    return len(_distinct_real_tokens(body)) < _THIN_MIN_REAL_TOKENS


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

# ── C7c (task #339): word-salad gate for auto-abstracted schemas ──────────
#
# A schema like "Recurring pattern across 5 observations: token token token"
# carries NO identifier, ADR, MR, file ref, or shared tag — it is unanchored
# prose and would, on insertion, get auto-promoted to an anchor (memorize
# write pipeline sets is_protected=True + _anchor tag when tier is set). Once
# anchored, it becomes a protected zombie: immune to decay, ranking high on
# recall, the exact phantom-namespace shape §1.4 forbids. The gate below drops
# these BEFORE insert, not after — the cheaper, more reliable fix.
#
# Five signal classes pass the gate; a schema that carries none AND has no
# common_tags AND fails the prose-density floor is a word salad and must be
# discarded by the caller.
_SALAD_BACKTICK_RE = re.compile(r"`[^`]+`")
_SALAD_ADR_MR_ISSUE_RE = re.compile(r"\b(?:ADR|MR)-\d+|\B#\d+\b")
_SALAD_FILE_REF_RE = re.compile(r"\b[\w\-]+\.(?:py|md|yaml|json|toml|sql)\b")
_SALAD_SNAKE_CASE_RE = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
_SALAD_CAMEL_CASE_RE = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b")

# C7c-fix (2026-08-24): the original gate (5 regex classes only) rejected
# legitimate prose consolidations — e.g. 4 paraphrases of "the deployment
# pipeline publishes container images to ECR..." carry real domain tokens
# but no identifier-shape token, so the gate returned None and broke the
# consolidation_cycle contract (BC-CLS2 e2e test). The fix adds a 6th
# signal: a schema long enough to host ≥ _PROSE_MIN_WORDS distinct
# meaningful words (post-stopword) is a legitimate consolidation, not a
# bag-of-words salad. Real word salads (short "pattern across N obs: a b
# c") still fail because their post-stopword token count is below the
# floor.
_PROSE_MIN_LEN = 80  # noqa: PLR2004 — prose-density floor, not a magic # noqa: ERA001
_PROSE_MIN_WORDS = 4  # noqa: PLR2004 — distinct meaningful-token floor # noqa: ERA001


@observe(tier="stage", metric="consolidation.cls._is_word_salad")
def _is_word_salad(schema: str, common_tags: list[str]) -> bool:
    """Return True when *schema* carries no identifier / ADR / file signal.

    The gate is the schema-level half of the C7c anchor defense (task #339):
    a memory whose body is the canonical "Recurring pattern across N
    observations: <bag of words>" template AND has no backtick identifier,
    ADR/MR/issue reference, file ref, snake_case identifier, CamelCase
    identifier, AND does not meet the prose-density floor (length ≥80 +
    ≥4 distinct post-stopword meaningful tokens), AND has no shared tag,
    is a word salad. Promotion must discard it.

    Conservative: any single recognised signal flips the verdict to "not
    salad" — this is the only path the validation runs. C7c-fix (2026-08-24)
    added the prose-density half so legitimate multi-memory prose
    consolidations (TestBCCLS1_2_3 e2e fixture) are no longer dropped.
    """
    if not isinstance(schema, str) or not schema:
        return True
    if common_tags:  # shared tags ARE a real signal even when body is prose
        return False
    if _SALAD_BACKTICK_RE.search(schema):
        return False
    if _SALAD_ADR_MR_ISSUE_RE.search(schema):
        return False
    if _SALAD_FILE_REF_RE.search(schema):
        return False
    if _SALAD_SNAKE_CASE_RE.search(schema):
        return False
    if _SALAD_CAMEL_CASE_RE.search(schema):
        return False
    # Prose-density escape hatch: a schema long enough AND diverse enough
    # in post-stopword tokens is a legitimate consolidation. The two floors
    # are jointly required so a short single-word schema ("Recurring pattern
    # across 4 observations: a") still falls through to the rejection path.
    if len(schema) >= _PROSE_MIN_LEN:
        meaningful_tokens = {
            w.strip(_PUNCT_STRIP)
            for w in schema.lower().split()
            if len(w.strip(_PUNCT_STRIP)) > 2 and w.strip(_PUNCT_STRIP) not in _SCHEMA_STOP_WORDS
        }
        if len(meaningful_tokens) >= _PROSE_MIN_WORDS:
            return False
    return True


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

    @observe(tier="boundary", metric="consolidation.cls.classify_memory")
    def classify_memory(self, content: str, tags: list[str], project_id: str) -> str:
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

    @observe(tier="hot", metric="consolidation.cls.check_consistency")
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

    @observe(tier="hot", metric="consolidation.cls.abstract_to_schema")
    def abstract_to_schema(self, cluster_memories: list[dict]) -> str | None:
        """Abstract multiple episodic memories into a semantic schema.

        Extracts common words and entities across all memories,
        then builds a generalized statement.
        """
        if not cluster_memories:
            return None

        word_freq, all_contents = _collect_word_freq(cluster_memories)
        n_memories = len(cluster_memories)

        # Words that appear in the majority of memories (>= 50%)
        common_threshold = max(n_memories / 2, 2)
        common_words = {w for w, count in word_freq.items() if count >= common_threshold}
        meaningful = common_words - _SCHEMA_STOP_WORDS

        if not meaningful:
            # Fallback: use the shortest memory as representative.
            # C7c (task #339): gate the fallback too — when no meaningful
            # words survive, the shortest-memory string is by construction a
            # bag of stop-words, exactly the salad the gate rejects. Drop it
            # before it can be promoted to an anchor.
            shortest = min(all_contents, key=len)
            fallback = f"Recurring pattern: {shortest}"
            if _is_word_salad(fallback, []):
                return None
            return fallback

        ordered_common = _build_ordered_common(all_contents[0], meaningful)
        key_phrase = " ".join(ordered_common[:15])  # Cap at 15 words

        common_tags = _collect_common_tags(cluster_memories, common_threshold)
        schema = f"Recurring pattern across {n_memories} observations: {key_phrase}"
        if common_tags:
            schema += f" [tags: {', '.join(common_tags[:5])}]"

        # C7c (task #339): drop word-salad schemas BEFORE they can be inserted
        # and auto-promoted to anchors. The promotion path is the cheap one to
        # gate; audit-side filtering is the backstop.
        if _is_word_salad(schema, common_tags):
            return None
        return schema
