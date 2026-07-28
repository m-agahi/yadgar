"""code_graph digest renderer — architecture dict → compact markdown block.

Car C of the code_graph train (ADR-0162).

``render_digest`` is a PURE function (no I/O): it turns the
``get_architecture(aspects=["all"])`` dict + endpoint rows + repo identity into a
compact, deterministic markdown digest bounded by ``DIGEST_CHAR_BUDGET``.  The
digest is what Car D injects into a yadgar memory BLOCK (always-injected at
SessionStart, recall-free).

Priority order (kept when truncating to budget):
    header > layers > hotspots > entry-points > endpoints > stale-line.

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
guard that path before ``_layers_section``/``_entry_points_section`` render it.
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

    Single choke point: both ``_layers_section`` and ``_entry_points_section``
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
def _layers_section(architecture: dict[str, Any]) -> list[str]:
    """Render the ``layers:`` section: ``<layer>: <name> — <reason>`` lines.

    Sorted by (layer, name) so equal layers keep a stable order (tie-break).
    ``entry``-layer rows are ALSO surfaced as entry-points elsewhere; they stay
    in the layers list too (the two views serve different questions).
    """
    rows = []
    for row in architecture.get("layers") or []:
        name = str(row.get("name", ""))
        layer = str(row.get("layer", ""))
        reason = str(row.get("reason", ""))
        if name or layer:
            rows.append((layer, name, reason))
    rows.sort(key=lambda r: (r[0], r[1]))

    lines = ["layers:"]
    for layer, name, reason in rows[:_MAX_LAYERS]:
        suffix = f" — {reason}" if reason else ""
        lines.append(f"  {layer}: {name}{suffix}")
    return lines


@observe(tier="stage")
def _entry_points_section(architecture: dict[str, Any]) -> list[str]:
    """Render entry-points, DERIVED from ``layers`` where ``layer == 'entry'``.

    There is no ``entry_points`` key in the measured architecture shape — entry
    points are the ``entry`` layer.  Empty → no section (skipped, not blocking).
    """
    names = sorted(
        str(row.get("name", ""))
        for row in architecture.get("layers") or []
        if str(row.get("layer", "")) == "entry" and row.get("name")
    )
    if not names:
        return []
    return ["entry-points: " + ", ".join(names[:_MAX_ENTRY_POINTS])]


@observe(tier="stage")
def _hotspots_section(architecture: dict[str, Any]) -> list[str]:
    """Render top hotspots by fan_in, tie-broken by qualified_name.

    Sort key ``(-fan_in, qualified_name)`` — equal-fan_in hotspots keep a stable
    order (ADR-0108/0147: an un-tie-broken sort is exactly the flake class).
    """
    rows = []
    for row in architecture.get("hotspots") or []:
        qname = str(row.get("qualified_name") or row.get("name") or "")
        fan_in = int(row.get("fan_in", 0) or 0)
        if qname:
            rows.append((fan_in, qname))
    rows.sort(key=lambda r: (-r[0], r[1]))

    lines = ["hotspots:"]
    for fan_in, qname in rows[:_MAX_HOTSPOTS]:
        lines.append(f"  {qname} (fan_in={fan_in})")
    return lines


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
def _endpoints_section(endpoints: list[dict[str, Any]]) -> list[str]:
    """Render the ``endpoints:`` section from route_method rows.

    0 rows (PHP/Go framework routes not parsed) → ``(none extracted)``.  Sorted
    by (path, method) for determinism.
    """
    parsed = []
    for row in endpoints or []:
        method, path, _name = _extract_endpoint(row)
        if method or path:
            parsed.append((path, method))
    if not parsed:
        return ["endpoints: (none extracted)"]

    parsed = sorted(set(parsed))
    lines = ["endpoints:"]
    for path, method in parsed[:_MAX_ENDPOINTS]:
        lines.append(f"  {method} {path}".rstrip())
    return lines


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

    Sections in priority order: header > layers > hotspots > entry-points >
    endpoints > stale.  When over ``budget`` the tail is truncated with an
    ellipsis marker counted in the budget, so ``len(result) <= budget``.
    """
    limit = config.DIGEST_CHAR_BUDGET if budget is None else budget

    # Layer-noise guard: drop leaked route-path fragments (can carry PII),
    # builtins, and generic route fragments (task #58) BEFORE either reader
    # (_layers_section, _entry_points_section) sees them — single choke point,
    # see _filter_layer_noise / _is_layer_noise.
    architecture = _filter_layer_noise(architecture)

    sections: list[list[str]] = [
        [_header_line(architecture, identity)],
        _layers_section(architecture),
        _hotspots_section(architecture),
        _entry_points_section(architecture),
        _endpoints_section(endpoints),
        _stale_line(identity),
    ]

    lines: list[str] = []
    for section in sections:
        lines.extend(section)

    text = "\n".join(line for line in lines if line)
    # Secret-gate FP guard (#30): break long [A-Za-z0-9/+] runs (git SHAs, 40-char
    # identifier/path segments) so the digest can never coincidentally form the
    # gate's exactly-40 AWS-secret shape. Applied BEFORE truncation so the budget
    # ceiling still holds. See _defang_secret_shaped_runs — the gate is unchanged.
    text = _defang_secret_shaped_runs(text)
    return _truncate(text, limit)


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
