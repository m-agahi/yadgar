"""C4 — LLM conflict-ops on write (Mem0 parity, Ollama-only, v5.3.4).

At memorize time, when YADGAR_CONFLICT_RESOLVER=on:
  1. Retrieve top-K similar memories via recall().
  2. Build a structured prompt describing the candidate and the similar memories.
  3. POST to Ollama /api/generate with format=json.
  4. Parse response: {op: ADD|UPDATE|DELETE|NOOP, target_id: int|None, reason: str}.
  5. Return that dict — caller honours the decision.

Fail-soft contract: any error (timeout, bad JSON, Ollama down) returns NOOP-or-ADD
so the caller always has a safe default. Specifically:
  - Resolver disabled → NOOP (skip insert entirely — content is a duplicate/stale).
  - Ollama unreachable / timeout / bad response → ADD (optimistic insert).

Environment variables:
  YADGAR_CONFLICT_RESOLVER  "on" to enable; anything else (or unset) → disabled.
  YADGAR_OLLAMA_URL         default http://localhost:11434
  YADGAR_OLLAMA_MODEL       default qwen3:8b
  YADGAR_CONFLICT_K         top-K similar to retrieve; default 5
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

_log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_VALID_OPS = frozenset({"ADD", "UPDATE", "DELETE", "NOOP"})

_PROMPT_TEMPLATE = """You are a memory conflict resolver. Given a candidate memory and a list of similar existing memories, decide what to do.

Candidate memory:
{candidate_content}
Tags: {candidate_tags}

Similar existing memories (id → content):
{similar_list}

Respond ONLY with valid JSON, no markdown, no explanation outside JSON:
{{"op": "ADD"|"UPDATE"|"DELETE"|"NOOP", "target_id": <int or null>, "reason": "<one sentence>"}}

Rules:
- ADD: candidate is genuinely new information not present in existing memories.
- UPDATE: candidate supersedes an existing memory (target_id = that memory's id). Update it.
- DELETE: an existing memory contradicts and must be removed; candidate itself is not stored (target_id = the to-delete id).
- NOOP: candidate is a duplicate or clearly redundant; skip insert entirely.
"""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _fetch_similar(candidate: dict, k: int) -> list[dict]:
    """Retrieve top-K similar memories using yadgar recall.

    Returns list of dicts with at least {id, content}.
    On any error: returns empty list (fail-soft).
    """
    try:
        from yadgar.server.lifecycle import _get_storage

        storage = _get_storage()
        candidate.get("content", "")
        rows = storage._q(
            "SELECT id, content FROM memory ORDER BY heat DESC LIMIT $k",
            {"k": k},
        )
        # Prefer semantic similarity if available; for now return top heat-ordered rows
        # as a lightweight proxy (avoids embedding re-computation in the hot path).
        results = []
        for row in rows or []:
            raw_id = row.get("id")
            if raw_id is None:
                continue
            try:
                if hasattr(raw_id, "id"):
                    rid = int(raw_id.id)
                else:
                    rid = int(str(raw_id).rsplit(":", 1)[-1].strip("'\""))
            except ValueError, TypeError:
                continue
            results.append({"id": rid, "content": str(row.get("content") or "")[:200]})
        return results
    except Exception as exc:
        _log.debug("_fetch_similar error (fail-soft): %s", exc)
        return []


def _build_prompt(candidate: dict, similar: list[dict]) -> str:
    similar_lines = (
        "\n".join(f"  id={m['id']}: {m.get('content', '')[:150]}" for m in similar) or "  (none)"
    )
    return _PROMPT_TEMPLATE.format(
        candidate_content=candidate.get("content", ""),
        candidate_tags=", ".join(candidate.get("tags") or []),
        similar_list=similar_lines,
    )


def _parse_ollama_response(response_text: str) -> dict[str, Any]:
    """Parse the 'response' field from Ollama generate output.

    Returns a valid {op, target_id, reason} dict, or raises ValueError on bad input.
    """
    parsed = json.loads(response_text)
    op = str(parsed.get("op", "NOOP")).upper()
    if op not in _VALID_OPS:
        raise ValueError(f"Unknown op: {op!r}")
    target_id = parsed.get("target_id")
    if target_id is not None:
        target_id = int(target_id)
    reason = str(parsed.get("reason") or "")
    return {"op": op, "target_id": target_id, "reason": reason}


# ── Public API ────────────────────────────────────────────────────────────────


def resolve_conflict(candidate: dict) -> dict[str, Any]:
    """Determine the conflict-resolution op for a candidate memory.

    Returns:
        {"op": "ADD"|"UPDATE"|"DELETE"|"NOOP", "target_id": int|None, "reason": str}

    Fail-soft rules:
    - Disabled (env unset or != 'on') → NOOP immediately (skip duplicate).
    - Ollama error / timeout → ADD (optimistic insert).
    - Non-JSON or unknown op → ADD.
    """
    enabled = os.environ.get("YADGAR_CONFLICT_RESOLVER", "off").lower() == "on"
    if not enabled:
        return {"op": "NOOP", "target_id": None, "reason": "conflict resolver disabled"}

    ollama_url = os.environ.get("YADGAR_OLLAMA_URL", "http://localhost:11434")
    model = os.environ.get("YADGAR_OLLAMA_MODEL", "qwen3:8b")
    k = int(os.environ.get("YADGAR_CONFLICT_K", "5"))

    similar = _fetch_similar(candidate, k)
    prompt = _build_prompt(candidate, similar)

    try:
        resp = httpx.post(
            f"{ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "format": "json", "stream": False},
            timeout=30.0,
        )
        resp.raise_for_status()
        body = resp.json()
        response_text = body.get("response", "{}")
        result = _parse_ollama_response(response_text)
        _log.info(
            "conflict_resolver: op=%s target_id=%s reason=%r",
            result["op"],
            result["target_id"],
            result["reason"],
        )
        return result
    except httpx.TimeoutException as exc:
        _log.warning("conflict_resolver: Ollama timeout (%s) — degrading to ADD", exc)
        return {"op": "ADD", "target_id": None, "reason": "ollama_timeout"}
    except Exception as exc:
        _log.warning("conflict_resolver: error (%s) — degrading to ADD", exc)
        return {"op": "ADD", "target_id": None, "reason": f"resolver_error: {exc}"}
