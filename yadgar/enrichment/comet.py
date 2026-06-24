"""COMET-BART commonsense inference engine.

RETIRED / DORMANT per ADR-0004 + benchmarks/reports/en2a_comet_ablation_2026-06-24.md
(net-negative recall, prohibitive cost). Intentionally retained — NOT dead code.
Do not enable without re-validating against the ablation. The model is lazy-loaded,
so this code is cost-free while COMET_ENRICHMENT_ENABLED is False (the default).
"""

import logging
import re

from yadgar.config import Settings
from yadgar.enrichment._seq2seq import _load_seq2seq_model

logger = logging.getLogger(__name__)


class CometInferencer:
    """COMET-BART commonsense inference engine."""

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

    def _extract_predicates(self, content: str) -> list[str]:
        """Extract sentences with named subjects and verbs."""
        sentences = re.split(r"[.!?]+", content)
        predicates = []
        # Match sentences that start with a capitalized word (potential named subject)
        # followed by a verb-like word
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            # Simple heuristic: sentence has a proper noun or pronoun + verb
            if re.match(r"^[A-Z][a-z]+\s+\w+", sent) or re.match(r"^(?:He|She|They|I|We)\s+", sent):
                predicates.append(sent)
        return predicates if predicates else [content.strip()]

    def infer(self, content: str, settings: Settings) -> list[str]:
        if not self._ensure_model(settings.COMET_MODEL):
            return []

        import torch

        relations = [r.strip() for r in settings.COMET_RELATIONS.split(",")]
        num_beams = settings.COMET_NUM_BEAMS
        top_k = settings.COMET_TOP_K_PER_RELATION
        min_confidence = settings.COMET_MIN_CONFIDENCE

        predicates = self._extract_predicates(content)
        all_inferences: list[str] = []
        seen: set[str] = set()

        for predicate in predicates:
            for relation in relations:
                prompt = f"{predicate} {relation} [GEN]"
                input_ids = self._tokenizer(
                    prompt, return_tensors="pt", padding=True, truncation=True
                ).input_ids.to(self._device)

                with torch.no_grad():
                    outputs = self._model.generate(
                        input_ids,
                        num_beams=num_beams,
                        num_return_sequences=min(top_k, num_beams),
                        max_length=64,
                        output_scores=True,
                        return_dict_in_generate=True,
                    )

                # Compute per-sequence scores via softmax over sequence scores
                if hasattr(outputs, "sequences_scores") and outputs.sequences_scores is not None:
                    scores = torch.softmax(outputs.sequences_scores, dim=0)
                else:
                    scores = torch.ones(len(outputs.sequences)) / len(outputs.sequences)

                for seq, score in zip(outputs.sequences, scores, strict=False):
                    text = self._tokenizer.decode(seq, skip_special_tokens=True).strip()
                    if not text or text.lower() == "none":
                        continue
                    if float(score) >= min_confidence and text not in seen:
                        seen.add(text)
                        all_inferences.append(text)

                # Cap at 3 per relation
                if len(all_inferences) >= 9:
                    return all_inferences[:9]

        return all_inferences[:9]
