"""Tests for scripts/check_model_id_liveness.py (task 0121, §5/§6.1).

Every case runs against a SYNTHETIC temp-dir tree (fake ``config.py`` + fake
``Dockerfile.backend`` + fake scan-set modules), never the real repo.  That is
deliberate: the real-tree day-one failures (``CROSS_ENCODER_MODEL``,
``NLI_MODEL``, ``COMET_MODEL``) are resolved by SEEDING the allowlist, so a test
asserting them against the real tree would self-destruct the moment the
allowlist lands.  The real-tree RED run is a one-off manual command whose output
goes in the PR body.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent


def _load(script_name: str):  # type: ignore[return]
    script_path = _REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(script_name.replace(".py", ""), script_path)
    assert spec and spec.loader, f"Cannot load {script_path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


guard = _load("check_model_id_liveness.py")


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------
_CONFIG_TMPL = '''\
"""Fake config module."""


class Settings:
{fields}
'''

_DOCKERFILE_TMPL = """\
FROM python:3.14
RUN python -c "\\
from sentence_transformers import SentenceTransformer, CrossEncoder; \\
{bakes}
print('baked')"
"""


def _write_tree(
    root: Path,
    fields: dict[str, str],
    baked: list[str],
    modules: dict[str, str] | None = None,
    allowlist: dict | None = None,
) -> Path:
    """Build a synthetic repo root the guard can be pointed at."""
    cfg_dir = root / "yadgar" / "_shared" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"    {k}: str = {v!r}" for k, v in fields.items()) or "    pass"
    (cfg_dir / "config.py").write_text(_CONFIG_TMPL.format(fields=body), encoding="utf-8")

    bakes = "\n".join(f"CrossEncoder('{b}'); \\" for b in baked)
    (root / "Dockerfile.backend").write_text(_DOCKERFILE_TMPL.format(bakes=bakes), encoding="utf-8")

    ml_dir = root / "yadgar" / "backend" / "ml_client"
    ml_dir.mkdir(parents=True, exist_ok=True)
    (ml_dir / "__init__.py").write_text("", encoding="utf-8")
    for rel, src in (modules or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")

    if allowlist is not None:
        (root / guard.ALLOWLIST_NAME).write_text(json.dumps(allowlist), encoding="utf-8")
    return root


_LONG = "a rationale long enough to clear the forty character minimum bar"


# ---------------------------------------------------------------------------
# Rule 1 — every *_MODEL default is baked or allowlisted
# ---------------------------------------------------------------------------
class TestRule1:
    def test_baked_fields_pass_with_no_allowlist(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {
                "EMBEDDING_MODEL": "all-MiniLM-L6-v2",
                "GTE_RERANKER_MODEL": "cross-encoder/ettin-32m",
            },
            ["all-MiniLM-L6-v2", "cross-encoder/ettin-32m"],
        )
        assert guard.check(tmp_path) == []

    def test_unbaked_field_fails_naming_the_field(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"CROSS_ENCODER_MODEL": "cross-encoder/ms-marco"}, ["other-model"])
        errors = guard.check(tmp_path)
        assert guard.unbaked_fields(errors) == {"CROSS_ENCODER_MODEL"}

    def test_asserts_the_failing_SET_not_a_count(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {
                "EMBEDDING_MODEL": "all-MiniLM-L6-v2",
                "CROSS_ENCODER_MODEL": "cross-encoder/ms-marco-MiniLM-L-6-v2",
                "NLI_MODEL": "cross-encoder/nli-deberta-v3-base",
                "COMET_MODEL": "mismayil/comet-bart-ai2",
            },
            ["all-MiniLM-L6-v2"],
        )
        errors = guard.check(tmp_path)
        assert guard.unbaked_fields(errors) == {
            "CROSS_ENCODER_MODEL",
            "NLI_MODEL",
            "COMET_MODEL",
        }

    def test_empty_default_is_skipped_not_failed(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"IMPLICIT_EMBEDDING_MODEL": ""}, ["all-MiniLM-L6-v2"])
        assert guard.check(tmp_path) == []

    def test_org_prefix_is_normalised_away(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"X_MODEL": "some-org/all-MiniLM-L6-v2"}, ["all-MiniLM-L6-v2"])
        assert guard.check(tmp_path) == []

    def test_allowlisted_field_passes(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"CROSS_ENCODER_MODEL": "cross-encoder/ms-marco"},
            ["other-model"],
            allowlist={"fields": {"CROSS_ENCODER_MODEL": {"rationale": _LONG}}},
        )
        assert guard.check(tmp_path) == []

    def test_short_rationale_is_rejected(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"CROSS_ENCODER_MODEL": "cross-encoder/ms-marco"},
            ["other-model"],
            allowlist={"fields": {"CROSS_ENCODER_MODEL": {"rationale": "too short"}}},
        )
        errors = guard.check(tmp_path)
        assert any("MALFORMED" in e and "CROSS_ENCODER_MODEL" in e for e in errors)

    def test_stale_entry_for_now_baked_field_is_hard_error(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"CROSS_ENCODER_MODEL": "cross-encoder/ms-marco"},
            ["cross-encoder/ms-marco"],
            allowlist={"fields": {"CROSS_ENCODER_MODEL": {"rationale": _LONG}}},
        )
        errors = guard.check(tmp_path)
        assert any("STALE" in e and "CROSS_ENCODER_MODEL" in e for e in errors)

    def test_stale_entry_for_nonexistent_field_is_hard_error(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"EMBEDDING_MODEL": "all-MiniLM-L6-v2"},
            ["all-MiniLM-L6-v2"],
            allowlist={"fields": {"GONE_MODEL": {"rationale": _LONG}}},
        )
        errors = guard.check(tmp_path)
        assert any("STALE" in e and "GONE_MODEL" in e for e in errors)


# ---------------------------------------------------------------------------
# Rule 2 — no orphan model-id literal in the scan set
# ---------------------------------------------------------------------------
_ORPHAN_MOD = '''\
"""Loader."""

from sentence_transformers import CrossEncoder


def load():
    return CrossEncoder("cross-encoder/nli-deberta-v3-small")
'''

_IN_VOCAB_MOD = '''\
"""Loader."""

from sentence_transformers import CrossEncoder


def load():
    return CrossEncoder("cross-encoder/ettin-32m")
'''


class TestRule2:
    def test_orphan_literal_fails(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"NLI_MODEL": "cross-encoder/nli-deberta-v3-base"},
            ["cross-encoder/nli-deberta-v3-base"],
            modules={"yadgar/backend/ml_client/local_ml_client.py": _ORPHAN_MOD},
        )
        errors = guard.check(tmp_path)
        assert guard.orphan_literals(errors) == {"cross-encoder/nli-deberta-v3-small"}

    def test_literal_in_vocabulary_passes(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"GTE_RERANKER_MODEL": "cross-encoder/ettin-32m"},
            ["cross-encoder/ettin-32m"],
            modules={"yadgar/backend/ml_client/local_ml_client.py": _IN_VOCAB_MOD},
        )
        assert guard.check(tmp_path) == []

    def test_allowlisted_literal_passes(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"NLI_MODEL": "cross-encoder/nli-deberta-v3-base"},
            ["cross-encoder/nli-deberta-v3-base"],
            modules={"yadgar/backend/ml_client/local_ml_client.py": _ORPHAN_MOD},
            allowlist={"literals": {"cross-encoder/nli-deberta-v3-small": {"rationale": _LONG}}},
        )
        assert guard.check(tmp_path) == []

    def test_stale_literal_entry_is_hard_error(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"GTE_RERANKER_MODEL": "cross-encoder/ettin-32m"},
            ["cross-encoder/ettin-32m"],
            modules={"yadgar/backend/ml_client/local_ml_client.py": _IN_VOCAB_MOD},
            allowlist={"literals": {"never-seen/anywhere": {"rationale": _LONG}}},
        )
        errors = guard.check(tmp_path)
        assert any("STALE" in e and "never-seen/anywhere" in e for e in errors)

    def test_scan_set_excludes_a_module_with_no_ml_import(self, tmp_path: Path) -> None:
        """§5.3's FP floor: an HTTP module carrying "application/json" is out of scope.

        The naive whole-repo variant of rule 2's regex picks up ~50 MIME/ref
        literals; the module narrowing is the reason this guard needs no MIME
        exclusion list.
        """
        _write_tree(
            tmp_path,
            {"EMBEDDING_MODEL": "all-MiniLM-L6-v2"},
            ["all-MiniLM-L6-v2"],
            modules={
                "yadgar/server/http.py": (
                    'HEADERS = {"Content-Type": "application/json"}\nREF = "origin/master"\n'
                )
            },
        )
        assert guard.check(tmp_path) == []
        scan = guard.build_scan_set(tmp_path)
        assert tmp_path / "yadgar" / "server" / "http.py" not in scan

    def test_tests_and_config_py_are_out_of_scan_set(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"NLI_MODEL": "cross-encoder/nli-deberta-v3-base"},
            ["cross-encoder/nli-deberta-v3-base"],
            modules={"yadgar/tests/backend/test_x.py": _ORPHAN_MOD},
        )
        assert guard.check(tmp_path) == []
        scan = guard.build_scan_set(tmp_path)
        assert tmp_path / "yadgar" / "_shared" / "config" / "config.py" not in scan

    def test_embed_service_package_is_in_scan_set(self, tmp_path: Path) -> None:
        """§5.3's stated blind-spot mitigation: embed_service/** is unconditional."""
        _write_tree(
            tmp_path,
            {"EMBEDDING_MODEL": "all-MiniLM-L6-v2"},
            ["all-MiniLM-L6-v2"],
            modules={
                "yadgar/backend/embed_service/embed_service_config.py": (
                    'FALLBACK = "cross-encoder/orphan-v1"\n'
                )
            },
        )
        errors = guard.check(tmp_path)
        assert guard.orphan_literals(errors) == {"cross-encoder/orphan-v1"}


# ---------------------------------------------------------------------------
# Allowlist plumbing
# ---------------------------------------------------------------------------
class TestAllowlist:
    def test_malformed_json_is_a_hard_error(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"EMBEDDING_MODEL": "all-MiniLM-L6-v2"}, ["all-MiniLM-L6-v2"])
        (tmp_path / guard.ALLOWLIST_NAME).write_text("{not json", encoding="utf-8")
        errors = guard.check(tmp_path)
        assert any("MALFORMED allowlist" in e for e in errors)

    def test_underscore_keys_are_comments_not_entries(self, tmp_path: Path) -> None:
        _write_tree(
            tmp_path,
            {"EMBEDDING_MODEL": "all-MiniLM-L6-v2"},
            ["all-MiniLM-L6-v2"],
            allowlist={"_comment": "governance blurb", "fields": {}, "literals": {}},
        )
        assert guard.check(tmp_path) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCli:
    def test_main_returns_1_on_violation(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"CROSS_ENCODER_MODEL": "cross-encoder/ms-marco"}, ["other"])
        assert guard.main(["--repo-root", str(tmp_path)]) == 1

    def test_main_returns_0_when_clean(self, tmp_path: Path) -> None:
        _write_tree(tmp_path, {"EMBEDDING_MODEL": "all-MiniLM-L6-v2"}, ["all-MiniLM-L6-v2"])
        assert guard.main(["--repo-root", str(tmp_path)]) == 0

    def test_real_repo_tree_is_clean(self) -> None:
        """The shipped tree + shipped allowlist must be green."""
        assert guard.check() == []
