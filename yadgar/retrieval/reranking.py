"""Reranker: cross-encoder, NLI, heuristic, MMR, and multi-passage reranking."""

import gc
import logging
import os
import time
from collections import defaultdict

from yadgar.retrieval.query_analysis import (
    _derive_implied_fact_passages,
    _question_to_statement,
    analyze_query,
)
from yadgar.storage import _FTS_STOP_WORDS

logger = logging.getLogger(__name__)

try:
    import sentence_transformers  # noqa: F401
except ImportError:
    logger.warning("Reranker disabled: install yadgar[ml] to enable")


class Reranker:
    """Holds all reranker state and methods, extracted from Retriever.

    Owns lazy-loaded model handles (_gte_reranker, _nli_model, etc.) so
    that Retriever stays focused on signal orchestration.
    """

    def __init__(self, settings, storage) -> None:
        self._settings = settings
        self._storage = storage
        self._gte_reranker = None  # Lazy-loaded GTE-Reranker
        self._nli_model = None  # Lazy-loaded NLI model
        self._flashrank_ranker = None  # Lazy-loaded FlashRank ranker
        self._cross_encoder = None  # Lazy-loaded sentence-transformers CrossEncoder
        self._last_reranker_used: float = 0.0  # monotonic timestamp of last reranker call

    def unload_if_idle(self, idle_seconds: float = 600.0) -> None:
        """Unload all reranker models if unused for `idle_seconds`. Frees ~500MB RSS."""
        if self._last_reranker_used == 0.0:
            return  # Never used — nothing to unload
        if time.monotonic() - self._last_reranker_used < idle_seconds:
            return
        unloaded = []
        if self._gte_reranker not in (None, False):
            self._gte_reranker = None
            unloaded.append("GTE-Reranker")
        if self._nli_model not in (None, False):
            self._nli_model = None
            unloaded.append("NLI")
        if self._cross_encoder is not None:
            self._cross_encoder = None
            unloaded.append("FlashRank-CE")
        if unloaded:
            gc.collect()
            logger.info("Idle reranker unload (%.0fs idle): %s", idle_seconds, ", ".join(unloaded))

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

    def cross_encoder_rerank(
        self,
        memories: list[dict],
        query: str,
        top_k: int | None = None,
    ) -> list[dict]:
        """Rerank memories using GTE-Reranker or FlashRank cross-encoder.

        Tries GTE-Reranker-ModernBERT first (better zero-shot OOD generalization),
        falls back to FlashRank (ONNX, faster on CPU), then sentence-transformers.
        """
        self._last_reranker_used = time.monotonic()
        if top_k is None:
            top_k = self._settings.CROSS_ENCODER_TOP_K

        if not memories or not query:
            return memories[:top_k] if memories else []

        query_analysis = analyze_query(query, self._settings)
        open_domain_mode = query_analysis.get("is_open_domain_like", False)

        # Try GTE-Reranker first (better zero-shot OOD generalization)
        gte_failed = False
        if getattr(self._settings, "GTE_RERANKER_ENABLED", False):
            try:
                if self._gte_reranker is None:
                    from sentence_transformers import CrossEncoder as STCrossEncoder

                    self._gte_reranker = STCrossEncoder(
                        self._settings.GTE_RERANKER_MODEL,
                        max_length=self._settings.GTE_RERANKER_MAX_LENGTH,
                    )
                    logger.info("Loaded GTE-Reranker: %s", self._settings.GTE_RERANKER_MODEL)

                if self._gte_reranker is not False:
                    pairs = [(query, m.get("content", "")[:512]) for m in memories]
                    scores = self._gte_reranker.predict(pairs)

                    raw_scores = [float(s) for s in scores]
                    max_score = max(raw_scores)
                    min_score = min(raw_scores)
                    score_range = max_score - min_score

                    ce_weight = getattr(self._settings, "CROSS_ENCODER_WEIGHT", 0.6)
                    ret_weight = 1.0 - ce_weight
                    for i, mem in enumerate(memories):
                        ce_norm = (
                            (raw_scores[i] - min_score) / score_range if score_range > 0 else 0.5
                        )

                        content = mem.get("content", "")
                        content_len = len(content)
                        if content_len < 80:
                            ce_norm *= 0.5
                        elif content_len < 150:
                            ce_norm *= 0.8

                        retrieval_score = mem.get("_retrieval_score", 0.0)
                        mem["_cross_encoder_score"] = round(ce_norm, 4)
                        mem["_retrieval_score"] = round(
                            ret_weight * retrieval_score + ce_weight * ce_norm, 4
                        )

                    memories.sort(key=lambda m: m["_retrieval_score"], reverse=True)
                    return memories[:top_k]
            except Exception as e:
                logger.warning("GTE-Reranker failed, falling back: %s", e)
                self._gte_reranker = False  # Prevent retry
                gte_failed = True

        # If GTE was enabled but failed, respect fallback setting
        if gte_failed and not getattr(self._settings, "GTE_RERANKER_FALLBACK_TO_FLASHRANK", True):
            return memories[:top_k]

        # Try FlashRank (ONNX cross-encoder fallback)
        try:
            from flashrank import Ranker, RerankRequest

            if self._flashrank_ranker is None:
                self._flashrank_ranker = Ranker(
                    model_name="ms-marco-MiniLM-L-12-v2",
                    cache_dir=os.path.expanduser("~/.cache/flashrank"),
                )

            passages = []
            variant_to_memory: dict[int, int] = {}
            for i, mem in enumerate(memories):
                base_text = mem.get("content", "")
                passages.append({"id": len(passages), "text": base_text})
                variant_to_memory[len(passages) - 1] = i

                if open_domain_mode:
                    implied_facts = _derive_implied_fact_passages(base_text)
                    if implied_facts:
                        passages.append({"id": len(passages), "text": " ".join(implied_facts)})
                        variant_to_memory[len(passages) - 1] = i

            rerank_req = RerankRequest(query=query, passages=passages)
            results = self._flashrank_ranker.rerank(rerank_req)

            # Map flashrank scores back to memories
            memory_raw_scores: dict[int, float] = defaultdict(float)
            for result in results:
                mem_idx = variant_to_memory.get(result["id"])
                if mem_idx is not None:
                    memory_raw_scores[mem_idx] = max(
                        memory_raw_scores.get(mem_idx, float("-inf")),
                        result["score"],
                    )

            raw_scores = list(memory_raw_scores.values())
            max_score = max(raw_scores) if raw_scores else 1.0
            min_score = min(raw_scores) if raw_scores else 0.0
            score_range = max_score - min_score

            ce_weight = getattr(self._settings, "CROSS_ENCODER_WEIGHT", 0.6)
            ret_weight = 1.0 - ce_weight
            for i, mem in enumerate(memories):
                raw = memory_raw_scores.get(i, 0.0)
                ce_norm = (raw - min_score) / score_range if score_range > 0 else 0.5

                # Penalize short/generic passages that CE over-scores.
                # Short passages (<80 chars) get filler chat messages like
                # "Sounds great!" that CE erroneously ranks highly.
                content = mem.get("content", "")
                content_len = len(content)
                if content_len < 80:
                    ce_norm *= 0.5  # heavy penalty for very short
                elif content_len < 150:
                    ce_norm *= 0.8  # mild penalty

                retrieval_score = mem.get("_retrieval_score", 0.0)
                mem["_cross_encoder_score"] = round(ce_norm, 4)
                mem["_retrieval_score"] = round(
                    ret_weight * retrieval_score + ce_weight * ce_norm, 4
                )

            memories.sort(key=lambda m: m["_retrieval_score"], reverse=True)
            return memories[:top_k]

        except ImportError:
            pass
        except Exception:
            logger.debug("FlashRank reranking failed, trying sentence-transformers")

        # Fallback: sentence-transformers CrossEncoder
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            logger.warning("No reranker available; skipping cross-encoder reranking")
            return memories

        if self._cross_encoder is None:
            try:
                self._cross_encoder = CrossEncoder(self._settings.CROSS_ENCODER_MODEL)
            except Exception:
                self._cross_encoder = None
                return memories

        pairs = [(query, mem.get("content", "")) for mem in memories]
        try:
            ce_scores = self._cross_encoder.predict(pairs, show_progress_bar=False)
        except Exception:
            return memories

        min_ce = float(min(ce_scores))
        max_ce = float(max(ce_scores))
        ce_range = max_ce - min_ce
        if ce_range > 0:
            normalized_ce = [(float(s) - min_ce) / ce_range for s in ce_scores]
        else:
            normalized_ce = [1.0] * len(ce_scores)

        ce_weight = getattr(self._settings, "CROSS_ENCODER_WEIGHT", 0.6)
        ret_weight = 1.0 - ce_weight
        for mem, ce_norm in zip(memories, normalized_ce, strict=False):
            retrieval_score = mem.get("_retrieval_score", 0.0)
            mem["_cross_encoder_score"] = round(ce_norm, 4)
            mem["_retrieval_score"] = round(ret_weight * retrieval_score + ce_weight * ce_norm, 4)

        memories.sort(key=lambda m: m["_retrieval_score"], reverse=True)
        return memories[:top_k]

    def nli_rerank(self, query: str, memories: list[dict]) -> list[dict]:
        """Score memories by NLI entailment probability."""
        if not getattr(self._settings, "NLI_RERANKING_ENABLED", False):
            return memories

        try:
            if self._nli_model is None:
                from sentence_transformers import CrossEncoder

                self._nli_model = CrossEncoder(self._settings.NLI_MODEL)
                logger.info("Loaded NLI model: %s", self._settings.NLI_MODEL)

            hypothesis = _question_to_statement(query)
            pairs = [(m["content"][:512], hypothesis) for m in memories]
            scores = self._nli_model.predict(
                pairs
            )  # Shape: (n, 3) for [contradiction, neutral, entailment]

            for i, mem in enumerate(memories):
                if hasattr(scores[i], "__len__") and len(scores[i]) == 3:
                    # Softmax to get probabilities
                    import numpy as np

                    exp_scores = np.exp(scores[i] - np.max(scores[i]))
                    probs = exp_scores / exp_scores.sum()
                    mem["_nli_entailment_score"] = float(probs[2])  # Index 2 = entailment
                else:
                    mem["_nli_entailment_score"] = float(scores[i])

        except Exception as e:
            logger.warning("NLI reranking failed: %s", e)
            self._nli_model = False
            for mem in memories:
                mem["_nli_entailment_score"] = 0.0

        return memories

    def cluster_memories(self, memories: list[dict]) -> list[list[dict]]:
        """Cluster memories by entity/topic overlap using Jaccard similarity."""
        threshold = getattr(self._settings, "MULTI_PASSAGE_CLUSTER_OVERLAP_THRESHOLD", 0.3)
        max_size = getattr(self._settings, "MULTI_PASSAGE_MAX_CLUSTER_SIZE", 3)

        # Tokenize each memory
        tokenized = []
        for m in memories:
            tokens = set(m.get("content", "").lower().split())
            tokens -= _FTS_STOP_WORDS
            tokenized.append(tokens)

        clusters = []
        used = set()

        for i, m in enumerate(memories):
            if i in used:
                continue
            cluster = [m]
            used.add(i)
            for j in range(i + 1, len(memories)):
                if j in used or len(cluster) >= max_size:
                    break
                # Jaccard similarity
                intersection = len(tokenized[i] & tokenized[j])
                union = len(tokenized[i] | tokenized[j])
                if union > 0 and intersection / union >= threshold:
                    cluster.append(memories[j])
                    used.add(j)
            clusters.append(cluster)

        return clusters

    def multi_passage_rerank(self, query: str, memories: list[dict], top_k: int) -> list[dict]:
        """Multi-passage evidence aggregation reranking.

        Groups related memories and re-scores clusters to detect when multiple
        weak pieces of evidence combine into strong evidence.
        """
        if not getattr(self._settings, "MULTI_PASSAGE_RERANKING_ENABLED", False):
            return memories[:top_k]

        # Cluster top-20 candidates
        clusters = self.cluster_memories(memories[:20])

        for cluster_mems in clusters:
            if len(cluster_mems) < 2:
                continue
            # Concatenate cluster texts
            combined = " | ".join(m.get("content", "")[:200] for m in cluster_mems[:3])

            # Score combined text using CE
            combined_score = self.score_single_pair(query, combined)

            # If combined evidence is stronger, boost individual members
            max_individual = max(
                m.get("_cross_encoder_score", m.get("_retrieval_score", 0)) for m in cluster_mems
            )
            if combined_score > max_individual:
                boost = (combined_score - max_individual) * 0.5
                for m in cluster_mems:
                    m["_retrieval_score"] = m.get("_retrieval_score", 0) + boost

        memories.sort(key=lambda m: m.get("_retrieval_score", 0), reverse=True)
        return memories[:top_k]

    def score_single_pair(self, query: str, document: str) -> float:
        """Score a single query-document pair using the active CE model."""
        try:
            if self._gte_reranker and self._gte_reranker is not False:
                scores = self._gte_reranker.predict([(query, document[:512])])
                return float(scores[0]) if hasattr(scores, "__len__") else float(scores)
            # Fallback to FlashRank
            if self._flashrank_ranker:
                from flashrank import RerankRequest

                req = RerankRequest(query=query, passages=[{"text": document[:512]}])
                result = self._flashrank_ranker.rerank(req)
                return result[0]["score"] if result else 0.0
        except Exception:
            pass
        return 0.0

    def mmr_rerank(
        self,
        memories: list[dict],
        query_embedding: bytes | None,
        top_k: int = 5,
        lambda_param: float = 0.7,
    ) -> list[dict]:
        """Maximal Marginal Relevance for diversity-aware reranking.

        Balances relevance (lambda) vs diversity (1-lambda) to avoid
        returning top-K results that are all from the same conversation segment.
        """
        if not memories or len(memories) <= 1 or query_embedding is None:
            return memories[:top_k]

        import numpy as np

        q_arr = np.frombuffer(query_embedding, dtype=np.float32)

        # Get embeddings for all candidate memories
        mem_embeddings = []
        valid_memories = []
        for mem in memories:
            full_mem = self._storage.get_memory(mem["id"])
            if full_mem and full_mem.get("embedding"):
                emb = np.frombuffer(full_mem["embedding"], dtype=np.float32)
                # Handle dimension mismatch
                if len(emb) == len(q_arr):
                    mem_embeddings.append(emb)
                    valid_memories.append(mem)
            else:
                valid_memories.append(mem)
                mem_embeddings.append(None)

        if not valid_memories:
            return memories[:top_k]

        def cosine_sim(a, b):
            if a is None or b is None:
                return 0.0
            dot = np.dot(a, b)
            norm = np.linalg.norm(a) * np.linalg.norm(b)
            return float(dot / norm) if norm > 0 else 0.0

        selected = []
        selected_embs = []
        candidates = list(range(len(valid_memories)))

        for _ in range(min(top_k, len(valid_memories))):
            best_idx = None
            best_score = -float("inf")

            for idx in candidates:
                emb = mem_embeddings[idx]
                relevance = valid_memories[idx].get("_retrieval_score", 0.0)

                # Max similarity to already-selected documents
                max_sim = 0.0
                if selected_embs and emb is not None:
                    for sel_emb in selected_embs:
                        sim = cosine_sim(emb, sel_emb)
                        if sim > max_sim:
                            max_sim = sim

                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim

                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx

            if best_idx is not None:
                selected.append(valid_memories[best_idx])
                selected_embs.append(mem_embeddings[best_idx])
                candidates.remove(best_idx)

        return selected

    def detect_adversarial(self, result_memories: list[dict]) -> dict:
        """Z-score gap analysis for adversarial/low-confidence detection.

        Uses statistical analysis of score distributions to detect:
        1. Flat distributions (all scores similar = no clear winner)
        2. Low absolute scores (nothing really matches)
        3. High diversity of sources needed

        Returns dict with:
        - "is_uncertain": bool — whether the results look unreliable
        - "confidence": float — overall confidence in the result set (0-1)
        - "score_gap": float — z-score normalized gap between top-1 and top-2
        - "abstain": bool — whether retrieval should abstain (very low confidence)
        """
        if len(result_memories) == 0:
            return {"is_uncertain": True, "confidence": 0.0, "score_gap": 0.0, "abstain": True}
        if len(result_memories) == 1:
            score = result_memories[0].get("_retrieval_score", 0.0)
            conf = min(1.0, score * 2)
            return {
                "is_uncertain": conf < 0.3,
                "confidence": conf,
                "score_gap": 0.0,
                "abstain": conf < 0.1,
            }

        scores = [mem.get("_retrieval_score", 0.0) for mem in result_memories]

        # Z-score analysis
        mean_s = sum(scores) / len(scores)
        std_s = (sum((s - mean_s) ** 2 for s in scores) / len(scores)) ** 0.5

        # Top-1 z-score: how far above mean is the best result?
        (scores[0] - mean_s) / std_s if std_s > 1e-9 else 0.0

        # Score gap between top-1 and top-2
        raw_gap = scores[0] - scores[1]
        z_gap = raw_gap / std_s if std_s > 1e-9 else 0.0

        # Coefficient of variation: low CV = flat distribution = uncertain
        cv = std_s / mean_s if mean_s > 1e-9 else 0.0

        # Confidence from multiple signals:
        # 1. Z-gap: clear winner has high z-gap
        gap_conf = min(1.0, z_gap / 2.0) if z_gap > 0 else 0.0
        # 2. Top-1 absolute score: very low = nothing matches
        abs_conf = min(1.0, scores[0] * 2)
        # 3. Distribution shape: high CV = clear separation
        dist_conf = min(1.0, cv * 2)

        confidence = 0.4 * gap_conf + 0.4 * abs_conf + 0.2 * dist_conf

        is_uncertain = confidence < self._settings.ADVERSARIAL_MIN_CONFIDENCE
        abstain = confidence < 0.15 or (scores[0] < 0.1 and z_gap < 0.5)

        return {
            "is_uncertain": is_uncertain,
            "confidence": round(confidence, 4),
            "score_gap": round(z_gap, 4),
            "abstain": abstain,
        }

    def compute_signal_confidence(
        self,
        signal_name: str,
        ranked_list: list[tuple[int, float]],
    ) -> float:
        """Compute confidence score for a retrieval signal's results.

        Returns a value in [0.0, 1.0] indicating how confident we are
        that this signal produced meaningful results. Used by confidence
        gating to zero out unreliable signals before fusion.
        """
        if signal_name == "vector":
            if not ranked_list:
                return 0.0
            top_score = ranked_list[0][1]
            if len(ranked_list) > 1:
                gap = ranked_list[0][1] - ranked_list[1][1]
            else:
                gap = top_score
            return min(1.0, top_score * (1 + gap))

        elif signal_name == "fts":
            if not ranked_list:
                return 0.0
            return min(1.0, len(ranked_list) / 5.0)

        elif signal_name in ("ppr", "spreading"):
            if not ranked_list:
                return 0.0
            scores = [s for _, s in ranked_list]
            if len(scores) < 2:
                return scores[0] if scores else 0.0
            max_score = max(scores)
            mean_score = sum(scores) / len(scores)
            return (max_score - mean_score) / max_score if max_score > 0 else 0.0

        elif signal_name == "temporal":
            if not ranked_list:
                return 0.0
            return min(1.0, len(ranked_list) / 3.0)

        return 0.5
