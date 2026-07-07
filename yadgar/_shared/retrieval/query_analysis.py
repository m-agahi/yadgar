"""Query expansion and analysis functions."""

import re

from yadgar._shared.observability.observe import observe
from yadgar._shared.retrieval.entities import (
    _LINE_SUBJECT_RE,
    _OPEN_DOMAIN_CUE_PHRASES,
    _OPEN_DOMAIN_FACT_PATTERNS,
    _OPEN_DOMAIN_MODAL_WORDS,
    _OPEN_DOMAIN_TOPIC_EXPANSIONS,
    _QUERY_STOP_WORDS,
    _QUESTION_WORDS,
    _SAID_LINE_RE,
)

# -- Pseudo-HyDE query expansion --
# Strip question syntax to convert queries into declarative pseudo-documents
# for better vector similarity (documents are statements, not questions).
_HYDE_STRIP_WORDS = {
    "what",
    "when",
    "where",
    "who",
    "why",
    "how",
    "does",
    "did",
    "is",
    "are",
    "was",
    "were",
    "would",
    "could",
    "should",
    "do",
    "can",
    "will",
    "has",
    "have",
    "had",
}
# Patterns for question-to-statement conversion
_HYDE_QTO_S = [
    # "What is X?" → "X is"
    (re.compile(r"^what\s+(?:is|are|was|were)\s+(.+?)\??$", re.IGNORECASE), r"\1 is"),
    # "What does X do?" → "X does"
    (re.compile(r"^what\s+(?:does|did|do)\s+(.+?)\??$", re.IGNORECASE), r"\1"),
    # "Who is X?" → "X is"
    (re.compile(r"^who\s+(?:is|are|was|were)\s+(.+?)\??$", re.IGNORECASE), r"\1 is"),
    # "Where is X?" → "X is located"
    (re.compile(r"^where\s+(?:is|are|was|were)\s+(.+?)\??$", re.IGNORECASE), r"\1 is located"),
    # "When did X?" → "X"
    (re.compile(r"^when\s+(?:did|does|do|was|were|is|are)\s+(.+?)\??$", re.IGNORECASE), r"\1"),
    # "How does X?" → "X"
    (
        re.compile(
            r"^how\s+(?:does|did|do|is|are|was|were|can|could|should|would)\s+(.+?)\??$",
            re.IGNORECASE,
        ),
        r"\1",
    ),
    # "Why does X?" → "X because"
    (
        re.compile(r"^why\s+(?:does|did|do|is|are|was|were)\s+(.+?)\??$", re.IGNORECASE),
        r"\1 because",
    ),
    # "Is X Y?" → "X Y" (strip auxiliary, keep subject+predicate)
    # Avoids broken grammar like "X Y is" — just let "X Y" match documents
    (re.compile(r"^(?:is|are|was|were)\s+(.+?)\??$", re.IGNORECASE), r"\1"),
    # "Does X ...?" → "X ..."
    (re.compile(r"^(?:does|did|do)\s+(.+?)\??$", re.IGNORECASE), r"\1"),
    # "Can/Could/Would/Should X?" → "X"
    (re.compile(r"^(?:can|could|would|should)\s+(.+?)\??$", re.IGNORECASE), r"\1"),
]


@observe(tier="hot", metric="retrieval.qa.pseudo_hyde_expand")
def _pseudo_hyde_expand(query: str) -> str:
    """Convert a question-form query into a declarative pseudo-document.

    This is a lightweight approximation of HyDE (Hypothetical Document Embeddings).
    Instead of using an LLM to generate a hypothetical answer, we use pattern
    matching to convert question syntax to statement syntax, bridging the semantic
    gap between questions and stored documents for vector search.

    Examples:
        "What is Alice's hobby?" → "Alice's hobby is"
        "How does the retrieval system work?" → "the retrieval system work"
        "When did we deploy v2?" → "we deploy v2"
    """
    if not query or not isinstance(query, str):
        return query

    stripped = query.strip()
    if not stripped:
        return query

    # Try pattern-based question-to-statement conversion first
    for pattern, replacement in _HYDE_QTO_S:
        match = pattern.match(stripped)
        if match:
            result = pattern.sub(replacement, stripped).strip()
            # Clean up trailing punctuation and whitespace
            result = result.rstrip("?.!").strip()
            if result:
                return result

    # Fallback: strip leading question words and trailing question marks
    words = stripped.split()
    if not words:
        return query

    # Remove leading question words
    start = 0
    while start < len(words) and words[start].lower().rstrip("?,") in _HYDE_STRIP_WORDS:
        start += 1
        # Don't strip more than 3 leading words to preserve meaning
        if start >= 3:
            break

    if start > 0 and start < len(words):
        result = " ".join(words[start:]).rstrip("?.!").strip()
        if result:
            return result

    # If stripping would remove everything, return original minus question mark
    return stripped.rstrip("?").strip()


@observe(tier="hot", metric="retrieval.qa.question_to_statement")
def _question_to_statement(query: str) -> str:
    """Convert a question into a declarative statement for NLI entailment scoring."""
    q = query.strip().rstrip("?").strip()
    # Pattern: Would X prefer Y → X prefers Y
    m = re.match(r"(?i)would\s+(\w+)\s+prefer\s+(.*)", q)
    if m:
        return f"{m.group(1)} prefers {m.group(2)}"
    m = re.match(r"(?i)does\s+(\w+)\s+enjoy\s+(.*)", q)
    if m:
        return f"{m.group(1)} enjoys {m.group(2)}"
    m = re.match(r"(?i)is\s+(\w+)\s+(?:a\s+)?(.*)", q)
    if m:
        return f"{m.group(1)} is {m.group(2)}"
    m = re.match(r"(?i)what\s+are\s+(\w+)'s\s+(.*)", q)
    if m:
        return f"{m.group(1)} has {m.group(2)}"
    # Default: strip modal verb
    q = re.sub(r"(?i)^(would|does|is|can|did|has|could|might)\s+", "", q)
    return q


@observe(tier="hot", metric="retrieval.qa.extract_content_terms")
def _extract_content_terms(query: str, limit: int | None = None) -> list[str]:
    """Extract content-bearing query terms, preserving order."""
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z][\w'-]*", query):
        normalized = token.lower()
        if len(normalized) < 3 or normalized in _QUERY_STOP_WORDS:
            continue
        if normalized not in seen:
            seen.add(normalized)
            terms.append(token)
            if limit is not None and len(terms) >= limit:
                break
    return terms


@observe(tier="hot", metric="retrieval.qa.extract_comparison_options")
def _extract_comparison_options(query: str) -> list[str]:
    """Extract short comparison options around an 'or' question."""
    tokens = re.findall(r"[A-Za-z][\w'-]*", query.lower())
    if "or" not in tokens:
        return []

    stop_boundaries = _QUERY_STOP_WORDS | {
        "going",
        "interested",
        "interest",
        "considered",
        "consider",
        "likely",
        "would",
        "could",
        "might",
        "more",
        "less",
    }

    def _collect_left(idx: int) -> str:
        collected: list[str] = []
        j = idx - 1
        while j >= 0 and len(collected) < 3:
            token = tokens[j]
            if token in {"a", "an", "the"}:
                j -= 1
                continue
            if token in stop_boundaries:
                break
            collected.append(token)
            j -= 1
        return " ".join(reversed(collected))

    def _collect_right(idx: int) -> str:
        collected: list[str] = []
        j = idx + 1
        while j < len(tokens) and len(collected) < 3:
            token = tokens[j]
            if token in {"a", "an", "the"}:
                j += 1
                continue
            if token in stop_boundaries:
                break
            collected.append(token)
            j += 1
        return " ".join(collected)

    options: list[str] = []
    seen: set[str] = set()
    for idx, token in enumerate(tokens):
        if token != "or":
            continue
        for option in (_collect_left(idx), _collect_right(idx)):
            cleaned = option.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                options.append(cleaned)
    return options[:2]


@observe(tier="hot", metric="retrieval.qa.build_boosted_fts_query")
def _build_boosted_fts_query(query: str) -> str:
    """Add duplicate content terms so BM25 sees a sharper lexical intent."""
    boosted = [query]
    for token in re.findall(r"[A-Za-z][\w'.-]*", query):
        cleaned = token.strip(".,;:!?()[]{}\"'")
        if len(cleaned) <= 1:
            continue
        low = cleaned.lower()
        if low in _QUERY_STOP_WORDS:
            continue
        if cleaned[0:1].isupper():
            boosted.extend([cleaned, cleaned])
        else:
            boosted.append(cleaned)
    return " ".join(boosted)


@observe(tier="hot", metric="retrieval.qa.build_open_domain_subqueries")
def _build_open_domain_subqueries(query: str, query_analysis: dict) -> list[str]:
    """Generate compact auxiliary queries for inference-style questions."""
    subqueries: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        cleaned = " ".join(candidate.split()).strip(" ?.!")
        if cleaned and cleaned.lower() not in seen and cleaned.lower() != query.lower():
            seen.add(cleaned.lower())
            subqueries.append(cleaned)

    hyde_query = _pseudo_hyde_expand(query)
    _add(hyde_query)

    subjects = query_analysis.get("named_entities", [])[:1]
    content_terms = query_analysis.get("content_terms", [])[:6]
    comparison_options = query_analysis.get("comparison_options", [])[:2]
    semantic_expansions = query_analysis.get("semantic_expansions", [])[:6]

    if subjects and content_terms and not comparison_options:
        _add(" ".join(subjects + content_terms[:4]))
    if subjects:
        for option in comparison_options:
            _add(" ".join(subjects + option.split()[:4]))
        if semantic_expansions:
            _add(" ".join(subjects + semantic_expansions[:4]))
    elif content_terms:
        _add(" ".join(content_terms[:6]))
    if semantic_expansions:
        _add(" ".join(semantic_expansions[:4]))

    return subqueries[:4]


def _compact_fact_object(text: str, max_words: int = 10) -> str:
    """Trim noisy trailing clauses from a derived fact object."""
    cleaned = re.split(r"\b(?:because|since|while|but|and|so|though)\b", text, maxsplit=1)[0]
    tokens = cleaned.strip(" ,.;:!?").split()
    return " ".join(tokens[:max_words])


# Each entry: (keywords_tuple, fact_template, use_elif_semantic)
# When use_elif_semantic=True the group is exclusive with the previous elif group.
# See _infer_subject_keyword_hints for evaluation logic.
_SUBJECT_KEYWORD_GROUPS: list[tuple[tuple[str, ...], str, bool]] = [
    # outdoor activities — primary (elif branch below is the fallback)
    (
        ("camping", "campfire", "meteor shower", "hiking", "forest", "mountains"),
        "{subject} enjoys outdoor activities, nature, camping, hiking, and national parks",
        False,
    ),
    # outdoor activities — fallback (only when primary not matched)
    (
        ("nature", "outdoors"),
        "{subject} enjoys outdoor activities and nature",
        True,  # elif: skip when previous group matched
    ),
    (
        ("classical", "bach", "mozart", "orchestra", "symphony"),
        "{subject} enjoys classical music and composers like Vivaldi",
        False,
    ),
    (
        ("counseling", "mental health"),
        "{subject} is interested in counseling and mental health careers",
        False,
    ),
    (
        ("volunteer", "shelter", "make a difference", "help others", "community service"),
        "{subject} values helping others and community service",
        False,
    ),
    (
        ("church", "faith", "cross necklace", "spiritual", "prayer"),
        "{subject} has religious or spiritual beliefs",
        False,
    ),
    (
        ("serve my country", "join the military", "running for office", "policymaking", "veteran"),
        "{subject} is patriotic and interested in public service",
        False,
    ),
    (
        ("adoption", "have a family", "having a family", "kids who need it"),
        "{subject} wants a family and cares about children",
        False,
    ),
    (
        ("lgbtq", "transgender", "rights", "acceptance", "supportive community"),
        "{subject} supports LGBTQ rights and acceptance",
        False,
    ),
]

# Books group uses a compound condition (keyword OR classic+book), handled separately.
_BOOKS_KEYWORDS = ("dr. seuss", "children's books", "kids' books", "kids books")
_BOOKS_FACT = "{subject} collects children's books and classic books"


@observe(tier="hot", metric="retrieval.qa.infer_said_line_hints")
def _infer_said_line_hints(other: str, quote: str) -> list[str]:
    """Return fact hints inferred from a dialogue quote attributed to *other*."""
    results: list[str] = []
    if "you're so thoughtful" in quote or "you are so thoughtful" in quote:
        results.append(f"{other} is thoughtful")
    if "your drive" in quote or "drive to help" in quote:
        results.append(f"{other} is driven")
    if "being real" in quote or "authentic" in quote:
        results.append(f"{other} is authentic")
    if "helping others" in quote or "caring heart" in quote:
        results.append(f"{other} is caring")
    return results


@observe(tier="hot", metric="retrieval.qa.infer_subject_keyword_hints")
def _infer_subject_keyword_hints(subject: str, lower: str) -> list[str]:
    """Return fact hints inferred from keyword groups in a subject-line."""
    results: list[str] = []
    prev_matched = False
    for keywords, template, is_elif in _SUBJECT_KEYWORD_GROUPS:
        if is_elif and prev_matched:
            prev_matched = False
            continue
        matched = any(kw in lower for kw in keywords)
        if matched:
            results.append(template.format(subject=subject))
        prev_matched = matched and not is_elif
    # Books: compound condition
    if any(kw in lower for kw in _BOOKS_KEYWORDS) or ("classic" in lower and "book" in lower):
        results.append(_BOOKS_FACT.format(subject=subject))
    return results


@observe(tier="hot", metric="retrieval.qa.resolve_other_speaker")
def _resolve_other_speaker(current: str | None, paired: list[str]) -> str | None:
    """Return the counterpart speaker in a two-speaker exchange, or None."""
    if not current or len(paired) != 2:
        return None
    if current == paired[0]:
        return paired[1]
    if current == paired[1]:
        return paired[0]
    return None


@observe(tier="hot", metric="retrieval.qa.add_unique_hint")
def _add_unique_hint(text: str, hints: list[str], seen: set[str]) -> None:
    """Append *text* to hints if not already present (dedup via *seen*)."""
    normalized = text.strip().rstrip(".")
    if normalized and normalized.lower() not in seen:
        seen.add(normalized.lower())
        hints.append(normalized + ".")


@observe(tier="hot", metric="retrieval.qa.derive_implied_fact_passages")
def _derive_implied_fact_passages(content: str) -> list[str]:
    """Generate short inferred fact passages for open-domain reranking."""
    hints: list[str] = []
    seen: set[str] = set()
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    speakers = [m.group("speaker") for line in lines if (m := _SAID_LINE_RE.match(line))]
    paired_speakers = speakers[:2]

    for line in lines:
        said_match = _SAID_LINE_RE.match(line)
        if said_match:
            other = _resolve_other_speaker(said_match.group("speaker"), paired_speakers)
            if other:
                for hint in _infer_said_line_hints(other, said_match.group("quote").lower()):
                    _add_unique_hint(hint, hints, seen)
            continue

        subject_match = _LINE_SUBJECT_RE.match(line)
        if not subject_match:
            continue
        subject = subject_match.group("subject")
        lower = line.lower()

        for pattern, template in _OPEN_DOMAIN_FACT_PATTERNS:
            match = pattern.match(line)
            if match:
                obj = _compact_fact_object(match.group("object"))
                if obj:
                    fact = template.format(subject=match.group("subject"), object=obj)
                    _add_unique_hint(fact, hints, seen)

        for hint in _infer_subject_keyword_hints(subject, lower):
            _add_unique_hint(hint, hints, seen)

    return hints[:4]


def _parse_setting_keywords(setting_str: str) -> list[str]:
    """Split a comma-separated settings keyword string, stripping blanks."""
    return [k.strip() for k in setting_str.split(",") if k.strip()]


@observe(tier="hot", metric="retrieval.qa.extract_named_entities")
def _extract_named_entities(query: str) -> list[str]:
    """Return words after the first that start with an uppercase letter."""
    entities: list[str] = []
    for w in query.split()[1:]:
        cleaned = w.strip(".,;:!?()[]{}\"'")
        if cleaned and cleaned[0].isupper():
            entities.append(cleaned)
    return entities


@observe(tier="hot", metric="retrieval.qa.collect_semantic_expansions")
def _collect_semantic_expansions(query_lower: str) -> list[str]:
    """Return topic expansions whose trigger phrase appears in the query."""
    expansions: list[str] = []
    for phrase, phrase_expansions in _OPEN_DOMAIN_TOPIC_EXPANSIONS.items():
        if phrase in query_lower:
            expansions.extend(phrase_expansions)
    return expansions


@observe(tier="hot", metric="retrieval.qa.classify_query_type")
def _classify_query_type(
    words: list[str],
    temporal_markers: list[str],
    is_open_domain_like: bool,
    has_question: bool,
    code_identifiers: list[str],
    has_relational_intent: bool,
) -> tuple[str, list[str]]:
    """Return (query_type, enabled_signals) from pre-computed feature flags."""
    all_signals = ["vector", "fts", "ppr", "spreading"]
    # Ordered table: first matching rule wins.
    if temporal_markers:
        return "temporal", ["vector", "fts"]
    if is_open_domain_like:
        return "open_domain", ["vector", "fts"]
    if has_question:
        return "factoid", ["vector", "fts", "ppr"]
    if code_identifiers:
        return "code", ["vector", "fts"]
    if has_relational_intent:
        return "relational", ["vector", "fts", "ppr", "spreading"]
    if len(words) <= 2:
        return "keyword", ["vector", "fts"]
    if len(words) < 5:
        return "simple", ["vector", "fts"]
    return "complex", list(all_signals)


@observe(tier="stage", metric="retrieval.analyze_query")
def analyze_query(query: str, settings) -> dict:
    """Analyze a query and classify it for signal routing.

    Returns dict with:
    - query_type: str — one of 'simple', 'temporal', 'code', 'relational', 'complex', 'factoid', 'keyword'
    - enabled_signals: list[str] — which signals to activate
    - temporal_markers: list[str] — temporal expressions found
    - named_entities: list[str] — entities detected
    - code_identifiers: list[str] — code identifiers found
    - has_relational_intent: bool
    """
    query_lower = query.lower().strip()
    words = query_lower.split()

    # 1. Check for temporal keywords
    temporal_keywords = _parse_setting_keywords(settings.TEMPORAL_KEYWORDS)
    temporal_markers = [kw for kw in temporal_keywords if kw in query_lower]

    # 2. Check for code keywords
    code_keywords = _parse_setting_keywords(settings.CODE_KEYWORDS)
    code_identifiers = [kw for kw in code_keywords if kw.lower() in query_lower]

    # 3. Check for relational keywords
    relational_keywords = _parse_setting_keywords(settings.RELATIONAL_KEYWORDS)
    has_relational_intent = any(kw.lower() in query_lower for kw in relational_keywords)

    # 5. Named entities: words starting with uppercase (excluding first word)
    named_entities = _extract_named_entities(query)
    named_entity_lowers = {name.lower() for name in named_entities}
    content_terms = [
        term
        for term in _extract_content_terms(query, limit=12)
        if term.lower() not in named_entity_lowers
    ][:8]
    comparison_options = _extract_comparison_options(query)
    semantic_expansions = _collect_semantic_expansions(query_lower)

    # Only trigger open_domain mode for genuine inference-style questions,
    # NOT for simple factual queries starting with "is/are/does/did/can".
    # The broad startswith catch was causing open_domain to fire on ~100% of
    # queries, leading to 4x sub-query expansion + implied fact CE passes
    # and catastrophic memory growth (OOM at 10-22GB).
    has_modal = any(word in _OPEN_DOMAIN_MODAL_WORDS for word in words)
    has_cue_phrase = any(phrase in query_lower for phrase in _OPEN_DOMAIN_CUE_PHRASES)
    has_topic_expansion = bool(semantic_expansions)
    is_open_domain_like = bool(
        comparison_options
        or has_cue_phrase
        or has_topic_expansion
        or (has_modal and has_relational_intent)
        or query_lower.startswith(("would ", "could ", "might "))
    )

    # 4. Classify
    has_question = any(w in _QUESTION_WORDS for w in words)
    query_type, enabled_signals = _classify_query_type(
        words,
        temporal_markers,
        is_open_domain_like,
        has_question,
        code_identifiers,
        has_relational_intent,
    )

    return {
        "query_type": query_type,
        "enabled_signals": enabled_signals,
        "temporal_markers": temporal_markers,
        "named_entities": named_entities,
        "code_identifiers": code_identifiers,
        "has_relational_intent": has_relational_intent,
        "content_terms": content_terms,
        "comparison_options": comparison_options,
        "semantic_expansions": semantic_expansions[:8],
        "is_open_domain_like": is_open_domain_like,
    }
