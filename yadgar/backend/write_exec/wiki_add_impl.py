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
    ``tags``, ``source_memory_ids``, ``confidence``, ``branch``, ``append``,
    ``replace_slug``, ``directory_context``, ``page_type``.

    Handles replace_slug overwrite, append merge, and normal upsert.
    """
    title = payload["title"]
    content = payload["content"]
    category = payload.get("category", "reference")
    tags = payload.get("tags")
    source_memory_ids = payload.get("source_memory_ids")
    confidence = payload.get("confidence", "medium")
    branch = payload.get("branch")
    append = payload.get("append", False)
    replace_slug = payload.get("replace_slug")
    directory_context = payload.get("directory_context")
    page_type = payload.get("page_type")
    # Car B0 (#83): repo-wiki module pages carry SHA256 + source path.
    src_hash = payload.get("hash")
    source_file = payload.get("source_file")
    # Car B (#83): explicit-slug + upsert. slug=None → title-derived (legacy).
    explicit_slug = payload.get("slug")
    upsert = payload.get("upsert", True)

    # replace_slug: overwrite a named existing page (gate already bypassed)
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
                    branch=branch,
                    directory_context=directory_context,
                    page_type=page_type,
                    hash=src_hash,
                    source_file=source_file,
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

    if append:
        result = _st._wiki.ingest(content, title, tags, source_memory_ids)
    else:
        result = _st._wiki.add(
            title,
            content,
            category,
            tags or [],
            opts=WikiAddOptions(
                source_memory_ids=source_memory_ids,
                confidence=confidence,
                branch=branch,
                directory_context=directory_context,
                page_type=page_type,
                hash=src_hash,
                source_file=source_file,
                slug=explicit_slug,  # Car B (#83): store at caller slug, no title fallback
                upsert=upsert,
            ),
        )
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
