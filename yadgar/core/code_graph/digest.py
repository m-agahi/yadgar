"""code_graph digest renderer — architecture dict → compact markdown block.

Car C of the code_graph train (ADR-0162).

``render_digest`` is a PURE function (no I/O): it turns the
``get_architecture(aspects=["all"])`` dict + endpoint rows + repo identity into a
compact, deterministic markdown digest bounded by ``DIGEST_CHAR_BUDGET``.  The
digest is what Car D injects into a yadgar memory BLOCK (always-injected at
SessionStart, recall-free).

Layout: a budget-RESERVED preamble (``header`` + the optional ``stale @ <sha>``
marker), followed by a body whose FOUR sections — layers, hotspots,
entry-points, endpoints — are still assembled in that priority order, but each
gets its OWN water-filled share of the remaining budget (Car 0087,
BC-CODEGRAPH-9) rather than all four sharing one budget with a single tail-cut
at the end:

    preamble: header > stale-line
    body:     layers, hotspots, entry-points, endpoints — each independently
              budgeted via ``_water_fill`` (max-min fair share); a section
              with less demand than its equal share frees the surplus for
              hungrier sections, but every section is guaranteed SOME share.

The marker is a qualifier on the WHOLE digest ("everything below describes
commit X and may be out of date"), so it belongs at the top on its own merits —
and reserving it inside the budget (rather than exempting it) keeps the single
``len(result) <= budget`` invariant that ``build_block_payload``'s ``chars`` and
the memory-block ``char_limit`` both lean on.  It used to be rendered LAST, so
the naive tail cut threw it away on any repo with package-qualified names
(measured: 3268 chars untruncated vs a 2000 budget → both ``endpoints:`` and the
marker absent).  See BC-CODEGRAPH-8.

BC-CODEGRAPH-9 (Car 0087): the SAME whole-blob tail-cut bug that BC-CODEGRAPH-8
fixed for the stale marker also applied to the body's own four sections —
whichever came first (``layers``) consumed the ENTIRE body budget on any real
repo, so ``endpoints`` (last in priority order) was silently truncated away
entirely, or worse, cut MID-LINE (observed live on this repo's own injected
``code_graph`` memory block: ``endpoints:\n  PATCH /`` — a truncated route
fragment, not a real one). The fix is per-section budgeting: each of the four
body sections is rendered against its OWN allocated share (``_water_fill``,
below), and within a section, truncation drops WHOLE lines/names from the end
— never a character-level mid-line cut — so a shown row is always complete.
Each truncated section carries its own ``… (N of M shown)`` marker so a reader
can tell "12 of 47 shown" apart from "47 of 47 shown, nothing hidden"; ``M`` is
always the TRUE total available (before the ``_MAX_*`` soft cap), so the
marker is honest even when the soft cap — not the per-section budget — did the
cutting.

Allocation policy (``_water_fill``): classic max-min fair share / progressive
filling. Start with an equal split of the body budget across the (2-4) active
sections; any section whose full untruncated render fits inside its equal
share is granted exactly that (its unused surplus is NOT reserved — it is
redistributed, again equal-split, among the remaining hungry sections); repeat
until every section is either fully satisfied or the remaining budget has been
split evenly among the sections still over their share. A section with ZERO
demand (``entry-points`` is entirely absent — no ``entry`` layer at all — is
skipped, not merely zero-content) never enters the split, so it can never
"reserve" budget it doesn't use. This is computed once, upfront, from each
section's full-text DEMAND — it does not do a second live pass to reclaim the
few chars a truncated section ends up leaving unused after line-level
fitting; that residual slack is small (at most one partial line's worth per
section) and is simply left unused rather than chased for a second time.
A safety-net whole-blob ``_truncate`` still runs on the assembled body if,
despite per-section budgeting, rounding or a pathologically tiny budget left
the total over ``body_budget`` — the same invariant the degenerate branch
below always guaranteed, now just rarely needed.

Determinism: every collection is sorted with an explicit tie-break (this repo's
own ADR-0108/0147 flake was exactly an un-tie-broken sort), no timestamps → same
input yields identical bytes, so golden tests are stable.

NOISE the renderer must IGNORE: the architecture dict carries a ``routes[]`` of
URL literals — that is not endpoint data.  Endpoints come ONLY from the
``Method.route_method`` Cypher rows (fetched I/O-side in ``runner``), never from
``routes[]`` and never from ``Route`` nodes.  The SAME URL-literal noise class
has also been observed leaking through ``layers`` rows (a route-path fragment,
sometimes containing PII such as an email, standing in for a real
package/component name) — ``_filter_layer_noise``/``_looks_like_url_literal``
guard that path before ``_layers_data``/``_entry_points_data`` render it.
A SECOND, distinct noise class (task #58) — Python builtins and generic short
route-path fragments mislabelled as layers — is caught by
``_looks_like_builtin_layer``/``_looks_like_generic_route_fragment``; all three
heuristics are unioned in ``_is_layer_noise``, the single choke point
``_filter_layer_noise`` consults.

Secret-gate note (#30): the LIVE block write (Car D / Claude → ``block_update``)
passes ``gate_or_reject`` — the SAME secret gate as ``wiki_add``.  This digest is
a SUMMARY (layer / hotspot / endpoint NAMES), never raw code or long
base64/token-like strings, so path/identifier false-positive risk is reduced.
The one residual risk is a benign EXACTLY-40-char ``[A-Za-z0-9/+]`` run (a full
git SHA, or a coincidental 40-char identifier/path segment) tripping the gate's
keyword-gated AWS-40 heuristic — ``_defang_secret_shaped_runs`` breaks such runs
here so that shape can never form.  That is FP-PREVENTION, not a gate: the gate
itself is UNCHANGED and remains the authoritative last line of defence (Car C is
the renderer only).
"""

from __future__ import annotations

import builtins
import keyword
import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from yadgar._shared.observability.observe import observe
from yadgar.core.code_graph import config

#: Rows shown per section before truncation kicks in (soft caps; the hard budget
#: is the real ceiling).  Kept small so a well-formed digest fits ~2000 chars.
_MAX_LAYERS = 12
_MAX_HOTSPOTS = 10
_MAX_ENTRY_POINTS = 6
_MAX_ENDPOINTS = 20

#: Ellipsis marker appended when the digest is truncated to budget.  Counted in
#: the budget (reserve its length before cutting) so ``len(out) <= budget`` holds.
_ELLIPSIS = "\n…"

#: TLD-like final segments that mark a dotted string as domain-shaped noise
#: rather than a real (dotted) qualified name.  Deliberately narrow — this is
#: only consulted by ``_looks_like_url_literal``, which is only ever applied to
#: ``layers`` rows (component/package names), never to hotspot qualified names
#: (those legitimately look like ``pkg.Class.method``).
_URL_LITERAL_TLD_SUFFIXES = frozenset(
    {"com", "org", "net", "io", "co", "dev", "app", "gov", "edu", "info", "biz"}
)

#: Python builtins + keywords that the indexer misclassifies as ``core:`` "high
#: fan-in" layers (task #58). ``len``/``dict``/``str``/``range``/``list``/… are
#: never real modules/packages in ANY indexed repo — the indexer counts every
#: reference to the builtin *name* as fan-in and mislabels it a layer. Built from
#: ``dir(builtins)`` + keywords (NOT a hardcoded five-name list) so ``int``,
#: ``set``, ``tuple``, ``type``, ``object``, … surfacing in a different repo are
#: covered too. Consulted only by ``_looks_like_builtin_layer`` (layer names).
_BUILTIN_LAYER_NAMES = frozenset(dir(builtins)) | frozenset(keyword.kwlist)

#: Substring (case-insensitive) marking a layer's ``reason`` as route-derived —
#: the noise vector for ``_looks_like_generic_route_fragment``.
_ROUTE_LAYER_REASON_MARKER = "http route"

#: Max length of a bare route-fragment layer name (``db``=2, ``test``=4,
#: ``jsonl``=5). A route-derived layer whose name is a plain lowercase token this
#: short is a URL-path SEGMENT the indexer mislabelled, not a real controller /
#: package (those are CamelCase, dotted, pathed, or underscored —
#: ``UserController``, ``api.v1``, ``internal/http`` — all spared).
_GENERIC_ROUTE_NAME_MAXLEN = 5

#: Secret-gate false-positive guard (#30, ADR-0121 + ADR-0162).
#:
#: The LIVE code_graph block write (Claude → ``block_update``) runs the SAME
#: ``gate_or_reject`` secret gate as ``wiki_add``. That gate's BROAD AWS-secret
#: heuristic — ``(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])`` — matches
#: an EXACTLY-40-char run when a keyword (``aws``/``secret``/``access``/``key``/
#: ``token``/``credential``) co-occurs. A benign digest routinely carries such a
#: keyword (any auth/token/key component name), and a full git SHA (exactly 40
#: hex) or a coincidental 40-char identifier/path segment then trips the gate and
#: the block write is REJECTED.
#:
#: Fix = Option B (renderer-side, gate UNCHANGED): break every ``[A-Za-z0-9/+]``
#: run of length >= _SECRET_RUN_MIN into <= _SECRET_RUN_CHUNK-char pieces so the
#: exactly-40 shape can never form. The gate stays the last line of defence:
#: because we INSERT a break (never drop chars) and the first piece is always
#: _SECRET_RUN_CHUNK long, every self-anchored high-precision rule still fires on
#: it — the longest such body minimum is Stripe ``sk_live_`` at 24 chars, so the
#: chunk size MUST sit in [24, 39]. The threshold is >= 40 so a Google ``AIza``
#: key (exactly 39 chars) is left byte-for-byte intact. Nothing here weakens the
#: gate for any other caller (memorize / wiki_add / other block writers).
_SECRET_RUN_MIN = 40
_SECRET_RUN_CHUNK = 30
_SECRET_SHAPED_RUN_RE = re.compile(rf"[A-Za-z0-9/+]{{{_SECRET_RUN_MIN},}}")


@observe(tier="stage")
def _defang_secret_shaped_runs(text: str) -> str:
    """Break long ``[A-Za-z0-9/+]`` runs so no exactly-40 AWS-secret shape forms.

    Each maximal run of length >= ``_SECRET_RUN_MIN`` is split into
    ``_SECRET_RUN_CHUNK``-char pieces joined by a single space. Chars are never
    dropped, so a genuine high-precision secret planted in digest content is
    still fully detectable by the downstream ``gate_or_reject`` block-write gate
    (its self-anchored rules match the first, >= 24-char piece). Deterministic
    (pure function of ``text``) — golden digests stay stable.
    """

    def _chunk(match: re.Match[str]) -> str:
        run = match.group(0)
        return " ".join(
            run[i : i + _SECRET_RUN_CHUNK] for i in range(0, len(run), _SECRET_RUN_CHUNK)
        )

    return _SECRET_SHAPED_RUN_RE.sub(_chunk, text)


@observe(tier="stage")
def _header_line(architecture: dict[str, Any], identity: dict[str, Any]) -> str:
    """Build the ``── code_graph: <repo-id> (<langs>) ──`` header line."""
    repo_id = (
        architecture.get("project") or Path(str(identity.get("canonical_root", "?"))).name or "?"
    )
    langs = _sorted_languages(architecture)
    lang_names = ", ".join(lang for lang, _ in langs[:3]) or "?"
    return f"── code_graph: {repo_id} ({lang_names}) ──"


@observe(tier="stage")
def _sorted_languages(architecture: dict[str, Any]) -> list[tuple[str, int]]:
    """Return ``[(language, file_count), ...]`` sorted (-file_count, language)."""
    langs: list[tuple[str, int]] = []
    for row in architecture.get("languages") or []:
        name = str(row.get("language", ""))
        count = int(row.get("file_count", 0) or 0)
        if name:
            langs.append((name, count))
    langs.sort(key=lambda lc: (-lc[1], lc[0]))
    return langs


@observe(tier="stage")
def _looks_like_url_literal(name: str) -> bool:
    """True when ``name`` looks like a leaked URL/route-path fragment.

    ADR-0162 says "Route nodes = URL-literal noise, ignore" for endpoints —
    but the upstream indexer has ALSO been observed emitting ``layers`` rows
    whose ``name`` is a URL-path fragment (e.g. a hardcoded test-fixture email
    used as a route path segment, like
    ``/gr/v1/shard/email/test.user@example.com/9``) rather than a real
    package/module/component name. Those fragments can carry PII (emails) when
    the indexed repo's test fixtures embed them in route strings, so they must
    never reach the rendered digest.

    Heuristics (conservative — a false positive here just drops one layer
    row, never crashes; only ever applied to ``layers`` names, NOT hotspot
    qualified names, which legitimately use dotted paths like
    ``pkg.Class.method``):

      - contains ``@``       → email-shaped fragment.
      - contains ``/``       → path-shaped fragment; a real layer/component
                                name is never a slash-joined fragment.
      - starts with a digit  → path segments / ids (e.g. ``9``), never a real
                                class/package/component name.
      - dot-separated with a TLD-like final segment (e.g. ``quinyx.com``) →
                                domain-shaped fragment.
    """
    if not name:
        return False
    if "@" in name:
        return True
    if "/" in name:
        return True
    if name[0].isdigit():
        return True
    if "." in name:
        suffix = name.rsplit(".", 1)[-1].lower()
        if suffix in _URL_LITERAL_TLD_SUFFIXES:
            return True
    return False


@observe(tier="stage")
def _looks_like_builtin_layer(name: str) -> bool:
    """True when ``name`` is a Python builtin/keyword misclassified as a layer.

    Task #58: builtins (``len``, ``dict``, ``str``, ``range``, ``list``, ``int``,
    ``type``, ``object`` …) surface as ``core:`` layers with a ``high fan-in``
    reason. They are never real modules — see ``_BUILTIN_LAYER_NAMES``. Shape
    rules (``_looks_like_url_literal``) can't catch them (no ``@``/``/``/digit/
    TLD), so this is a DIFFERENT, membership-based heuristic added alongside.
    """
    return name in _BUILTIN_LAYER_NAMES


@observe(tier="stage")
def _looks_like_generic_route_fragment(name: str, reason: str) -> bool:
    """True when a route-derived layer name is a bare short URL-path segment.

    Task #58: ``db``/``jsonl``/``test`` surface as ``api:`` layers with a
    ``has HTTP route definitions`` reason — the same route-noise class as the
    URL-literal leak, but a fragment too short/plain to trip
    ``_looks_like_url_literal``'s shape rules. Gated on the route reason (NOT
    name-shape alone) so a short lowercase name that is NOT route-derived (e.g. a
    ``core`` package) is spared; credible route layers (CamelCase / dotted /
    pathed / underscored, or longer than ``_GENERIC_ROUTE_NAME_MAXLEN``) survive
    because ``str.isalnum`` excludes separators. Tradeoff: a hypothetical REAL
    controller literally named e.g. ``auth`` with a route reason is also dropped
    — acceptable for a lossy summary, and the gate never touches fan-in /
    package-boundary layers.
    """
    if _ROUTE_LAYER_REASON_MARKER not in reason.lower():
        return False
    if not name or len(name) > _GENERIC_ROUTE_NAME_MAXLEN:
        return False
    return name.isascii() and name.isalnum() and name.islower()


@observe(tier="stage")
def _is_layer_noise(row: dict[str, Any]) -> bool:
    """True when a ``layers`` row is any known noise class (task #58 + URL leak).

    Union of three heuristics — URL-literal shape, Python-builtin membership,
    and generic route-fragment (reason-gated). Real layer/component names match
    none and survive.
    """
    name = str(row.get("name", ""))
    reason = str(row.get("reason", ""))
    return (
        _looks_like_url_literal(name)
        or _looks_like_builtin_layer(name)
        or _looks_like_generic_route_fragment(name, reason)
    )


@observe(tier="stage")
def _filter_layer_noise(architecture: dict[str, Any]) -> dict[str, Any]:
    """Return ``architecture`` with noise ``layers`` rows dropped.

    Single choke point: both ``_layers_data`` and ``_entry_points_data``
    read ``architecture["layers"]``, so filtering once here (rather than
    duplicating the check in each section) closes both leak paths in one place.
    Noise = URL-literal fragments OR Python builtins OR generic route fragments
    (see ``_is_layer_noise``).
    """
    layers = architecture.get("layers") or []
    clean = [row for row in layers if not _is_layer_noise(row)]
    if len(clean) == len(layers):
        return architecture
    filtered = dict(architecture)
    filtered["layers"] = clean
    return filtered


@observe(tier="stage")
def _layers_data(architecture: dict[str, Any]) -> tuple[str, list[str], int]:
    """Return ``(header, item_lines, total_available)`` for the layers section.

    Sorted by (layer, name) so equal layers keep a stable order (tie-break).
    ``entry``-layer rows are ALSO surfaced as entry-points elsewhere; they stay
    in the layers list too (the two views serve different questions).

    ``item_lines`` are soft-capped to ``_MAX_LAYERS`` AND already defanged
    (``_defang_secret_shaped_runs``, per-line — see the "Defang timing" note on
    ``_fit_line_section``) so a caller can measure/join them directly with no
    second whole-text defang pass. ``total_available`` is the FULL row count
    BEFORE the ``_MAX_LAYERS`` soft cap, so a "N of M shown" marker can report
    the true scope even when the soft cap — not a per-section budget — is what
    trimmed the list.
    """
    rows = []
    for row in architecture.get("layers") or []:
        name = str(row.get("name", ""))
        layer = str(row.get("layer", ""))
        reason = str(row.get("reason", ""))
        if name or layer:
            rows.append((layer, name, reason))
    rows.sort(key=lambda r: (r[0], r[1]))

    total_available = len(rows)
    items = [
        _defang_secret_shaped_runs(f"  {layer}: {name}" + (f" — {reason}" if reason else ""))
        for layer, name, reason in rows[:_MAX_LAYERS]
    ]
    return "layers:", items, total_available


@observe(tier="stage")
def _entry_points_data(architecture: dict[str, Any]) -> tuple[str, list[str], int] | None:
    """Return ``(prefix, names, total_available)`` for entry-points, or ``None``.

    Derived from ``layers`` where ``layer == 'entry'`` (there is no
    ``entry_points`` key in the measured architecture shape). ``None`` — not a
    zero-item tuple — when there are no entry rows at all: matches the
    pre-existing behavior of skipping the section entirely (no bare header),
    and means the section contributes NO demand to ``_water_fill``, so its
    entire notional share is free for the other three sections rather than
    being reserved-but-unused.

    ``names`` are soft-capped to ``_MAX_ENTRY_POINTS`` and already defanged.
    """
    names_all = sorted(
        str(row.get("name", ""))
        for row in architecture.get("layers") or []
        if str(row.get("layer", "")) == "entry" and row.get("name")
    )
    if not names_all:
        return None
    total_available = len(names_all)
    names = [_defang_secret_shaped_runs(n) for n in names_all[:_MAX_ENTRY_POINTS]]
    return "entry-points: ", names, total_available


@observe(tier="stage")
def _hotspots_data(architecture: dict[str, Any]) -> tuple[str, list[str], int]:
    """Return ``(header, item_lines, total_available)`` for the hotspots section.

    Sort key ``(-fan_in, qualified_name)`` — equal-fan_in hotspots keep a stable
    order (ADR-0108/0147: an un-tie-broken sort is exactly the flake class).
    ``item_lines`` soft-capped to ``_MAX_HOTSPOTS`` and already defanged; see
    ``_layers_data`` for why both of those matter to the caller.
    """
    rows = []
    for row in architecture.get("hotspots") or []:
        qname = str(row.get("qualified_name") or row.get("name") or "")
        fan_in = int(row.get("fan_in", 0) or 0)
        if qname:
            rows.append((fan_in, qname))
    rows.sort(key=lambda r: (-r[0], r[1]))

    total_available = len(rows)
    items = [
        _defang_secret_shaped_runs(f"  {qname} (fan_in={fan_in})")
        for fan_in, qname in rows[:_MAX_HOTSPOTS]
    ]
    return "hotspots:", items, total_available


@observe(tier="stage")
def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first truthy value in ``row`` among ``keys``, as str, else ''.

    Module-level (not a nested closure) so it carries its own tri-signal span.
    """
    for key in keys:
        val = row.get(key)
        if val:
            return str(val)
    return ""


@observe(tier="stage")
def _extract_endpoint(row: dict[str, Any] | list[Any]) -> tuple[str, str, str]:
    """Extract ``(method, path, name)`` from ONE route_method Cypher row.

    THE single endpoint-row shape site — the binary is mocked in Car B/C, so
    whether the keys come back prefixed (``m.route_method``) or bare
    (``route_method``) was UNVERIFIED. Car F live-smoke (2026-07-26) found the
    real binary returns rows as bare positional lists, not dicts — matching
    ``_ENDPOINT_CYPHER``'s ``RETURN m.route_method, m.route_path, m.name``
    column order. Both shapes are accepted here (mirrors ``runner._run_tool``'s
    single-correction-site philosophy).
    """
    if isinstance(row, list):
        method = str(row[0]) if len(row) > 0 and row[0] else ""
        path = str(row[1]) if len(row) > 1 and row[1] else ""
        name = str(row[2]) if len(row) > 2 and row[2] else ""
        return method, path, name
    method = _pick(row, ("m.route_method", "route_method"))
    path = _pick(row, ("m.route_path", "route_path"))
    name = _pick(row, ("m.name", "name"))
    return method, path, name


@observe(tier="stage")
def _endpoints_data(endpoints: list[dict[str, Any]]) -> tuple[str, list[str], int]:
    """Return ``(header, item_lines, total_available)`` for the endpoints section.

    0 rows (PHP/Go framework routes not parsed) → the ``header`` IS the literal
    ``"endpoints: (none extracted)"`` text with no items and
    ``total_available=0``, so it renders as a single line with no "N of M"
    marker (0 shown of 0 available — nothing was hidden) — this is a case, not
    a special short-circuit, so ``_fit_line_section`` handles it uniformly.
    Otherwise sorted by (path, method) for determinism and soft-capped to
    ``_MAX_ENDPOINTS``; items already defanged.
    """
    parsed = []
    for row in endpoints or []:
        method, path, _name = _extract_endpoint(row)
        if method or path:
            parsed.append((path, method))
    if not parsed:
        return _defang_secret_shaped_runs("endpoints: (none extracted)"), [], 0

    parsed = sorted(set(parsed))
    total_available = len(parsed)
    items = [
        _defang_secret_shaped_runs(f"  {method} {path}".rstrip())
        for path, method in parsed[:_MAX_ENDPOINTS]
    ]
    return "endpoints:", items, total_available


@observe(tier="stage")
def _stale_line(identity: dict[str, Any]) -> list[str]:
    """Render ``stale @ <sha>`` when the identity says the index is stale.

    Passthrough only — no ``detect_changes`` call, no staleness detection here
    (a fresh index is never stale; Car C is the renderer, not the freshness
    authority).
    """
    if identity.get("stale") and identity.get("head_sha"):
        # Short SHA (12 chars): human-readable AND < 40 chars, so a full 40-hex
        # SHA never forms the gate's exactly-40 AWS-secret shape (#30). The
        # renderer-wide _defang_secret_shaped_runs pass is the general net; this
        # keeps the stale line clean rather than splitting a hash mid-string.
        short_sha = str(identity.get("head_sha"))[:12]
        return [f"stale @ {short_sha}"]
    return []


@observe(tier="stage")
def _join_sections(sections: list[list[str]]) -> str:
    """Flatten sections into one newline-joined string, dropping empty entries.

    The ``if line`` filter is load-bearing: an absent section (``_stale_line``
    returning ``[]`` on a fresh digest) must not leave a blank line behind, which
    would shift every following line by one.
    """
    lines: list[str] = []
    for section in sections:
        lines.extend(section)
    return "\n".join(line for line in lines if line)


@observe(tier="stage")
def _truncate(text: str, budget: int) -> str:
    """Return ``text`` bounded by ``budget``, appending ``_ELLIPSIS`` if cut.

    Reserves the ellipsis length BEFORE cutting so the final string (marker
    included) is ``<= budget`` — the classic off-by-marker bug is avoided.
    """
    if len(text) <= budget:
        return text
    keep = max(0, budget - len(_ELLIPSIS))
    return text[:keep] + _ELLIPSIS


@observe(tier="stage")
def _section_full_text(header: str, items: list[str], total_available: int) -> str:
    """Render a line-style section's full (budget-unaware) text.

    ``items`` are ALREADY defanged (see ``_layers_data``/``_hotspots_data``/
    ``_endpoints_data``) — this function only joins, never re-derives content.
    Used both as the fast-path return of ``_fit_line_section`` (when the full
    text already fits its allocated share) and as the DEMAND figure
    ``_water_fill`` measures each section against. A trailing ``"… (N of M
    shown)"`` marker is appended whenever the soft cap already dropped rows
    (``len(items) < total_available``), even before any budget constraint is
    applied — so the reader is told the true scope regardless of which
    mechanism (soft cap or budget) did the trimming.
    """
    shown = len(items)
    lines = [header, *items]
    if shown < total_available:
        lines.append(f"  … ({shown} of {total_available} shown)")
    return "\n".join(lines)


@observe(tier="stage")
def _csv_full_text(prefix: str, names: list[str], total_available: int) -> str:
    """Render entry-points' full (budget-unaware) comma-joined single line.

    ``names`` are ALREADY defanged (see ``_entry_points_data``). Mirrors
    ``_section_full_text`` but for the ONE section rendered as a single
    comma-joined line rather than one line per item.
    """
    shown = len(names)
    text = prefix + ", ".join(names)
    if shown < total_available:
        text += f" … (+{total_available - shown} more)"
    return text


@observe(tier="stage")
def _fit_line_section(header: str, items: list[str], total_available: int, budget: int) -> str:
    """Fit a header + item lines (layers/hotspots/endpoints style) to ``budget``.

    Defang timing: ``items`` arrive already defanged (one ``_defang_secret_shaped_runs``
    pass per line, at construction in ``_layers_data``/``_hotspots_data``/
    ``_endpoints_data``) rather than defanged here as one whole-text pass. The
    two are equivalent — no ``[A-Za-z0-9/+]`` run can span a ``"\\n"`` or the
    ``" — "``/``" "`` separators inside a line, since none of those characters
    are in that class — but per-line defang lets this function measure
    already-final lengths, which is what keeps the ``len(out) <= budget``
    invariant exact once each section is measured/allocated independently.

    When the full untruncated rendering (header + every item, plus an "N of M"
    marker if the soft cap already trimmed some) fits in ``budget``, it is
    returned unchanged. Otherwise items are dropped from the END (lowest
    priority within the section) one at a time — never a mid-line character
    cut, so a shown row is always a complete, well-formed line — until what
    remains, plus a marker reporting the new (smaller) shown-count, fits.
    """
    full_text = _section_full_text(header, items, total_available)
    if len(full_text) <= budget:
        return full_text

    for shown in range(len(items), -1, -1):
        kept = items[:shown]
        remaining = total_available - shown
        lines = [header, *kept]
        if remaining > 0:
            lines.append(f"  … ({shown} of {total_available} shown)")
        text = "\n".join(lines)
        if len(text) <= budget:
            return text
    # Pathological: budget too small even for the bare header. render_digest
    # re-checks the assembled body against body_budget and falls back to a
    # whole-blob _truncate as a safety net, so returning the header here
    # (rather than an empty string) keeps this function total and simple.
    return header


@observe(tier="stage")
def _fit_csv_section(prefix: str, names: list[str], total_available: int, budget: int) -> str:
    """Fit the entry-points comma-joined single line to ``budget``.

    ``names`` are already defanged/soft-capped (see ``_entry_points_data``).
    Whole NAMES are dropped from the end, never a mid-name character cut —
    mirrors ``_fit_line_section``'s line-level (not char-level) truncation.
    """
    full_text = _csv_full_text(prefix, names, total_available)
    if len(full_text) <= budget:
        return full_text

    for shown in range(len(names), -1, -1):
        kept = names[:shown]
        remaining = total_available - shown
        text = prefix + ", ".join(kept)
        if remaining > 0:
            text += f" … (+{remaining} more)"
        if len(text) <= budget:
            return text
    return prefix


@observe(tier="stage")
def _water_fill(demands: list[int], total_budget: int) -> list[int]:
    """Max-min fair share: split ``total_budget`` across ``demands``, in order.

    Classic progressive-filling bandwidth-allocation algorithm. A section
    whose demand is <= its current equal share is granted its FULL demand
    immediately; the leftover is redistributed (again equal-split) among the
    remaining sections. This is what stops an early, big section (layers)
    from starving a later one (endpoints) the way a single shared
    whole-budget tail-cut did — every section still in the running gets at
    least the current equal share, and a section that needs less than its
    share never "reserves" the unused part.

    Deterministic: ``demands`` is iterated as a list (never a set), in the
    caller's priority order; only integer ``//``/``%`` are used, never floats
    — so equal runs are reproducible byte-for-byte and the split does not
    depend on set/dict iteration order.

    Returns a list of per-index allocations (same length/order as ``demands``)
    summing to at most ``total_budget``.
    """
    n = len(demands)
    allocation = [0] * n
    if n == 0 or total_budget <= 0:
        return allocation

    remaining_budget = total_budget
    active = list(range(n))
    while active:
        share = remaining_budget // len(active)
        satisfied_now = [i for i in active if demands[i] <= share]
        if satisfied_now:
            for i in satisfied_now:
                allocation[i] = demands[i]
                remaining_budget -= demands[i]
            active = [i for i in active if i not in satisfied_now]
            continue
        # Nobody's demand fits the current equal share: split what remains
        # evenly across the still-active sections. The first `extra` sections
        # (in `active`'s fixed order) get one additional char so the shares
        # sum EXACTLY to remaining_budget.
        base = remaining_budget // len(active)
        extra = remaining_budget % len(active)
        for idx, i in enumerate(active):
            allocation[i] = base + (1 if idx < extra else 0)
        active = []
    return allocation


@observe(tier="stage")
def render_digest(
    architecture: dict[str, Any],
    endpoints: list[dict[str, Any]],
    identity: dict[str, Any],
    *,
    budget: int | None = None,
) -> str:
    """Render a compact, deterministic markdown digest bounded by ``budget``.

    PURE function.  ``architecture`` = ``get_architecture(aspects=["all"])`` dict.
    ``endpoints`` = route_method Cypher rows (NOT ``routes[]``, NOT ``Route``
    nodes).  ``identity`` carries ``canonical_root`` / ``subdir`` and optional
    ``stale`` / ``head_sha``.

    The header and the optional ``stale @ <sha>`` marker form a budget-RESERVED
    preamble. The body's four sections (layers, hotspots, entry-points,
    endpoints — entry-points omitted entirely when there is no ``entry``
    layer) still render in that priority order, but each is fit against its
    OWN ``_water_fill``-allocated share of the remaining budget (BC-CODEGRAPH-9,
    Car 0087) rather than all four sharing one budget with a single tail-cut —
    the bug that let an early section (layers) starve a later one (endpoints)
    entirely. The ellipsis / "N of M shown" markers are counted in each
    section's budget, and a whole-blob ``_truncate`` safety net still runs on
    the assembled body if per-section allocation ever leaves the total over
    ``body_budget``, so ``len(result) <= budget`` holds unconditionally — the
    marker's survival is structural, not a side effect of the digest happening
    to be short.
    """
    limit = config.DIGEST_CHAR_BUDGET if budget is None else budget

    # Layer-noise guard: drop leaked route-path fragments (can carry PII),
    # builtins, and generic route fragments (task #58) BEFORE either reader
    # (_layers_data, _entry_points_data) sees them — single choke point,
    # see _filter_layer_noise / _is_layer_noise.
    architecture = _filter_layer_noise(architecture)

    preamble_sections: list[list[str]] = [
        [_header_line(architecture, identity)],
        _stale_line(identity),
    ]

    layers_header, layers_items, layers_total = _layers_data(architecture)
    hotspots_header, hotspots_items, hotspots_total = _hotspots_data(architecture)
    entry_data = _entry_points_data(architecture)
    end_header, end_items, end_total = _endpoints_data(endpoints)

    layers_full = _section_full_text(layers_header, layers_items, layers_total)
    hotspots_full = _section_full_text(hotspots_header, hotspots_items, hotspots_total)
    end_full = _section_full_text(end_header, end_items, end_total)
    entry_full = _csv_full_text(*entry_data) if entry_data is not None else ""

    # Secret-gate FP guard (#30): break long [A-Za-z0-9/+] runs (git SHAs, 40-char
    # identifier/path segments) so the digest can never coincidentally form the
    # gate's exactly-40 AWS-secret shape. Applied to BOTH halves — equivalent to
    # defanging the joined text, because "\n" is not in [A-Za-z0-9/+] so no run can
    # ever span the join. Applied BEFORE the budget arithmetic (defanging INSERTS
    # spaces, so it grows the text) and before truncation, so the ceiling holds.
    # See _defang_secret_shaped_runs — the gate itself is unchanged.
    preamble = _defang_secret_shaped_runs(_join_sections(preamble_sections))

    body_budget = limit - len(preamble) - 1  # -1 for the joining newline
    if body_budget <= len(_ELLIPSIS):
        # Degenerate: the budget cannot hold the preamble plus a meaningful cut of
        # the body (pathological budget, or an absurd repo_id / language list).
        # Fall back to truncating the whole assembled text exactly as before —
        # never per-section-fit with a negative or sub-ellipsis budget. All four
        # section texts are already defanged (per-line, at construction), so no
        # extra defang pass is needed here.
        body = "\n".join(s for s in (layers_full, hotspots_full, entry_full, end_full) if s)
        return _truncate(preamble + "\n" + body, limit)

    # Water-fill body_budget across the sections that are actually present
    # (entry-points is entirely absent, not zero-content, when there is no
    # `entry` layer — see _entry_points_data) so a section with nothing to say
    # never reserves budget it won't use.
    slots: list[tuple[int, Callable[[int], str]]] = [
        (len(layers_full), partial(_fit_line_section, layers_header, layers_items, layers_total)),
        (
            len(hotspots_full),
            partial(_fit_line_section, hotspots_header, hotspots_items, hotspots_total),
        ),
    ]
    if entry_data is not None:
        slots.append((len(entry_full), partial(_fit_csv_section, *entry_data)))
    slots.append((len(end_full), partial(_fit_line_section, end_header, end_items, end_total)))

    allocations = _water_fill([demand for demand, _render in slots], body_budget)
    body_parts = [
        render(alloc) for (_demand, render), alloc in zip(slots, allocations, strict=True)
    ]
    body = "\n".join(part for part in body_parts if part)

    # Safety net: per-section allocation is designed to sum to <= body_budget,
    # but never trust that in the return path — if rounding or an edge case
    # ever leaves the assembled body over budget, fall back to the same
    # whole-blob truncate the degenerate branch above uses.
    if len(body) > body_budget:
        body = _truncate(body, body_budget)

    return preamble + "\n" + body


@observe(tier="stage")
def _digest_directory(identity: dict[str, Any]) -> str:
    """Return the exact-match injection directory = canonical_root + subdir.

    Block injection scope is an EXACT ``str(directory)`` match against the
    session cwd, so a monorepo-leaf digest MUST be keyed to
    ``canonical_root/subdir`` (not the bare root) or it never injects.
    """
    root = str(identity.get("canonical_root", ""))
    subdir = str(identity.get("subdir", "") or "")
    if subdir:
        return str(Path(root) / subdir)
    return root


@observe(tier="stage")
def build_block_payload(
    architecture: dict[str, Any],
    endpoints: list[dict[str, Any]],
    identity: dict[str, Any],
    *,
    budget: int | None = None,
) -> dict[str, Any]:
    """Build the C→D seam payload: a block descriptor, NOT a block write.

    Returns ``{"block_name","directory","content","chars","skipped": False}``.
    Car D's stop-hook prompt hands this to Claude, who calls ``block_update``
    (gated).  The write is deliberately NOT done here (mirrors repo_wiki's
    ``wiki_add`` Claude-in-the-loop flow).

    ``directory`` = ``canonical_root`` joined with ``subdir`` (exact-match
    injection scope).  ``chars`` == ``len(content)`` and ≤ budget.
    """
    content = render_digest(architecture, endpoints, identity, budget=budget)
    return {
        "block_name": "code_graph",
        "directory": _digest_directory(identity),
        "content": content,
        "chars": len(content),
        "skipped": False,
    }
