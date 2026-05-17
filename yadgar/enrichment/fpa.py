"""False Positive Attenuation (FPA) filter for enrichment terms."""

import logging

import numpy as np

logger = logging.getLogger(__name__)


class FPAFilter:
    """Cosine similarity noise filter for enrichment terms."""

    def __init__(self, embedding_engine) -> None:
        self._engine = embedding_engine

    def filter(
        self,
        original_embedding: bytes,
        enrichment_texts: list[str],
        threshold: float,
    ) -> list[str]:
        if not enrichment_texts:
            return []

        original_vec = np.frombuffer(original_embedding, dtype=np.float32)
        kept = []

        for text in enrichment_texts:
            encoded = self._engine.encode_query(text)
            if encoded is None:
                continue
            text_vec = np.frombuffer(encoded, dtype=np.float32)
            similarity = float(np.dot(original_vec, text_vec))
            if similarity >= threshold:
                kept.append(text)

        rejected = len(enrichment_texts) - len(kept)
        if rejected > 0:
            logger.info(
                "FPA filter rejected %d/%d enrichment terms (threshold=%.2f)",
                rejected,
                len(enrichment_texts),
                threshold,
            )

        return kept
