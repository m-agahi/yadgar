"""Heuristic reranking mixin — entity/term/bigram/exact-match scoring."""

from __future__ import annotations


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

        # Extract query entities (capitalized words, likely names/places)
        query_entities = set()
        for token in query.split():
            stripped = token.strip(".,;:!?()[]{}\"'`~@#$%^&*-_+=<>/\\|")
            if stripped and stripped[0].isupper() and len(stripped) > 1:
                query_entities.add(stripped.lower())

        # Tokenize query (excluding common question words)
        _question_words = {
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
        query_terms = set()
        query_content_terms = set()  # Terms that carry meaning
        for token in query_lower.split():
            stripped = token.strip(".,;:!?()[]{}\"'`~@#$%^&*-_+=<>/\\|")
            if stripped:
                query_terms.add(stripped)
                if stripped not in _question_words and len(stripped) > 2:
                    query_content_terms.add(stripped)

        # Build bigrams for phrase matching
        q_words = [t.strip(".,;:!?()[]{}\"'`~@#$%^&*-_+=<>/\\|") for t in query_lower.split()]
        q_words = [w for w in q_words if w]
        query_bigrams = set()
        for j in range(len(q_words) - 1):
            query_bigrams.add(f"{q_words[j]} {q_words[j + 1]}")

        if not query_terms:
            return memories[:top_k]

        for mem in memories:
            content = mem.get("content", "")
            content_lower = content.lower()

            content_terms = set()
            for token in content_lower.split():
                stripped = token.strip(".,;:!?()[]{}\"'`~@#$%^&*-_+=<>/\\|")
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
                c_words = [
                    t.strip(".,;:!?()[]{}\"'`~@#$%^&*-_+=<>/\\|") for t in content_lower.split()
                ]
                c_words = [w for w in c_words if w]
                content_bigrams = set()
                for j in range(len(c_words) - 1):
                    content_bigrams.add(f"{c_words[j]} {c_words[j + 1]}")
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

        memories.sort(key=lambda m: m["_retrieval_score"], reverse=True)
        return memories[:top_k]
