"""Real-model load smokes for the deps-modernization train (gates b/c/d).

Opt-in, NOT part of the CE-mocked CI legs: these tests download / load REAL
model weights (hundreds of MB) and run real inference. They exist to prove a
dependency-stack change (transformers 5.x, huggingface_hub 1.x — see
docs/plans/deps-modernization-train-2026-07-12.md) still loads and scores
every prod model path plus the Ettin candidates the T4 train needs.

Run locally as the pre-merge gate:

    YADGAR_MODEL_LOAD_SMOKE=1 uv run --extra test --extra ml \
        pytest yadgar/tests/backend/test_model_load_smoke.py -v -n0

Gate map (plan §Acceptance gates):
  (b) Ettin-32m / Ettin-68m load + score  — THE gate this train exists for
  (c) GTE prod reranker still loads + scores
  (d) MiniLM embed + doc2query load smoke

ADR-0192 dropped this file's flashrank case: it was `importorskip`-gated on a
package that is not in pyproject/uv.lock, so it never executed in any gate and
only pinned a dead constant.
"""

from __future__ import annotations

import math
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("YADGAR_MODEL_LOAD_SMOKE"),
    reason="real-model load smoke — opt-in via YADGAR_MODEL_LOAD_SMOKE=1 (downloads weights)",
)

SMOKE_PAIR = ("hello world", "a greeting")


def _assert_finite_score(scores) -> None:
    score = float(scores[0])
    assert math.isfinite(score), f"model returned non-finite score: {score}"


@pytest.mark.parametrize(
    "model_id",
    [
        "cross-encoder/ettin-reranker-32m-v1",
        "cross-encoder/ettin-reranker-68m-v1",
    ],
)
def test_ettin_loads_and_scores(model_id: str) -> None:
    """Gate (b): Ettin CrossEncoders load and score on the new stack.

    These models declare tokenizer_class=TokenizersBackend and
    transformers_version=5.7.0 — impossible on transformers 4.x.
    """
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(model_id)
    _assert_finite_score(model.predict([SMOKE_PAIR]))


def test_gte_prod_reranker_loads_and_scores() -> None:
    """Gate (c): the prod GTE reranker must not regress on the dep bump."""
    from sentence_transformers import CrossEncoder

    model = CrossEncoder("Alibaba-NLP/gte-reranker-modernbert-base")
    _assert_finite_score(model.predict([SMOKE_PAIR]))


def test_minilm_embed_loads_and_encodes() -> None:
    """Gate (d): prod embed model encodes with the expected dimension."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("all-MiniLM-L6-v2", trust_remote_code=True)
    vec = model.encode("the quick brown fox")
    assert len(vec) == 384
    assert all(math.isfinite(float(x)) for x in vec)


def test_doc2query_loads_and_generates() -> None:
    """Gate (d): doc2query seq2seq — the one direct-transformers prod path."""
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    model_id = "doc2query/msmarco-t5-small-v1"
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
    model.eval()
    inputs = tokenizer("the quick brown fox jumps over the lazy dog", return_tensors="pt")
    with torch.no_grad():
        out = model.generate(**inputs, max_length=32, num_return_sequences=1)
    text = tokenizer.decode(out[0], skip_special_tokens=True)
    assert isinstance(text, str) and text.strip()
