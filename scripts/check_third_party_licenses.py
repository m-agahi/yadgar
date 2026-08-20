#!/usr/bin/env python3
"""Third-party-license ratchet — every bundled binary is catalogued and shipped.

WHY THIS EXISTS (the incident, verified 2026-08-20)
---------------------------------------------------
``Dockerfile.backend`` has carried

    COPY --from=surrealdb/surrealdb:v3.1.5 /surreal /usr/local/bin/surreal

for many releases.  SurrealDB is under the Business Source License 1.1, whose
own text says it "is not an Open Source license".  A bare-binary ``COPY --from``
brings nothing but the binary, so the upstream license does not travel with it —
and the source image cannot help either: a whole-filesystem listing of
``surrealdb/surrealdb:v3.1.5`` returns ZERO paths matching license/copyright/
notice.  Read-only inspection of the PUBLISHED ``openfantasy/yadgar-backend:
5.76.3`` confirmed the result: no license file anywhere for ``surreal``.

The same inspection is the reason apt packages are OUT of scope.  In that very
same published image, ``/usr/share/doc/mariadb-server/copyright`` (35 900 bytes)
and ``/usr/share/doc/curl/copyright`` (22 132 bytes) are both present —
``rm -rf /var/lib/apt/lists/*`` does not touch them.  That is the whole crux:
**an apt install carries its own license into the image; a bare-binary
``COPY --from`` and a ``curl``-ed release tarball do not.**

``Dockerfile.ci`` is the worst site and the easiest to wave off as "just CI":
it has no ``COPY . /app`` at all (only ``pyproject.toml`` + ``uv.lock``), it
bundles surreal AND gitleaks, and it is pulled from docker.io by every CI job —
i.e. distributed.

THE RULES
---------
**Rule 1 — every EXTERNAL reference is catalogued or allowlisted.**  This is
deliberately an inversion of the obvious design.  Matching "the ways we know a
binary gets bundled" (``COPY --from=``, ``curl … releases/download``) is a
PATTERN ALLOWLIST, and a pattern allowlist is a silent coverage hole, not a
false-positive source: a fourth mechanism (``ADD <url>``, ``wget``, a vendored
wheel, ``apk add`` in a Wolfi stage) would be invisible and a green run would
say nothing.  See the same argument, made about model-id prefixes, under "THE
SCAN SET IS THE DESIGN" in ``check_model_id_liveness.py``.

So the scan set is EVERY external reference instead:

  * every ``FROM <image>`` whose image is not a stage declared earlier in the
    same Dockerfile,
  * every ``COPY --from=<ref>`` whose ref is not such a stage (nor a stage
    index),
  * every ``https://`` / ``http://`` URL appearing inside a ``RUN`` instruction.

Each one must be either (a) matched to a ``components`` entry AT THE PINNED
VERSION, or (b) present in ``allowlist`` with a rationale of at least 40
characters.  Anything else FAILS.

Version-matching is what makes (a) do double duty.  A reference is covered by a
component only when it contains one of the component's ``match`` tokens AND the
component's recorded ``version``.  So bumping ``surrealdb/surrealdb:v3.1.5`` to
``v3.2.0`` without updating the manifest and THIRD-PARTY-LICENSES goes RED — and
because ``surrealdb`` is pinned independently in TWO Dockerfiles
(``Dockerfile.backend``'s image tag and ``Dockerfile.ci``'s ``SURREAL_VERSION``
ARG), bumping only one of them goes RED too.  One component, one recorded
version; the NOTICE can only tell one truth.

**Rule 2 — the verbatim text is byte-identical to what was recorded.**  Each
component's ``license_file`` must exist and its SHA-256 must equal the recorded
``license_sha256``.  A reflowed verbatim license is not verbatim, and nothing
else in this repository would signal that it happened.

Be precise about what the companion ``exclude:`` does, because it is easy to
overstate.  ``.pre-commit-config.yaml`` runs ``end-of-file-fixer``,
``trailing-whitespace`` and ``mixed-line-ending`` over the tree, and those three
now skip ``third-party/``.  MEASURED 2026-08-20, both current texts are already
hook-clean — no trailing whitespace on any line, a final newline present, no
CRLF — so the exclude corrects nothing today.  (The BSL's tabs are LEADING
indentation, which ``trailing-whitespace`` never touches.)  It is prophylactic
for a future text that is not so lucky: a CRLF upstream file, or one with no
final newline, would be rewritten on the commit that adds it.  This rule is what
makes the exclude being dropped visible either way.

**Rule 3 — every image ships the license artifacts.**  A file in the repository
is not a file in the image, and the images are the copies.  Every Dockerfile
must ``COPY`` all of ``ship_paths`` into the image, OR build ``FROM`` an
intra-repo image whose allowlist entry declares ``built_from`` pointing at a
Dockerfile that does.  That inheritance clause exists for ``Dockerfile.ci-viz``,
which is ``FROM openfantasy/yadgar-ci`` and receives surreal, gitleaks and the
license artifacts as inherited layers without a COPY of its own.

**Rule 4 — the prose cannot drift from the manifest.**  ``THIRD-PARTY-LICENSES``
must mention every component's name AND its version, and reference every
``license_file`` path.  The manifest is machine-read and the inventory is what a
human reads; nothing else holds them together.

**Rule 5 — no stale entries.**  A ``components`` entry whose ``match`` tokens
appear in no Dockerfile, or an ``allowlist`` key matching no external reference,
is a HARD ERROR.  This is the anti-rot rule, and it is the one that
``.ledger-chokepoint-allowlist.txt`` lacked.

═══════════════════════════════════════════════════════════════════════════════
THE CEILING — read this before trusting a green run
═══════════════════════════════════════════════════════════════════════════════
  * **A commit gate is not a publish gate.**  This runs at commit and in CI on
    PRs.  The obligation attaches when an image is PUBLISHED.  It is also wired
    into the release workflow ahead of the build-push step, but a hand-run
    ``docker build && docker push`` from a laptop bypasses both.
  * **Nothing here reads an actual image.**  Rule 3 checks that a ``COPY``
    instruction naming the artifacts exists in the Dockerfile.  It does not
    build the image and does not verify the files landed.  A ``.dockerignore``
    entry excluding ``third-party/`` would defeat it silently — which is why
    ``yadgar/tests/scripts/test_check_third_party_licenses.py`` asserts the
    ship_paths are absent from ``.dockerignore``.
  * **The license IDENTIFICATION is not verified.**  Rule 2 pins the bytes of
    the recorded text, not that those bytes are the right license for that
    component.  A wrong-but-stable license file passes forever.
  * **Transitive bundling inside an allowlisted base image is invisible.**  If
    ``python:3.14-slim`` began shipping a bare non-apt binary, nothing here
    would notice.
  * **Model weights are out of scope by decision, not oversight.**  The
    HuggingFace bakes in ``Dockerfile.backend`` and ``Dockerfile.ci`` download
    weights, each under its own model licence.  They are not enumerated.
  * **Python distributions are out of scope**; their license metadata rides
    along in each ``.dist-info`` inside the image.
  * **URL detection is textual.**  A URL assembled at build time from parts that
    never appear as a literal (or arriving via ``--build-arg``) is not seen.
  * **ARG resolution handles ``${NAME}``, ``$NAME`` and ``${NAME#prefix}``
    only.**  A more exotic shell expansion leaves the placeholder in place; the
    reference then fails to match any version and goes RED, which is the safe
    direction but reports a confusing reason.

Usage:
  python scripts/check_third_party_licenses.py                # check, exit 0/1
  python scripts/check_third_party_licenses.py --list-refs    # dump the scan set
  python scripts/check_third_party_licenses.py --repo-root /path

Exit codes:
  0  every external reference is catalogued or governed, texts are intact,
     every image ships the artifacts, and no entry is stale
  1  one or more UNCATALOGUED / VERSION-DRIFT / TEXT-DRIFT / NOT-SHIPPED /
     PROSE-DRIFT / STALE / MALFORMED violations
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

MANIFEST_NAME = ".third-party-manifest.json"
INVENTORY_NAME = "THIRD-PARTY-LICENSES"
_MIN_RATIONALE = 40

# Dockerfiles are discovered, not listed: a new Dockerfile must not be able to
# join the repo unnoticed by this guard (that is exactly the rot this exists to
# prevent). Glob at the repo root, where all four live today.
_DOCKERFILE_GLOB = "Dockerfile*"

_URL_RE = re.compile(r"https?://[^\s\"'\\)>;|]+")
_ARG_RE = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.IGNORECASE)
_FROM_RE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
_COPY_FROM_RE = re.compile(r"^COPY\s+.*?--from=(\S+)", re.IGNORECASE)
# ${NAME}, ${NAME#prefix} and bare $NAME.
_SUBST_BRACED_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:#([^}]*))?\}")
_SUBST_BARE_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class Ref:
    """One external reference found in a Dockerfile."""

    def __init__(self, dockerfile: str, lineno: int, kind: str, text: str) -> None:
        self.dockerfile = dockerfile
        self.lineno = lineno
        self.kind = kind  # "FROM" | "COPY --from" | "URL"
        self.text = text

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.dockerfile}:{self.lineno} [{self.kind}] {self.text}"


# ---------------------------------------------------------------------------
# Dockerfile parsing
# ---------------------------------------------------------------------------
def _strip_comment(line: str) -> str:
    """Drop a full-line Dockerfile comment.

    Only FULL-line comments are stripped. Dockerfile has no trailing-comment
    syntax, so a ``#`` mid-line is data (a URL fragment, a shell expansion like
    ``${VERSION#v}``) and removing it would corrupt the reference.
    """
    return "" if line.lstrip().startswith("#") else line


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations into logical instructions.

    Returns ``(1-based lineno of the instruction's first line, joined text)``.
    Comment-only physical lines inside a continuation are dropped, which is what
    keeps ``Dockerfile.ci``'s long commented RUN blocks from leaking prose URLs
    (e.g. the ``packages.debian.org`` link in a comment) into the scan set.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for i, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw)
        if not buf and not line.strip():
            continue
        if not buf:
            start = i
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        joined = " ".join(part.strip() for part in buf if part.strip())
        if joined:
            out.append((start, joined))
        buf = []
    if buf:
        joined = " ".join(part.strip() for part in buf if part.strip())
        if joined:
            out.append((start, joined))
    return out


def _substitute(text: str, args: dict[str, str]) -> str:
    """Resolve ``${NAME}``, ``${NAME#prefix}`` and ``$NAME`` from *args*."""

    def braced(m: re.Match[str]) -> str:
        name, prefix = m.group(1), m.group(2)
        if name not in args:
            return m.group(0)
        val = args[name]
        if prefix and val.startswith(prefix):
            val = val[len(prefix) :]
        return val

    text = _SUBST_BRACED_RE.sub(braced, text)
    return _SUBST_BARE_RE.sub(lambda m: args.get(m.group(1), m.group(0)), text)


def parse_dockerfile(name: str, text: str) -> list[Ref]:
    """Return every EXTERNAL reference in one Dockerfile.

    Intra-file build stages (``FROM x AS prod`` then ``FROM prod AS dev``, or
    ``COPY --from=0``) are resolved away — they are not external.
    """
    refs: list[Ref] = []
    args: dict[str, str] = {}
    stages: set[str] = set()

    for lineno, line in _logical_lines(text):
        m_arg = _ARG_RE.match(line)
        if m_arg:
            args[m_arg.group(1)] = m_arg.group(2).strip().strip("\"'")
            continue

        resolved = _substitute(line, args)

        m_from = _FROM_RE.match(resolved)
        if m_from:
            image, alias = m_from.group(1), m_from.group(2)
            if image.lower() not in stages:
                refs.append(Ref(name, lineno, "FROM", image))
            if alias:
                stages.add(alias.lower())
            continue

        m_copy = _COPY_FROM_RE.match(resolved)
        if m_copy:
            src = m_copy.group(1)
            if src.lower() not in stages and not src.isdigit():
                refs.append(Ref(name, lineno, "COPY --from", src))
            # fall through: a COPY line can carry no URL, but never skip the
            # URL sweep for other instruction kinds.

        if resolved.upper().startswith("RUN "):
            for url in _URL_RE.findall(resolved):
                refs.append(Ref(name, lineno, "URL", url.rstrip(".,;")))

    return refs


def copy_sources(text: str) -> set[str]:
    """Return every source path named by a ``COPY`` instruction.

    ``--from=`` copies are EXCLUDED: those pull out of another image, so they
    cannot be the instruction that ships this repository's license artifacts.
    """
    found: set[str] = set()
    for _lineno, line in _logical_lines(text):
        if not line.upper().startswith("COPY "):
            continue
        if "--from=" in line.lower():
            continue
        parts = [p for p in line.split()[1:] if not p.startswith("--")]
        # Last token is the destination.
        for src in parts[:-1]:
            found.add(src.strip("\"'").rstrip("/"))
    return found


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _covered_by(ref: Ref, components: dict) -> tuple[str | None, str | None]:
    """Return ``(component_name, None)`` if covered, or ``(name, reason)`` on drift.

    A reference is covered when it contains one of a component's ``match``
    tokens AND that component's recorded version. A token hit WITHOUT the
    version is version drift, reported against that component by name — the
    single most useful failure message this guard produces.
    """
    for cname, spec in components.items():
        for token in spec.get("match", []):
            if token in ref.text:
                if str(spec.get("version", "")) in ref.text:
                    return cname, None
                return cname, (
                    f"references {cname!r} but not its recorded version {spec.get('version')!r}"
                )
    return None, None


def _rule1_refs(
    refs: list[Ref], components: dict, allowlist: dict
) -> tuple[list[str], set[str], set[str]]:
    """Rule 1 — every external reference is catalogued or allowlisted.

    Also returns the sets of component / allowlist keys that matched something,
    which rule 5 consumes to find stale entries.
    """
    errors: list[str] = []
    hit_components: set[str] = set()
    hit_allowlist: set[str] = set()

    for ref in refs:
        allow_hit = next((k for k in allowlist if k in ref.text), None)
        if allow_hit:
            hit_allowlist.add(allow_hit)
            continue
        cname, reason = _covered_by(ref, components)
        if cname:
            hit_components.add(cname)
            if not reason:
                continue
            errors.append(
                f"VERSION-DRIFT: {ref.dockerfile}:{ref.lineno} [{ref.kind}] "
                f"{ref.text} — {reason}. Update {MANIFEST_NAME}, the verbatim "
                f"text under third-party/, and {INVENTORY_NAME}."
            )
            continue
        errors.append(
            f"UNCATALOGUED: {ref.dockerfile}:{ref.lineno} [{ref.kind}] {ref.text} "
            f"— add a `components` entry (with the verbatim license text) or an "
            f"`allowlist` entry with a rationale in {MANIFEST_NAME}."
        )
    return errors, hit_components, hit_allowlist


def _rule2_texts(repo_root: Path, components: dict) -> list[str]:
    """Rule 2 — each recorded verbatim text exists and is byte-identical."""
    errors: list[str] = []
    for cname, spec in components.items():
        for field in ("version", "license", "license_file", "license_sha256"):
            if not spec.get(field):
                errors.append(f"MALFORMED: component {cname!r} is missing {field!r}")
        rationale = spec.get("rationale", "")
        if len(rationale) < _MIN_RATIONALE:
            errors.append(
                f"MALFORMED: component {cname!r} rationale is "
                f"{len(rationale)} chars, need >= {_MIN_RATIONALE}"
            )
        lic_rel = spec.get("license_file")
        if not lic_rel:
            continue
        lic_path = repo_root / lic_rel
        if not lic_path.exists():
            errors.append(f"TEXT-DRIFT: component {cname!r} license_file {lic_rel} does not exist")
            continue
        actual = _sha256(lic_path)
        if actual != spec.get("license_sha256"):
            errors.append(
                f"TEXT-DRIFT: {lic_rel} sha256 is {actual}, manifest records "
                f"{spec.get('license_sha256')}. The verbatim text changed — if a "
                f"formatter rewrote it, restore it and check the pre-commit "
                f"`exclude:` for third-party/."
            )
    return errors


def _ships_everything(text: str, ship_paths: list[str]) -> list[str]:
    """Return the ship_paths this Dockerfile does NOT COPY into its image."""
    srcs = copy_sources(text)
    return [s for s in ship_paths if s.rstrip("/") not in srcs]


def _inherits_ship_paths(
    name: str, text: str, parsed: dict[str, str], allowlist: dict, ship_paths: list[str]
) -> bool:
    """True when this Dockerfile builds FROM an intra-repo image that ships them.

    The parent is RESOLVED, not assumed: its Dockerfile is re-checked, so a
    parent that stops shipping takes the child red with it.
    """
    for ref in parse_dockerfile(name, text):
        if ref.kind != "FROM":
            continue
        key = next((k for k in allowlist if k in ref.text), None)
        parent = allowlist.get(key, {}).get("built_from") if key else None
        if parent and parent in parsed and not _ships_everything(parsed[parent], ship_paths):
            return True
    return False


def _rule3_shipping(parsed: dict[str, str], allowlist: dict, ship_paths: list[str]) -> list[str]:
    """Rule 3 — every image carries the license artifacts."""
    errors: list[str] = []
    for name, text in sorted(parsed.items()):
        missing = _ships_everything(text, ship_paths)
        if not missing:
            continue
        if _inherits_ship_paths(name, text, parsed, allowlist, ship_paths):
            continue
        errors.append(
            f"NOT-SHIPPED: {name} does not COPY {missing} into the image, and "
            f"does not build FROM an intra-repo image that does. A license "
            f"file in the repo is not a license file in the image."
        )
    return errors


def _rule4_prose(repo_root: Path, components: dict) -> list[str]:
    """Rule 4 — the human-readable inventory tracks the machine-read manifest."""
    inv_path = repo_root / INVENTORY_NAME
    if not inv_path.exists():
        return [f"MALFORMED: missing {INVENTORY_NAME}"]
    inv = inv_path.read_text(encoding="utf-8")
    errors: list[str] = []
    for cname, spec in components.items():
        if cname not in inv:
            errors.append(f"PROSE-DRIFT: {INVENTORY_NAME} does not mention component {cname!r}")
        elif str(spec.get("version", "")) not in inv:
            errors.append(
                f"PROSE-DRIFT: {INVENTORY_NAME} does not record version "
                f"{spec.get('version')!r} for {cname!r}"
            )
        lic_rel = spec.get("license_file")
        if lic_rel and lic_rel not in inv:
            errors.append(f"PROSE-DRIFT: {INVENTORY_NAME} does not reference {lic_rel}")
    return errors


def _rule5_stale(
    components: dict, allowlist: dict, hit_components: set[str], hit_allowlist: set[str]
) -> list[str]:
    """Rule 5 — an entry matching nothing is a HARD ERROR, not dead weight."""
    errors: list[str] = []
    for cname in components:
        if cname not in hit_components:
            errors.append(
                f"STALE: component {cname!r} matches nothing in any Dockerfile — "
                f"it is no longer bundled. Remove it from {MANIFEST_NAME} and "
                f"{INVENTORY_NAME} (and delete its verbatim text)."
            )
    for key, spec in allowlist.items():
        rationale = spec.get("rationale", "")
        if len(rationale) < _MIN_RATIONALE:
            errors.append(
                f"MALFORMED: allowlist entry {key!r} rationale is "
                f"{len(rationale)} chars, need >= {_MIN_RATIONALE}"
            )
        if key not in hit_allowlist:
            errors.append(
                f"STALE: allowlist entry {key!r} matches no external reference in "
                f"any Dockerfile — remove it from {MANIFEST_NAME}."
            )
    return errors


def _load_manifest(repo_root: Path) -> tuple[dict | None, str | None]:
    """Read the manifest. Returns ``(manifest, error)`` — exactly one is None."""
    manifest_path = repo_root / MANIFEST_NAME
    if not manifest_path.exists():
        return None, f"MALFORMED: missing manifest {MANIFEST_NAME}"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"MALFORMED: {MANIFEST_NAME} is not valid JSON: {exc}"


def collect_refs(parsed: dict[str, str]) -> list[Ref]:
    """Flatten every external reference across every parsed Dockerfile."""
    refs: list[Ref] = []
    for name, text in parsed.items():
        refs.extend(parse_dockerfile(name, text))
    return refs


def check(repo_root: Path) -> tuple[bool, list[str]]:
    """Pure decision logic. Returns ``(ok, messages)``."""
    manifest, load_error = _load_manifest(repo_root)
    if manifest is None:
        return False, [load_error or "MALFORMED: manifest unreadable"]

    components: dict = manifest.get("components", {})
    allowlist: dict = manifest.get("allowlist", {})
    ship_paths: list[str] = manifest.get("ship_paths", [])

    dockerfiles = sorted(p for p in repo_root.glob(_DOCKERFILE_GLOB) if p.is_file())
    if not dockerfiles:
        return False, ["MALFORMED: no Dockerfile found at the repo root"]
    parsed: dict[str, str] = {p.name: p.read_text(encoding="utf-8") for p in dockerfiles}

    errors: list[str] = []
    if not ship_paths:
        errors.append("MALFORMED: manifest declares no ship_paths")

    ref_errors, hit_components, hit_allowlist = _rule1_refs(
        collect_refs(parsed), components, allowlist
    )
    errors += ref_errors
    errors += _rule2_texts(repo_root, components)
    errors += _rule3_shipping(parsed, allowlist, ship_paths)
    errors += _rule4_prose(repo_root, components)
    errors += _rule5_stale(components, allowlist, hit_components, hit_allowlist)

    return not errors, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo-root", default=str(_REPO_ROOT))
    ap.add_argument(
        "--list-refs",
        action="store_true",
        help="print every external reference found and exit 0",
    )
    ap.add_argument("filenames", nargs="*", help="ignored (pre-commit passes these)")
    args = ap.parse_args(argv)
    root = Path(args.repo_root).resolve()

    if args.list_refs:
        for p in sorted(root.glob(_DOCKERFILE_GLOB)):
            if not p.is_file():
                continue
            for ref in parse_dockerfile(p.name, p.read_text(encoding="utf-8")):
                print(ref)
        return 0

    ok, messages = check(root)
    if ok:
        print("third-party licenses OK — every bundled binary catalogued and shipped")
        return 0
    print("Third-party license check FAILED:\n", file=sys.stderr)
    for msg in messages:
        print(f"  {msg}", file=sys.stderr)
    print(
        f"\n{len(messages)} violation(s). See scripts/check_third_party_licenses.py for the rules.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
