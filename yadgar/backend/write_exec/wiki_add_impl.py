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
from yadgar._shared.wiki import WikiAddOptions

logger = logging.getLogger(__name__)


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
            ),
        )
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
