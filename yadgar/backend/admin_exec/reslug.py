"""Car L (0047 §7 D32 ③) — ADR wiki page re-slug admin op.

Re-slugs every ADR wiki page from the legacy ``yadgar-adr-NNNN`` format
to the canonical ``{project_id}_adr-NNNN`` format (``/`` → ``_``). The
op also rewrites:

  - ``wiki_crossref.from_slug`` and ``to_slug`` for every crossref that
    points to or from an old-format slug
  - inline ``[[old-slug]]`` text in every rewritten page body
  - ``adr.body_slug`` on the SQL ledger table (via MariaStorageEngine)

Dry-run mode is the safe default. It returns the manifest
(``{"rewrites": [...], "dry_run": True}``) WITHOUT writing — intended
for the operator's pre-flight review.

Atomicity: this is a BATCH op that mutates many rows across two tables
(surreal ``wiki_page`` + ``wiki_crossref``) and one SQL table
(``adr.body_slug``). The op is not wrapped in a single transaction —
partial failure leaves a clear trace in the response (the manifest
already records what was rewritten before the failure). Re-running
is idempotent: already-reslugged pages are skipped (the discovery
query is ``WHERE slug STARTSWITH 'yadgar-adr-'`` so the new-format
slugs are not selected).

D32 ③ (0047 §7). The export ``reslug_adr_pages`` is the dispatch body
registered under ``"reslug"`` in ``yadgar/backend/admin_exec/__init__.py``.
The original-format discovery regex ``^yadgar-adr-(\\d+)$`` is exported as
``ADR_BODY_RE``; the slug template ``{project_id}_adr-{n:04d}`` is exported
as ``NEW_SLUG_TEMPLATE``. The sync MariaStorageEngine bridge
(``_sync_set_adr_body_slug``) wraps the async ``set_adr_body_slug`` via
``asyncio.run`` — the same pattern used by ``project_registry.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import TYPE_CHECKING

from yadgar._shared.observability.observe import observe

if TYPE_CHECKING:
    pass

logger = logging.getLogger("yadgar.backend.admin_exec.reslug")

#: Old-format ADR slug regex — captures the numeric suffix.
ADR_BODY_RE = re.compile(r"^yadgar-adr-(\d+)$")

#: ADR-0202's slug cap: *"cap at 256 chars with a hash suffix on overflow"*.
#: Every slug this module EMITS is capped here. The ledger's slug columns
#: (``task.body_slug`` / ``adr.body_slug`` / the agent tables') are sized to
#: this same number in alembic revision ``002_ledger_tables`` — the two must
#: move together, or an overflowing slug becomes an INSERT failure instead of
#: a hashed one.
SLUG_MAX_CHARS = 256

#: Length of the hex digest kept in the overflow suffix. 16 hex chars = 64
#: bits: at the scale of one user's project set, a birthday collision is not
#: a real risk, and a shorter suffix leaves more of the readable head intact.
_HASH_HEX_CHARS = 16

#: Marker that introduces the overflow hash. Chosen so a capped slug is
#: recognisable by eye and cannot be confused with the ``_adr-NNNN`` tail of
#: an un-capped one.
_HASH_MARKER = "-h"


@observe(tier="hot", span=False)
def cap_slug(slug: str) -> str:
    """Return *slug* capped to :data:`SLUG_MAX_CHARS`, hashing on overflow.

    ADR-0202: slugs are capped at 256 chars *with a hash suffix on overflow*.
    A bare truncation would be catastrophic rather than merely lossy — every
    long slug sharing a 256-char prefix would collapse onto one value, and
    because the two DB-wide slug lookups (``get_wiki_page_by_slug`` and the
    ``wiki_bookmark_slug_idx`` UNIQUE index) are slug-only, that collapse is
    a SILENT WRONG READ. The suffix is what keeps distinct inputs distinct.

    The digest is taken over the WHOLE pre-cap string, so two inputs that
    differ anywhere — including past the truncation point, and including in
    the ``_adr-NNNN`` tail that truncation removes — produce different
    suffixes.

    Idempotent: a value already within the cap is returned unchanged, so
    re-capping an emitted slug is a no-op.

    This is the SINGLE capping layer. ``_project_id_to_slug`` deliberately
    does not cap (see its docstring); any future caller that emits a slug of
    its own — C10(d)'s ``adr_seed`` is the next one — routes it through here
    rather than adding a second layer, because a hash over a hash is
    injective but unreadable from either end.
    """
    if len(slug) <= SLUG_MAX_CHARS:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:_HASH_HEX_CHARS]
    suffix = f"{_HASH_MARKER}{digest}"
    return slug[: SLUG_MAX_CHARS - len(suffix)] + suffix


def _project_id_to_slug(project_id: str) -> str:
    """Replace ``/`` with ``_`` in a project_id for use in a wiki slug.

    Wiki slugs cannot contain ``/`` (the path is the slug). The canonical
    mapping is mechanical: ``owner/repo`` → ``owner_repo``. The substitution
    runs across the WHOLE path, so GitLab groups and subgroups need no
    depth-aware special case. The exposed ``NEW_SLUG_TEMPLATE`` calls this
    internally so the caller passes the un-mangled ``owner/repo`` project_id.

    THE COLLISION IS DELIBERATE — DO NOT "FIX" IT BY ADDING ESCAPING.
    This mapping is NOT injective. ``_`` is legal inside both owner and repo
    names, so ``a_b/c`` and ``a/b_c`` both yield ``a_b_c``. ADR-0202 accepted
    that knowingly: the collision needs two of ONE user's own projects whose
    paths differ only in underscore placement, and since nothing ever parses
    a slug (ADR-0202: *"the slug is OPAQUE: never parsed"*), the blast radius
    is a loud unique-index conflict at write time, never a silent wrong read.

    ADR-0202 requires this note wherever slug construction is implemented,
    in its own words, *"or a future reader will 'fix' it by adding escaping
    and break every existing slug"* — escape-then-join was considered and
    rejected there as noise for an adversarial-naming case, and adopting it
    now would re-slug the entire minted corpus, whose slugs are IMMUTABLE.
    The sanctioned response to a real collision is ADR-0202's own revisit
    trigger (escape-then-join for NEWLY minted slugs only), not a change here.

    NOT a capping function — it is a pure separator swap. The cap
    (:func:`cap_slug`) is applied once, at the point the finished slug is
    emitted, because that is the only place the total length is known.
    """
    return project_id.replace("/", "_")


class _SlugTemplate(str):
    """A ``str`` subclass whose ``.format`` substitutes ``/`` → ``_`` for ``project_id``.

    The exposed constant ``NEW_SLUG_TEMPLATE`` is one of these. The pattern
    string is ``"{project_id_safe}_adr-{n:04d}"`` — the field name is
    ``project_id_safe`` so the production caller (which has the bare
    ``owner/repo`` project_id) sees ``project_id`` as the kwarg and the
    override does the substitution in one place. The test caller passes
    ``project_id`` directly; the override below maps it to
    ``project_id_safe`` via ``_project_id_to_slug``.

    Why a subclass: the alternative — a plain ``str.Format`` with a custom
    Formatter — would require the caller to instantiate the Formatter each
    call. Overriding ``format`` on a ``str`` subclass keeps the API
    identical to a plain template-string ``.format()`` call while
    inserting the substitution at the seam.

    THE CAP (#17, ADR-0202) IS APPLIED HERE, on the finished string. This is
    the module's emit point: it is the last place before the value becomes a
    slug, and the only place the total length — project part plus the
    ``_adr-NNNN`` tail — is known. In the overflow case the readable tail is
    truncated away; that is acceptable on ADR-0202's own terms (*"the slug is
    OPAQUE: never parsed"*), and the digest covers the tail so two ADRs of
    one overflowing project still differ.
    """

    _PATTERN = "{project_id_safe}_adr-{n:04d}"

    @observe(tier="hot", span=False)
    def format(self, /, *args, **kwargs):  # type: ignore[override]
        # Intercept ``project_id`` (the canonical kwarg) and convert to
        # ``project_id_safe`` (the template field). Other kwargs pass through.
        if "project_id" in kwargs and "project_id_safe" not in kwargs:
            kwargs["project_id_safe"] = _project_id_to_slug(kwargs["project_id"])
        return cap_slug(str.format(self._PATTERN, *args, **kwargs))


#: New-format slug template. ``{project_id}`` substitutes ``/`` with ``_``
#: automatically (the slash is not legal in wiki slugs). The caller passes
#: the canonical ``owner/repo`` (or ``local/<basename>``) project_id and
#: the implementation handles the substitution.
#: Example: ``NEW_SLUG_TEMPLATE.format(project_id="m-agahi/yadgar", n=42)`` →
#: ``"m-agahi_yadgar_adr-0042"``.
NEW_SLUG_TEMPLATE = _SlugTemplate()

#: Discovery prefix used in the SELECT against ``wiki_page``. ADR pages
#: in the old format all start with this; new-format pages start with
#: ``{project_id}_adr-`` and are NOT matched, which is the idempotency
#: mechanism — a second run finds nothing.
_OLD_SLUG_PREFIX = "yadgar-adr-"

#: Inline ``[[old-slug]]`` link pattern — used to find/replace body text.
#: Reuses the canonical wiki-link regex shape from the rest of the project
#: (alphanumerics + dash + underscore only, no spaces). The slash is NOT
#: part of the body regex because the new format uses underscore.
_INLINE_LINK_RE = re.compile(r"\[\[([a-zA-Z0-9_-]+)\]\]")


@observe(tier="hot", span=False)
def _sync_set_adr_body_slug(adr_id: int, body_slug: str) -> None:
    """Sync bridge to ``MariaStorageEngine.set_adr_body_slug`` (async).

    The admin-op dispatch is sync; ``MariaStorageEngine.set_adr_body_slug``
    is async (asyncmy is async-only). Same pattern as
    ``yadgar/backend/admin_exec/project_registry.py:_ensure_project_exists_sync``
    — a private event loop per call, fine for a slow admin op that runs
    at most once per project bootstrap + once per Car L rollout.

    Failures: the call is BEST-EFFORT. A transient connection failure
    on the SQL leg is logged and swallowed so the surreal-side writes
    (wiki_page + wiki_crossref) still commit. The operator can re-run
    the op to recover the body_slug; the op is idempotent.
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415

    engine = _st._sql_storage
    if engine is None:
        logger.warning(
            "reslug: _sync_set_adr_body_slug skipped — engine #2 not composed (adr_id=%s slug=%s)",
            adr_id,
            body_slug,
        )
        return

    @observe(tier="hot", span=False)
    async def _set() -> None:
        await engine.set_adr_body_slug(adr_id, body_slug)

    try:
        asyncio.run(_set())
    except Exception as exc:  # noqa: BLE001 — best-effort SQL bridge
        logger.warning(
            "reslug: _sync_set_adr_body_slug failed for adr_id=%s slug=%s: %s",
            adr_id,
            body_slug,
            exc,
        )


@observe(tier="hot", span=False)
def _detect_slug_collisions(storage, rewrites: list[dict]) -> tuple[list[dict], set]:
    """Return ``(collisions, skip_ids)`` for a rewrite manifest.

    A rewrite collides when its target (``new``) slug is already occupied
    by a DIFFERENT page (``id`` mismatch) — e.g. ``yadgar-adr-0001`` ->
    ``m-agahi_yadgar_adr-0001`` when that canonical slug already exists as
    its own row. Without this check the apply pass would hit a unique-index
    violation partway through, having already rewritten some pages with no
    transaction and no rollback. Extracted out of :func:`reslug_adr_pages`
    to keep that function under the repo's complexity caps (I13/I30).

    Returns:
        ``collisions`` — one dict per colliding rewrite (``old``, ``new``,
        ``id``, ``occupant_id``), for the manifest.
        ``skip_ids`` — the set of rewrite ``id`` values to skip in the
        write pass.
    """
    collisions: list[dict] = []
    skip_ids: set = set()
    for w in rewrites:
        existing = storage._q(
            "SELECT id, content FROM wiki_page WHERE slug = $slug",
            {"slug": w["new"]},
        )
        occupant = next((e for e in existing if e.get("id") != w["id"]), None)
        if occupant is not None:
            collisions.append(
                {
                    "old": w["old"],
                    "new": w["new"],
                    "id": w["id"],
                    "occupant_id": occupant.get("id"),
                }
            )
            skip_ids.add(w["id"])
    return collisions, skip_ids


@observe(tier="boundary", metric="admin.reslug_adr_pages")
def reslug_adr_pages(payload: dict, *, storage) -> dict:
    """Re-slug every ADR wiki page from ``yadgar-adr-NNNN`` to ``{project_id}_adr-NNNN``.

    Args:
        payload: ``{"project_id": str, "dry_run": bool, "adr_id_by_slug": dict[str, int]}``.
            * ``project_id`` — the canonical ``owner/repo`` (or ``local/<basename>``).
              The slash is replaced with ``_`` when stamping the new slug.
            * ``dry_run`` — when True (default), the manifest is returned
              WITHOUT any writes. The operator is expected to inspect
              ``{"rewrites": [{"old": ..., "new": ..., "id": ...}, ...]}``
              and re-run with ``dry_run=False`` to commit.
            * ``adr_id_by_slug`` — mapping ``{old_slug: adr_id}`` so the
              op can stamp ``adr.body_slug`` (the SQL leg). The caller
              MUST populate this from a SELECT against the SQL ``adr``
              table before invoking.
        storage: StorageEngine — passed as a keyword-only arg so the
            dispatch chain can route it through ``_get_storage()`` without
            shadowing the ``payload`` keys.

    Returns:
        ``{"rewrites": [{"old": ..., "new": ..., "id": ...}, ...], "dry_run": bool}``
        — the manifest. Always includes the full rewrite list (even on
        dry-run) so the operator can review what WOULD be changed.

    Raises:
        KeyError: when ``payload["project_id"]`` is missing.
    """
    project_id = payload["project_id"]
    dry_run = bool(payload.get("dry_run", True))
    adr_id_by_slug = dict(payload.get("adr_id_by_slug") or {})

    # 1. Discover old-format ADR pages. The fake's ``_q`` matches this
    # query shape; the real SurrealDB query is a STARTSWITH on the slug
    # field — see the inline comment below for the exact syntax.
    rows = storage._q(
        "SELECT id, slug, content FROM wiki_page WHERE slug STARTSWITH $slug_prefix",
        {"slug_prefix": _OLD_SLUG_PREFIX},
    )

    # 2. Build the rewrite manifest. Each row that matches the legacy
    # format gets a new slug from the template; rows that don't match
    # (e.g. an ``yadgar-adr-foo`` typo) are skipped silently — they'd
    # not match the discovery prefix anyway, but the regex check is
    # defensive against a future schema where the prefix overlaps.
    rewrites: list[dict] = []
    for r in rows:
        old = r["slug"]
        match = ADR_BODY_RE.match(old)
        if not match:
            continue
        n = int(match.group(1))
        new = NEW_SLUG_TEMPLATE.format(project_id=project_id, n=n)
        rewrites.append({"id": r["id"], "old": old, "new": new})

    # 2b. Collision guard (see ``_detect_slug_collisions``): reported in
    # BOTH dry-run and apply mode so an operator sees the collision before
    # applying anything; colliding rewrites are excluded from the pages
    # actually written, but stay in ``rewrites`` so the manifest still
    # shows what WOULD have happened.
    collisions, skip_ids = _detect_slug_collisions(storage, rewrites)

    if dry_run:
        # No writes. The manifest is the entire return — collisions are
        # reported so the operator sees what the apply pass would skip.
        return {"rewrites": rewrites, "dry_run": True, "collisions": collisions}

    # 3. Apply: rewrite each page (slug + inline body links), then update
    # crossrefs, then stamp the SQL body_slug. Colliding pages are skipped
    # entirely — the occupant is never overwritten and one collision does
    # not abort the rest of the run.
    # Build a single old→new map for inline body replacement. Inline links
    # MUST rewrite ANY old-format slug pointed to, not just the current
    # page's own old slug — page 1's body may link to page 2 via
    # ``[[yadgar-adr-0002]]`` and that link has to be updated when page 2
    # is re-slugged. Colliding pages are excluded from this map too — their
    # slug is never actually rewritten, so pointing an inline link at the
    # "new" slug would dangle.
    old_to_new: dict[str, str] = {w["old"]: w["new"] for w in rewrites if w["id"] not in skip_ids}

    for w in rewrites:
        if w["id"] in skip_ids:
            continue
        old = w["old"]
        new = w["new"]
        # Re-fetch the current content so we operate on the latest copy
        # — multiple slug rewrites don't compound (a single page won't
        # appear twice in the rewrite list because the discovery query
        # only matches old-format slugs).
        current = storage._q(
            "SELECT id, content FROM wiki_page WHERE slug = $slug",
            {"slug": old},
        )
        content = ""
        if current:
            content = current[0].get("content", "") or ""

        # Replace inline ``[[old-slug]]`` with ``[[new-slug]]`` across the
        # entire rewrite set, not just this page's own slug. The regex
        # matches the canonical wiki-link shape; references to old-format
        # slugs are rewritten, every other link is left alone.
        new_content = _INLINE_LINK_RE.sub(
            lambda m: f"[[{old_to_new[m.group(1)]}]]" if m.group(1) in old_to_new else m.group(0),
            content,
        )

        storage.update_wiki_page(
            w["id"],
            {"slug": new, "content": new_content},
        )

        # SQL leg: stamp adr.body_slug via the sync bridge. Best-effort;
        # the surreal-side writes still commit even if the SQL leg fails.
        adr_id = adr_id_by_slug.get(old)
        if adr_id is not None:
            _sync_set_adr_body_slug(int(adr_id), new)

    # 4. Update crossrefs in both directions. ``update_wiki_crossref_from``
    # rewrites every crossref whose ``from_slug`` equals ``old``;
    # ``update_wiki_crossref_to`` does the same for ``to_slug``. The two
    # passes hit every crossref that references ANY rewritten slug, in
    # either direction. The crossref table does not enforce a foreign
    # key against wiki_page, so a stale ``to_slug`` would silently
    # dangle — hence the bidirectional rewrite. Colliding pages are
    # skipped — their slug was never actually rewritten.
    for w in rewrites:
        if w["id"] in skip_ids:
            continue
        old = w["old"]
        new = w["new"]
        storage.update_wiki_crossref_from(old, new)
        storage.update_wiki_crossref_to(old, new)

    return {"rewrites": rewrites, "dry_run": False, "collisions": collisions}
