"""Self-test for scripts/check_swallowed_failure.py (ADR-0420, ledger task 287).

Covers the two shipped rules (R3 enforced at zero, R1 baseline-ratcheted) and —
the part that matters most — the ratchet invariants that stop this from becoming
the ``.complexity-allowlist`` HARD-entry pattern ADR-0420 itself cites as an
instance of the class:

  * a stale entry exits 1 EVEN under ``--warn``;
  * ``shape`` and ``log_level`` are RE-COMPARED every run, not merely stored;
  * an improvement TIGHTENS the baseline and can never be given back.

Run::

    uv run pytest yadgar/tests/scripts/test_check_swallowed_failure.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_swallowed_failure.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_swallowed_failure", _SCRIPT)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: @dataclass resolves annotations via
    # sys.modules[cls.__module__].__dict__, which is None for an unregistered module.
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


mod = _load()


def _scan(tmp_path: Path, source: str, name: str = "sample.py"):
    (tmp_path / name).write_text(source, encoding="utf-8")
    return mod.run(tmp_path, tmp_path)


def _rules(result) -> list[str]:
    return sorted(s.rule for s in result.sites)


# ---------------------------------------------------------------------------
# R3 — non-2xx status paired with a success-shaped body
# ---------------------------------------------------------------------------


class TestR3:
    @pytest.mark.parametrize(
        "body",
        ["[]", "{}", "()", '{"ok": True}', '{"stored": True}', '{"committed": True, "n": 1}'],
    )
    def test_flags_empty_or_success_shaped_body(self, tmp_path, body):
        res = _scan(
            tmp_path,
            f"def h():\n    return JSONResponse({body}, status_code=500)\n",
        )
        assert "R3" in _rules(res)

    def test_flags_503_not_only_500(self, tmp_path):
        res = _scan(tmp_path, "def h():\n    return JSONResponse([], status_code=503)\n")
        assert "R3" in _rules(res)

    def test_ignores_2xx(self, tmp_path):
        res = _scan(tmp_path, "def h():\n    return JSONResponse([], status_code=200)\n")
        assert "R3" not in _rules(res)

    def test_ignores_body_that_names_the_failure(self, tmp_path):
        res = _scan(
            tmp_path,
            'def h():\n    return JSONResponse({"error": "boom"}, status_code=500)\n',
        )
        assert "R3" not in _rules(res)

    def test_ignores_ok_false(self, tmp_path):
        res = _scan(
            tmp_path,
            'def h():\n    return JSONResponse({"ok": False}, status_code=500)\n',
        )
        assert "R3" not in _rules(res)

    def test_reads_content_keyword_not_only_positional(self, tmp_path):
        res = _scan(
            tmp_path,
            "def h():\n    return JSONResponse(content=[], status_code=500)\n",
        )
        assert "R3" in _rules(res)

    def test_ignores_non_response_call(self, tmp_path):
        res = _scan(tmp_path, "def h():\n    return make_thing([], status_code=500)\n")
        assert "R3" not in _rules(res)


# ---------------------------------------------------------------------------
# R1 — swallow-and-return-empty
# ---------------------------------------------------------------------------


_SWALLOW = """
import logging
logger = logging.getLogger(__name__)


def h():
    try:
        return risky()
    except Exception:
        logger.debug("nope")
        return []
"""


class TestR1:
    def test_flags_broad_except_returning_empty_with_debug_log(self, tmp_path):
        res = _scan(tmp_path, _SWALLOW)
        site = next(s for s in res.sites if s.rule == "R1")
        assert site.shape == "list"
        assert site.log_level == "debug"

    def test_flags_bare_except(self, tmp_path):
        res = _scan(
            tmp_path,
            "def h():\n    try:\n        return risky()\n    except:\n        return []\n",
        )
        assert "R1" in _rules(res)
        assert next(s for s in res.sites if s.rule == "R1").log_level == "none"

    def test_ignores_narrow_except(self, tmp_path):
        res = _scan(
            tmp_path,
            "def h():\n    try:\n        return risky()\n"
            "    except ValueError:\n        return []\n",
        )
        assert "R1" not in _rules(res)

    def test_ignores_handler_that_logs_at_warning(self, tmp_path):
        res = _scan(tmp_path, _SWALLOW.replace("logger.debug", "logger.warning"))
        assert "R1" not in _rules(res)

    def test_ignores_handler_that_reraises(self, tmp_path):
        res = _scan(tmp_path, _SWALLOW.replace("return []", "raise"))
        assert "R1" not in _rules(res)

    def test_ignores_non_literal_fallback_documented_limit(self, tmp_path):
        """A known coverage HOLE, asserted so it cannot regress silently."""
        res = _scan(
            tmp_path,
            "def h():\n    try:\n        return risky()\n"
            "    except Exception:\n        return _fallback()\n",
        )
        assert "R1" not in _rules(res)

    def test_ignores_bare_return_documented_limit(self, tmp_path):
        """The 197-of-250 recall traded for precision — excluded on purpose."""
        res = _scan(
            tmp_path,
            "def h():\n    try:\n        return risky()\n"
            "    except Exception:\n        return None\n",
        )
        assert "R1" not in _rules(res)

    def test_does_not_import_guard_blanket_exempt(self, tmp_path):
        """ADR-0125: a bare except around an internal import killed the whole
        @observe metric arm process-wide since v5.101. Exempting import guards
        would exempt one of the repo's worst instances."""
        res = _scan(
            tmp_path,
            "def h():\n    try:\n        import thing\n    except Exception:\n        return []\n",
        )
        assert "R1" in _rules(res)

    def test_return_inside_nested_def_is_not_the_handlers(self, tmp_path):
        res = _scan(
            tmp_path,
            "def h():\n    try:\n        return risky()\n"
            "    except Exception:\n"
            "        def inner():\n            return []\n"
            "        raise\n",
        )
        assert "R1" not in _rules(res)


# ---------------------------------------------------------------------------
# Baseline governance — ALWAYS hard, and only tightens
# ---------------------------------------------------------------------------


class TestBaselineGovernance:
    @staticmethod
    def _one_site(tmp_path):
        res = _scan(tmp_path, _SWALLOW)
        return next(s for s in res.sites if s.rule == "R1"), res

    @staticmethod
    def _entry(site, **over):
        base = {
            "category": "pre-existing",
            "rationale": "x" * 60,
            "shape": site.shape,
            "log_level": site.log_level,
        }
        base.update(over)
        return {site.key: base}

    def test_matching_entry_passes(self, tmp_path):
        site, res = self._one_site(tmp_path)
        assert mod.check_baseline(res.sites, self._entry(site), res.qualnames) == []

    def test_short_rationale_fails(self, tmp_path):
        site, res = self._one_site(tmp_path)
        errs = mod.check_baseline(
            res.sites, self._entry(site, rationale="too short"), res.qualnames
        )
        assert any("rationale" in e for e in errs)

    def test_bad_category_fails(self, tmp_path):
        site, res = self._one_site(tmp_path)
        errs = mod.check_baseline(res.sites, self._entry(site, category="whatever"), res.qualnames)
        assert any("invalid category" in e for e in errs)

    def test_missing_shape_fails(self, tmp_path):
        """A stored-but-uncompared field would make the ratchet decorative."""
        site, res = self._one_site(tmp_path)
        entry = self._entry(site)
        del entry[site.key]["shape"]
        errs = mod.check_baseline(res.sites, entry, res.qualnames)
        assert any("shape" in e for e in errs)

    def test_stale_entry_for_deleted_function_fails(self, tmp_path):
        _site, res = self._one_site(tmp_path)
        ghost = {
            "sample.py:gone:R1#0": {
                "category": "pre-existing",
                "rationale": "y" * 60,
                "shape": "list",
                "log_level": "debug",
            }
        }
        errs = mod.check_baseline(res.sites, ghost, res.qualnames)
        assert any("STALE" in e for e in errs)

    def test_improved_site_tightens_and_fails_until_regenerated(self, tmp_path):
        """The site is FIXED, so it no longer matches — the entry must go."""
        res = _scan(tmp_path, _SWALLOW.replace("logger.debug", "logger.error"))
        stale = {
            "sample.py:h:R1#0": {
                "category": "pre-existing",
                "rationale": "z" * 60,
                "shape": "list",
                "log_level": "debug",
            }
        }
        errs = mod.check_baseline(res.sites, stale, res.qualnames)
        assert any("NO LONGER A VIOLATION" in e for e in errs)
        assert any("RATCHET TIGHTENING" in e for e in errs), (
            "the message must say this is the ratchet, not a bug"
        )

    def test_log_level_improvement_within_rule_tightens(self, tmp_path):
        """debug -> info is still a violation, but louder. Baseline must follow."""
        res = _scan(tmp_path, _SWALLOW.replace("logger.debug", "logger.info"))
        site = next(s for s in res.sites if s.rule == "R1")
        errs = mod.check_baseline(res.sites, self._entry(site, log_level="debug"), res.qualnames)
        assert any("LOG LEVEL IMPROVED" in e for e in errs)

    def test_log_level_regression_fails(self, tmp_path):
        site, res = self._one_site(tmp_path)  # observed debug
        errs = mod.check_baseline(res.sites, self._entry(site, log_level="info"), res.qualnames)
        assert any("REGRESSED" in e for e in errs)

    def test_shape_change_in_place_fails(self, tmp_path):
        site, res = self._one_site(tmp_path)
        errs = mod.check_baseline(res.sites, self._entry(site, shape="dict"), res.qualnames)
        assert any("SHAPE CHANGED" in e for e in errs)


class TestWarnDoesNotSoftenIntegrity:
    def test_stale_entry_exits_1_even_under_warn(self, tmp_path):
        (tmp_path / "sample.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
        bl = tmp_path / "bl.json"
        bl.write_text(
            json.dumps(
                {
                    "sample.py:gone:R1#0": {
                        "category": "pre-existing",
                        "rationale": "q" * 60,
                        "shape": "list",
                        "log_level": "debug",
                    }
                }
            ),
            encoding="utf-8",
        )
        rc = mod.main(
            ["--root", str(tmp_path), "--baseline-file", str(bl), "--warn"],
        )
        assert rc == 1, "--warn must not soften baseline integrity"


class TestRepoIsClean:
    def test_r3_is_enforced_at_zero_on_the_real_tree(self):
        """R3 has NO baseline — it must be zero, or the ratchet died on day one."""
        repo = _SCRIPT.parent.parent
        res = mod.run(repo / "yadgar", repo)
        r3 = [s for s in res.sites if s.rule == "R3"]
        assert r3 == [], f"R3 violations on the tree: {[(s.relpath, s.lineno) for s in r3]}"

    def test_every_baseline_entry_resolves_on_the_real_tree(self):
        repo = _SCRIPT.parent.parent
        res = mod.run(repo / "yadgar", repo)
        baseline = json.loads((repo / ".swallow-baseline.json").read_text(encoding="utf-8"))
        assert mod.check_baseline(res.sites, baseline, res.qualnames) == []

    def test_baseline_header_says_the_entries_are_unreviewed(self):
        repo = _SCRIPT.parent.parent
        baseline = json.loads((repo / ".swallow-baseline.json").read_text(encoding="utf-8"))
        header = " ".join(baseline["_header"])
        assert "UNREVIEWED DEBT" in header
        assert "NOT VETTED" in header.upper()
