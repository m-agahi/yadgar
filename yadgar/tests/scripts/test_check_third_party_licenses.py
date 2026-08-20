"""Tests for scripts/check_third_party_licenses.py (ledger task 69).

MUTATION DISCIPLINE. This repo has a documented recurring failure — guards that
report OK while checking nothing (wiki page
``guards-and-tooling-in-this-repo-that-do-not-check-what-their-nam``). A guard
that passes on a tree missing every license is worthless, so the synthetic cases
below assert BOTH directions for each rule: a clean tree is green, and each
single mutation is red WITH the expected violation class.

Most cases run against a SYNTHETIC temp-dir tree, never the real repo, so they
survive a future manifest change. The exceptions are the three real-tree cases
at the bottom, which pin properties of THIS repository that the synthetic tree
cannot express: that the guard is green on master, that the surreal pin agrees
across the two Dockerfiles that carry it, and that .dockerignore does not
exclude the artifacts the Dockerfiles now COPY.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):  # type: ignore[return]
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


guard = _load("check_third_party_licenses.py")

_RATIONALE = "x" * 60
_LICENSE_TEXT = "Fake License 1.0\n\nDo whatever.\n"
_LICENSE_SHA = hashlib.sha256(_LICENSE_TEXT.encode()).hexdigest()

_SHIP = "COPY LICENSE NOTICE THIRD-PARTY-LICENSES /usr/share/doc/yadgar/\nCOPY third-party /usr/share/doc/yadgar/third-party\n"

_DOCKERFILE = """\
FROM python:3.14-slim AS prod
RUN apt-get update && apt-get install -y curl
COPY --from=widget/widget:v2.5.0 /widget /usr/local/bin/widget
{ship}FROM prod AS dev
RUN pip install -e .
"""

_INVENTORY = """\
THIRD-PARTY LICENSES

widget
Version bundled: 2.5.0
Verbatim text: third-party/widget-2.5.0.LICENSE
"""


def _manifest(**over) -> dict:
    m = {
        "ship_paths": ["LICENSE", "NOTICE", "THIRD-PARTY-LICENSES", "third-party"],
        "components": {
            "widget": {
                "version": "2.5.0",
                "license": "MIT",
                "license_file": "third-party/widget-2.5.0.LICENSE",
                "license_sha256": _LICENSE_SHA,
                "upstream": "https://github.com/widget/widget",
                "match": ["widget/widget"],
                "rationale": _RATIONALE,
            }
        },
        "allowlist": {
            "python:3.14-slim": {"kind": "base-image", "rationale": _RATIONALE},
        },
    }
    m.update(over)
    return m


def _write_tree(
    root: Path,
    dockerfiles: dict[str, str] | None = None,
    manifest: dict | None = None,
    inventory: str | None = None,
    license_text: str = _LICENSE_TEXT,
) -> Path:
    """Build a synthetic repo root the guard can be pointed at."""
    dockerfiles = dockerfiles or {"Dockerfile": _DOCKERFILE.format(ship=_SHIP)}
    for name, text in dockerfiles.items():
        (root / name).write_text(text, encoding="utf-8")
    (root / "third-party").mkdir(exist_ok=True)
    (root / "third-party" / "widget-2.5.0.LICENSE").write_text(license_text, encoding="utf-8")
    (root / ".third-party-manifest.json").write_text(
        json.dumps(manifest if manifest is not None else _manifest()), encoding="utf-8"
    )
    (root / "THIRD-PARTY-LICENSES").write_text(
        _INVENTORY if inventory is None else inventory, encoding="utf-8"
    )
    return root


def _classes(messages: list[str]) -> set[str]:
    return {m.split(":", 1)[0] for m in messages}


# ---------------------------------------------------------------------------
# Mutation matrix — clean is green, each single mutation is red
# ---------------------------------------------------------------------------
def test_clean_tree_is_green(tmp_path: Path) -> None:
    """(d) The baseline must pass, or every red below proves nothing."""
    ok, msgs = guard.check(_write_tree(tmp_path))
    assert ok, msgs


def test_uncatalogued_bundled_binary_is_red(tmp_path: Path) -> None:
    """(a) The brief's case: a fake bundled binary with no manifest entry."""
    df = _DOCKERFILE.format(ship=_SHIP).replace(
        "RUN pip install -e .",
        "COPY --from=sneaky/sneaky:v9.9.9 /sneaky /usr/local/bin/sneaky",
    )
    ok, msgs = guard.check(_write_tree(tmp_path, dockerfiles={"Dockerfile": df}))
    assert not ok
    assert "UNCATALOGUED" in _classes(msgs)
    assert any("sneaky/sneaky:v9.9.9" in m for m in msgs)


def test_removing_the_fake_binary_returns_to_green(tmp_path: Path) -> None:
    """The brief's explicit round-trip: remove it, confirm green again."""
    ok, msgs = guard.check(_write_tree(tmp_path))
    assert ok, msgs


def test_version_drift_is_red(tmp_path: Path) -> None:
    """(b) Dockerfile pin bumped, manifest/inventory left behind."""
    df = _DOCKERFILE.format(ship=_SHIP).replace("widget:v2.5.0", "widget:v3.0.0")
    ok, msgs = guard.check(_write_tree(tmp_path, dockerfiles={"Dockerfile": df}))
    assert not ok
    assert "VERSION-DRIFT" in _classes(msgs)
    assert any("2.5.0" in m for m in msgs)


def test_missing_ship_copy_is_red(tmp_path: Path) -> None:
    """(c) The scope-item-3 regression guard: bundles, but ships no license."""
    df = _DOCKERFILE.format(ship="")
    ok, msgs = guard.check(_write_tree(tmp_path, dockerfiles={"Dockerfile": df}))
    assert not ok
    assert "NOT-SHIPPED" in _classes(msgs)


def test_partial_ship_copy_is_red(tmp_path: Path) -> None:
    """COPYing LICENSE but not the third-party texts is not compliance."""
    df = _DOCKERFILE.format(ship="COPY LICENSE /usr/share/doc/yadgar/\n")
    ok, msgs = guard.check(_write_tree(tmp_path, dockerfiles={"Dockerfile": df}))
    assert not ok
    assert "NOT-SHIPPED" in _classes(msgs)


def test_reflowed_verbatim_text_is_red(tmp_path: Path) -> None:
    """A formatter stripping trailing whitespace must be caught, not absorbed."""
    ok, msgs = guard.check(_write_tree(tmp_path, license_text=_LICENSE_TEXT + "\n"))
    assert not ok
    assert "TEXT-DRIFT" in _classes(msgs)


def test_missing_verbatim_file_is_red(tmp_path: Path) -> None:
    root = _write_tree(tmp_path)
    (root / "third-party" / "widget-2.5.0.LICENSE").unlink()
    ok, msgs = guard.check(root)
    assert not ok
    assert "TEXT-DRIFT" in _classes(msgs)


def test_inventory_prose_drift_is_red(tmp_path: Path) -> None:
    """Manifest updated, human-readable inventory not."""
    ok, msgs = guard.check(_write_tree(tmp_path, inventory="THIRD-PARTY LICENSES\n\nnothing\n"))
    assert not ok
    assert "PROSE-DRIFT" in _classes(msgs)


def test_stale_component_is_red(tmp_path: Path) -> None:
    """A component nothing bundles any more must be removed, not left to rot."""
    m = _manifest()
    m["components"]["ghost"] = {
        "version": "1.0.0",
        "license": "MIT",
        "license_file": "third-party/widget-2.5.0.LICENSE",
        "license_sha256": _LICENSE_SHA,
        "match": ["ghost/ghost"],
        "rationale": _RATIONALE,
    }
    ok, msgs = guard.check(_write_tree(tmp_path, manifest=m))
    assert not ok
    assert "STALE" in _classes(msgs)


def test_stale_allowlist_entry_is_red(tmp_path: Path) -> None:
    m = _manifest()
    m["allowlist"]["debian:bookworm"] = {"kind": "base-image", "rationale": _RATIONALE}
    ok, msgs = guard.check(_write_tree(tmp_path, manifest=m))
    assert not ok
    assert "STALE" in _classes(msgs)


def test_short_rationale_is_red(tmp_path: Path) -> None:
    """An allowlist without a real reason is a list, and lists rot."""
    m = _manifest()
    m["allowlist"]["python:3.14-slim"]["rationale"] = "because"
    ok, msgs = guard.check(_write_tree(tmp_path, manifest=m))
    assert not ok
    assert "MALFORMED" in _classes(msgs)


def test_allowlisting_a_bundled_binary_is_accepted_with_rationale(tmp_path: Path) -> None:
    """The escape hatch works — but only with a rationale in the diff."""
    m = _manifest()
    del m["components"]["widget"]
    m["allowlist"]["widget/widget"] = {"kind": "reviewed", "rationale": _RATIONALE}
    ok, msgs = guard.check(_write_tree(tmp_path, manifest=m, inventory="THIRD-PARTY LICENSES\n"))
    assert ok, msgs


# ---------------------------------------------------------------------------
# Inheritance (Dockerfile.ci-viz shape)
# ---------------------------------------------------------------------------
def test_child_image_inherits_ship_paths_from_intra_repo_parent(tmp_path: Path) -> None:
    """A FROM-our-own-image child needs no redundant COPY."""
    m = _manifest()
    m["allowlist"]["openfantasy/parent"] = {
        "kind": "intra-repo-image",
        "built_from": "Dockerfile",
        "rationale": _RATIONALE,
    }
    dfs = {
        "Dockerfile": _DOCKERFILE.format(ship=_SHIP),
        "Dockerfile.child": "FROM docker.io/openfantasy/parent:1.0.0\nRUN echo hi\n",
    }
    ok, msgs = guard.check(_write_tree(tmp_path, dockerfiles=dfs, manifest=m))
    assert ok, msgs


def test_child_goes_red_when_the_parent_stops_shipping(tmp_path: Path) -> None:
    """Inheritance is resolved, not assumed — break the parent, both go red."""
    m = _manifest()
    m["allowlist"]["openfantasy/parent"] = {
        "kind": "intra-repo-image",
        "built_from": "Dockerfile",
        "rationale": _RATIONALE,
    }
    dfs = {
        "Dockerfile": _DOCKERFILE.format(ship=""),
        "Dockerfile.child": "FROM docker.io/openfantasy/parent:1.0.0\nRUN echo hi\n",
    }
    ok, msgs = guard.check(_write_tree(tmp_path, dockerfiles=dfs, manifest=m))
    assert not ok
    assert sum("NOT-SHIPPED" in m_ for m_ in msgs) == 2


# ---------------------------------------------------------------------------
# Parser properties
# ---------------------------------------------------------------------------
def test_intra_file_stages_are_not_external() -> None:
    """`FROM prod AS dev` is a stage, not a third party."""
    refs = guard.parse_dockerfile("Dockerfile", _DOCKERFILE.format(ship=_SHIP))
    assert not any(r.text == "prod" for r in refs)


def test_comment_urls_are_not_scanned() -> None:
    """Prose links in comments must not enter the scan set."""
    text = "# see https://packages.debian.org/bookworm/nodejs\nFROM python:3.14-slim\n"
    refs = guard.parse_dockerfile("D", text)
    assert not any("packages.debian.org" in r.text for r in refs)


def test_arg_substitution_resolves_pinned_versions() -> None:
    """A version behind an ARG must still be seen, incl. `${NAME#v}`."""
    text = (
        "FROM python:3.14-slim\n"
        "ARG TOOL_VERSION=v8.30.1\n"
        'RUN curl -fsSL "https://example.com/t/${TOOL_VERSION}/t_${TOOL_VERSION#v}.tgz" -o /t.tgz\n'
    )
    urls = [r.text for r in guard.parse_dockerfile("D", text) if r.kind == "URL"]
    assert urls == ["https://example.com/t/v8.30.1/t_8.30.1.tgz"]


def test_label_urls_are_not_scanned() -> None:
    """Only RUN instructions carry fetches; a LABEL URL is metadata."""
    text = 'FROM python:3.14-slim\nLABEL org.opencontainers.image.source="https://github.com/m-agahi/yadgar"\n'
    assert not any(r.kind == "URL" for r in guard.parse_dockerfile("D", text))


# ---------------------------------------------------------------------------
# Real-tree properties — things only THIS repo can assert
# ---------------------------------------------------------------------------
def test_real_repo_is_green() -> None:
    ok, msgs = guard.check(_REPO_ROOT)
    assert ok, "real tree violates the third-party license ratchet:\n" + "\n".join(msgs)


def test_surreal_pin_agrees_across_both_dockerfiles() -> None:
    """One component, one version. The two pins are independent and can drift."""
    backend = (_REPO_ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    ci = (_REPO_ROOT / "Dockerfile.ci").read_text(encoding="utf-8")
    manifest = json.loads((_REPO_ROOT / ".third-party-manifest.json").read_text(encoding="utf-8"))
    version = manifest["components"]["surrealdb"]["version"]
    assert f"surrealdb/surrealdb:v{version}" in backend
    assert f"SURREAL_VERSION=v{version}" in ci


@pytest.mark.parametrize("artifact", ["LICENSE", "NOTICE", "THIRD-PARTY-LICENSES", "third-party"])
def test_dockerignore_does_not_exclude_the_shipped_artifacts(artifact: str) -> None:
    """The guard reads Dockerfiles, not images. .dockerignore could defeat it."""
    ignore = (_REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = [ln.strip() for ln in ignore.splitlines() if ln.strip() and not ln.startswith("#")]
    assert artifact not in patterns


def test_surreal_license_is_the_business_source_license() -> None:
    """Pin the identity of the text, not only its bytes (rule 2's blind spot)."""
    text = (_REPO_ROOT / "third-party" / "surrealdb-3.1.5.LICENSE").read_text(encoding="utf-8")
    assert text.startswith("Business Source License 1.1")
    assert "Change Date:          2030-01-01" in text
    assert "Change License:       Apache License, Version 2.0" in text
    # The sentence is line-wrapped in the source; asserting the unwrapped form
    # is what first caught a paraphrase of it in NOTICE.
    assert "is not an Open\nSource license." in text
    assert "you may not use the Licensed Work as a Database" in text


_QUOTE_BEGIN = "--- BEGIN VERBATIM QUOTE: "
_QUOTE_END = "--- END VERBATIM QUOTE ---"


def _verbatim_blocks(body: str) -> list[tuple[str, list[str]]]:
    """Return ``(named license_file, block lines)`` for each delimited quote."""
    blocks: list[tuple[str, list[str]]] = []
    source: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith(_QUOTE_BEGIN):
            source = line[len(_QUOTE_BEGIN) :].removesuffix(" ---").strip()
            lines = []
        elif line.strip() == _QUOTE_END:
            assert source is not None, "END VERBATIM QUOTE with no BEGIN"
            blocks.append((source, lines))
            source = None
        elif source is not None:
            lines.append(line)
    assert source is None, "BEGIN VERBATIM QUOTE with no END"
    return blocks


@pytest.mark.parametrize("doc", ["NOTICE", "THIRD-PARTY-LICENSES"])
def test_quoted_excerpts_are_verbatim(doc: str) -> None:
    """Every line presented as quoted license text must actually be in that text.

    The failure this exists for is real and happened while writing these files: a
    NOTICE paragraph re-punctuated ``"License"`` to ``'License'`` inside what it
    called a verbatim quote, and re-wrapped a line. Nothing but this check would
    have noticed — the guard's SHA-256 rule pins the third-party/ file, not the
    prose that claims to quote it.

    Quotes are EXPLICITLY DELIMITED rather than inferred from indentation. The
    first version of this test guessed ("indented 4+ spaces and containing no
    path separator") and was measurably wrong: a fabricated clause carrying a URL
    was appended to THIRD-PARTY-LICENSES and the test stayed GREEN, because the
    path heuristic exempted it. An inferred boundary is a coverage hole in a
    check whose entire job is to be exact, so the documents now state where their
    quotes begin and end and this reads only those regions.
    """
    body = (_REPO_ROOT / doc).read_text(encoding="utf-8")
    blocks = _verbatim_blocks(body)
    assert blocks, f"{doc} declares no verbatim quote blocks"
    for source, lines in blocks:
        known = {
            ln.strip() for ln in (_REPO_ROOT / source).read_text(encoding="utf-8").splitlines()
        }
        offenders = [ln for ln in lines if ln.strip() and ln.strip() not in known]
        assert not offenders, (
            f"{doc} quotes these as verbatim from {source}, but they are not in it:\n"
            + "\n".join(offenders)
        )


def test_the_verbatim_quote_check_detects_a_fabricated_line(tmp_path: Path) -> None:
    """Mutation-check the check above — its predecessor passed a fabrication."""
    lic = tmp_path / "fake.LICENSE"
    lic.write_text("Real clause one.\nReal clause two.\n", encoding="utf-8")
    doc = (
        f"{_QUOTE_BEGIN}fake.LICENSE ---\n"
        "Real clause one.\n"
        "Invented clause with a path https://example.com/fake\n"
        f"{_QUOTE_END}\n"
    )
    blocks = _verbatim_blocks(doc)
    assert len(blocks) == 1
    known = {ln.strip() for ln in lic.read_text(encoding="utf-8").splitlines()}
    offenders = [ln for ln in blocks[0][1] if ln.strip() and ln.strip() not in known]
    assert offenders == ["Invented clause with a path https://example.com/fake"]
