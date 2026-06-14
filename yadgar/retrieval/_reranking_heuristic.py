"""Heuristic reranking mixin — entity/term/bigram/exact-match scoring."""

from __future__ import annotations

_PUNCT = ".,;:!?()[]{}\"'`~@#$%^&*-_+=<>/\\|"

_QUESTION_WORDS: frozenset[str] = frozenset(
    {
        "what",
        "who",
        "when",
        "where",
        "why",
        "how",
        "would",
        "could",
        "should",
        "does",
        "did",
        "is",
        "are",
        "was",
        "were",
        "do",
        "has",
        "have",
        "had",
        "can",
        "will",
        "the",
        "a",
        "an",
        "of",
        "in",
        "to",
        "for",
        "and",
        "or",
        "be",
        "if",
        "that",
        "this",
        "it",
        "on",
        "at",
        "by",
        "with",
        "as",
        "not",
        "but",
        "from",
        "likely",
        "still",
        "also",
        "more",
        "most",
        "very",
        "about",
    }
)


def _extract_query_entities(query: str) -> set[str]:
    """Return lowercased capitalized tokens (len>1) from *query* as entity set."""
    entities: set[str] = set()
    for token in query.split():
        stripped = token.strip(_PUNCT)
        if stripped and stripped[0].isupper() and len(stripped) > 1:
            entities.add(stripped.lower())
    return entities


def _extract_query_terms(query_lower: str) -> tuple[set[str], set[str]]:
    """Tokenize *query_lower*, return (all_terms, content_terms).

    Content terms exclude question/stop words and tokens of length ≤2.
    """
    query_terms: set[str] = set()
    query_content_terms: set[str] = set()
    for token in query_lower.split():
        stripped = token.strip(_PUNCT)
        if stripped:
            query_terms.add(stripped)
            if stripped not in _QUESTION_WORDS and len(stripped) > 2:
                query_content_terms.add(stripped)
    return query_terms, query_content_terms


def _build_bigrams(text_lower: str) -> set[str]:
    """Tokenize *text_lower* and return the set of adjacent word bigrams."""
    words = [t.strip(_PUNCT) for t in text_lower.split()]
    words = [w for w in words if w]
    bigrams: set[str] = set()
    for j in range(len(words) - 1):
        bigrams.add(f"{words[j]} {words[j + 1]}")
    return bigrams


def _score_memory(
    mem: dict,
    query_lower: str,
    query_entities: set[str],
    query_content_terms: set[str],
    query_bigrams: set[str],
) -> None:
    """Compute and mutate *mem* in-place with ``_rerank_score`` and updated ``_retrieval_score``."""
    content = mem.get("content", "")
    content_lower = content.lower()

    content_terms: set[str] = set()
    for token in content_lower.split():
        stripped = token.strip(_PUNCT)
        if stripped:
            content_terms.add(stripped)

    # 1. Entity coverage (names, places — most important signal)
    entity_score = 0.0
    if query_entities:
        entity_overlap = query_entities & content_terms
        entity_score = len(entity_overlap) / len(query_entities)

    # 2. Content term coverage (meaningful words only)
    term_score = 0.0
    if query_content_terms:
        content_overlap = query_content_terms & content_terms
        term_score = len(content_overlap) / len(query_content_terms)

    # 3. Bigram overlap (phrase matching)
    bigram_score = 0.0
    if query_bigrams:
        content_bigrams = _build_bigrams(content_lower)
        bigram_overlap = query_bigrams & content_bigrams
        bigram_score = len(bigram_overlap) / len(query_bigrams)

    # 4. Exact substring match
    exact_match = 1.0 if query_lower in content_lower else 0.0

    rerank_score = (
        entity_score * 0.35 + term_score * 0.30 + bigram_score * 0.20 + exact_match * 0.15
    )
    mem["_rerank_score"] = round(rerank_score, 4)

    # Combine: retrieval score (85%) + rerank (15%)
    retrieval_score = mem.get("_retrieval_score", 0.0)
    mem["_retrieval_score"] = round(0.85 * retrieval_score + 0.15 * rerank_score, 4)


class _HeuristicMixin:
    """Provides heuristic_rerank using entity matching, noun overlap, and IDF weighting."""

    def heuristic_rerank(
        self,
        memories: list[dict],
        query: str,
        top_k: int | None = None,
    ) -> list[dict]:
        """Enhanced reranker using entity matching, noun overlap, and IDF weighting.

        Signals:
        1. Entity coverage: capitalized words / proper nouns from query in content
        2. Content term coverage (excluding stop words)
        3. Bigram overlap for phrase matching
        4. Exact substring match bonus
        """
        if top_k is None:
            top_k = self._settings.RERANKER_TOP_K

        if not memories or not query:
            return memories[:top_k] if memories else []

        query_lower = query.lower()

        query_entities = _extract_query_entities(query)
        query_terms, query_content_terms = _extract_query_terms(query_lower)
        query_bigrams = _build_bigrams(query_lower)

        if not query_terms:
            return memories[:top_k]

        for mem in memories:
            _score_memory(mem, query_lower, query_entities, query_content_terms, query_bigrams)

        memories.sort(key=lambda m: m["_retrieval_score"], reverse=True)
        return memories[:top_k]
