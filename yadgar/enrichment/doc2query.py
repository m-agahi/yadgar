"""Synthetic query generation via doc2query."""

import logging

from yadgar.config import Settings
from yadgar.enrichment._seq2seq import _load_seq2seq_model

logger = logging.getLogger(__name__)


class Doc2QueryExpander:
    """Synthetic query generation via doc2query."""

    def __init__(self) -> None:
        self._model = None
        self._tokenizer = None
        self._device = None
        self._unavailable = False

    def _ensure_model(self, model_name: str) -> bool:
        if self._model is not None:
            return True
        if self._unavailable:
            return False
        result = _load_seq2seq_model(model_name)
        if result is None:
            self._unavailable = True
            return False
        self._model, self._tokenizer, self._device = result
        return True

    def _token_overlap(self, a: str, b: str) -> float:
        """Compute token overlap ratio between two strings."""
        tokens_a = set(a.lower().split())
        tokens_b = set(b.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))

    def expand(self, content: str, settings: Settings) -> list[str]:
        if not self._ensure_model(settings.DOC2QUERY_MODEL):
            return []

        import torch

        num_queries = settings.DOC2QUERY_NUM_QUERIES

        input_ids = self._tokenizer(
            content, return_tensors="pt", padding=True, truncation=True, max_length=512
        ).input_ids.to(self._device)

        with torch.no_grad():
            outputs = self._model.generate(
                input_ids,
                num_beams=num_queries * 2,
                num_return_sequences=num_queries * 2,  # generate extra to filter
                max_length=64,
                do_sample=False,
            )

        queries: list[str] = []
        seen: set[str] = set()

        for seq in outputs:
            query = self._tokenizer.decode(seq, skip_special_tokens=True).strip()
            if not query:
                continue
            query_lower = query.lower()
            if query_lower in seen:
                continue
            if self._token_overlap(query, content) > 0.8:
                continue
            seen.add(query_lower)
            queries.append(query)
            if len(queries) >= num_queries:
                break

        return queries
