"""Shared HuggingFace seq2seq model loader."""

import logging

from yadgar.observability.observe import observe

logger = logging.getLogger(__name__)


@observe(tier="stage")
def _load_seq2seq_model(
    model_name: str,
) -> tuple[object, object, str] | None:
    """Load a HuggingFace seq2seq model + tokenizer onto the best available device.

    Returns (model, tokenizer, device) on success, or None on ImportError /
    any load failure.  Callers are responsible for setting _unavailable=True
    when None is returned.
    """
    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name).to(device)
        model.eval()
        return model, tokenizer, device
    except (ImportError, Exception) as exc:  # noqa: BLE001
        logger.warning("seq2seq model %r unavailable: %s", model_name, exc)
        return None
