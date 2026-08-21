"""Backend wiki_add sync-write entry (R3 Car 1 write-half).

The queue drainer replays a queued ``wiki_add`` job by writing it synchronously
via the WikiStore. The enqueue fast-path, secret gate, and similarity gate live
in the core wiki shell / drainer pre-apply stage; this module owns the sync
execution (WikiStore.add / ingest + .md mirror + viz event).

Imports ``_shared`` + backend only — no ``yadgar.core.*`` edge. The FileQueue
used for the .md mirror is resolved from shared runtime state (_st._file_queue),
which the core lifecycle populates.
"""

from __future__ import annotations

import logging

import yadgar._shared.runtime.state as _st
from yadgar._shared.observability.observe import observe
from yadgar._shared.server_helpers import _push_event
from yadgar._shared.wiki.contract import WikiAddOptions

logger = logging.getLogger(__name__)


class _SlugExistsRejection(Exception):
    """Raised by run_wiki_add_replay when upsert=False and slug already exists.

    Propagates to _apply_pending via _apply_inner, which classifies it as
    "permanent" and routes it to DLQ via _reject_permanent_to_dlq. The DLQ
    sidecar is then read by _read_dlq_rejection to surface the rejection
    synchronously to wait=True callers.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"slug_exists: page already exists at slug {slug!r}")


def _mirror_wiki(slug: str, content: str) -> None:
    """Mirror the wiki page to a date-stamped .md via the FileQueue (non-fatal)."""
    fq = _st._file_queue
    if fq is None:
        return
    try:
        fq.write_wiki(slug, content)
    except Exception as exc:
        logger.debug("File queue wiki mirror failed (non-fatal): %s", exc)


@observe(tier="stage", metric="write_exec.wiki_add_sync_write")
def run_wiki_add_replay(payload: dict) -> dict:
    """Execute the sync wiki_add write path (drain-replay / fallback).

    ``payload`` is the queued wiki_add job dict (the same shape the drainer
    already holds). Recognised keys: ``title``, ``content``, ``category``,
    ``tags``, ``source_memory_ids``, ``confidence``, ``append``,
    ``replace_slug``, ``directory_context``, ``page_type``.

    Handles replace_slug overwrite, append merge, and normal upsert.
    """
    title = payload["title"]
    content = payload["content"]
    category = payload.get("category", "reference")
    tags = payload.get("tags")
    source_memory_ids = payload.get("source_memory_ids")
    confidence = payload.get("confidence", "medium")
    append = payload.get("append", False)
    replace_slug = payload.get("replace_slug")
    directory_context = payload.get("directory_context")
    page_type = payload.get("page_type")
    # Car B (#83): explicit-slug + upsert. slug=None → title-derived (legacy).
    explicit_slug = payload.get("slug")
    upsert = payload.get("upsert", True)
    # C3 (0047 PR#40 §5.C3): the enqueue-time project_id stamped by the core
    # tool (the only process that can see the session). Carried to
    # WikiAddOptions on BOTH construction sites below — the replace_slug branch
    # is a real write path — AND (C13) into ``ingest``, the append branch that
    # reaches ``WikiStore.add`` indirectly and was missed when this comment was
    # written. So ``insert_wiki_page`` receives it as ``caller_value`` and never
    # reaches the classifier this container cannot run (§1.1). None only for a
    # legacy payload enqueued before this car.
    project_id = payload.get("project_id")

    # replace_slug: overwrite a named existing page (gate already bypassed)
    # Car F (0047 §7): server-side sanctioned token threads from _wiki_write_canonical
    # so mutability='locked' page_types (e.g. ``adr``) can be first-inserted AND
    # updated by the canonical write seam.
    _sanctioned = bool(payload.get("_sanctioned", False))
    # Ledger task 271: the size-collapse gate's escape hatch
    # (storage/truncation_gate.py). Two ways open here — the caller's explicit
    # ``allow_truncation``, and ``_sanctioned``, which marks the CANONICAL
    # write seam (_wiki_write_canonical: task-list mirrors, ADR bodies). Those
    # regenerate a whole body from current truth and legitimately shrink — a
    # task-list mirror shrinks as tasks complete. The coupling lives HERE, at
    # the one seam that knows a write is canonical, and deliberately NOT inside
    # the gate: a regenerator emitting a gutted page is the case the gate is
    # best placed to catch, so every OTHER sanctioned writer stays gated.
    # Carried on BOTH option bundles below — the replace_slug branch is a real
    # write path.
    _allow_truncation = bool(payload.get("allow_truncation", False)) or _sanctioned
    if replace_slug is not None:
        existing = _st._wiki._storage.get_wiki_page_by_slug(replace_slug)
        if existing is not None:
            result = _st._wiki.add(
                title,
                content,
                category,
                tags or [],
                opts=WikiAddOptions(
                    source_memory_ids=source_memory_ids,
                    confidence=confidence,
                    directory_context=directory_context,
                    page_type=page_type,
                    sanctioned=_sanctioned,
                    project_id=project_id,
                    allow_truncation=_allow_truncation,
                ),
            )
            result.pop("embedding", None)
            _push_event(
                {
                    "event": "wiki_updated",
                    "node": {
                        "id": f"wiki:{result.get('id', '')}",
                        "type": "wiki",
                        "slug": result.get("slug", ""),
                        "title": result.get("title", ""),
                    },
                }
            )
            _mirror_wiki(result.get("slug", title), content)
            return result

    # W1 (ledger task 220): ONE option bundle for both branches. The append
    # branch used to hand ``ingest`` four positionals plus ``project_id`` and
    # drop the other six values on the floor — so an explicitly-slugged append
    # re-derived its slug from the TITLE and wrote a shadow page there, while
    # the response reported the caller's slug back. C13 fixed exactly one of the
    # six (``project_id``) because that one raised; the rest failed silently.
    _opts = WikiAddOptions(
        source_memory_ids=source_memory_ids,
        confidence=confidence,
        directory_context=directory_context,
        page_type=page_type,
        slug=explicit_slug,  # Car B (#83): store at caller slug, no title fallback
        upsert=upsert,
        sanctioned=_sanctioned,
        project_id=project_id,
        allow_truncation=_allow_truncation,
    )
    if append:
        result = _st._wiki.ingest(
            content,
            title,
            tags,
            source_memory_ids,
            category=category,
            opts=_opts,
        )
    else:
        result = _st._wiki.add(title, content, category, tags or [], opts=_opts)
    # Car C (#83): upsert=False collision → surface synchronously.
    # WikiStore.add returns {stored: False, reason: "slug_exists"} when upsert=False
    # and the slug already exists. _apply_inner ignores the return value, so the
    # rejection would be silently swallowed and the job archived as "committed".
    # Raising SlugExistsRejection makes _apply_pending classify it as "permanent"
    # and route it to DLQ via _reject_permanent_to_dlq, where _read_dlq_rejection
    # surfaces it synchronously to wait=True callers.
    if result.get("stored") is False and result.get("reason") == "slug_exists":
        raise _SlugExistsRejection(explicit_slug or "")
    result.pop("embedding", None)
    event_type = "wiki_updated" if result.get("_merged") else "wiki_added"
    _push_event(
        {
            "event": event_type,
            "node": {
                "id": f"wiki:{result.get('id', '')}",
                "type": "wiki",
                "slug": result.get("slug", ""),
                "title": result.get("title", ""),
            },
        }
    )
    _mirror_wiki(result.get("slug", title), content)
    return result
