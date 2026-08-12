"""Car 0108 — runaway-guard invariants for the local test/benchmark entry points.

Context (incident 2026-08-01): the workstation hard-locked overnight. The RCA
cleared the pytest sweep as the mechanism (an abandoned libvirt VM was the
culprit), but the audit that came with it found three real holes in the guards
that were supposed to make an unattended run un-lockable:

1. ``scripts/test-capped.sh`` **failed open** — when ``systemd-run --user
   --scope`` was unavailable it silently degraded to a bare
   ``timeout --signal=KILL``, i.e. no ``CPUQuota`` and no ``MemoryMax``. The
   guarantee the script exists to provide vanished with a one-line stderr note.
2. Six ``Makefile`` targets could start ``pytest`` / a benchmark **without the
   wrapper at all** (``check``, ``test-ci``, ``e2e``, ``eval``,
   ``longmemeval``, ``perf``) — and ``e2e`` is fired automatically by the
   ``e2e-behavior-contract`` pre-push hook.
3. ``-n auto`` was clamped by RAM only, never by CPU
   (``pytest_xdist_auto_num_workers`` -> ``_ram_safe_workers``), so a
   high-RAM many-core box still spawned enough workers to peg every core.

The Makefile invariant below is the ratchet that matters most: it fails when a
NEW target invokes pytest or a benchmark without routing through
``scripts/test-capped.sh``, so this class cannot silently come back.

Gating: ``yadgar/tests/scripts/`` is collected by the ``test-fast`` CI job
(``.forgejo/workflows/ci-pr.yaml``), which ``test-gate`` needs.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

from yadgar.tests._paths import REPO_ROOT

MAKEFILE = REPO_ROOT / "Makefile"
TEST_CAPPED = REPO_ROOT / "scripts" / "test-capped.sh"

# Targets deliberately allowed to invoke pytest / a benchmark WITHOUT the
# wrapper. Empty on purpose: every such target routes through
# scripts/test-capped.sh today. An entry here must carry a rationale, and a
# stale entry (target gone, or no longer invoking pytest) is a hard failure —
# same governance shape as .health-endpoint-allowlist.json.
UNCAPPED_TARGET_ALLOWLIST: dict[str, str] = {}

_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-/]*)\s*:(?!=)")
_PYTEST_RE = re.compile(r"\bpytest\b")
_BENCHMARK_RE = re.compile(r"\bpython3?\s+benchmarks/\S+\.py")


def _parse_recipes(text: str) -> dict[str, str]:
    """Map target name -> its recipe as ONE string.

    Two things a naive per-line scan gets wrong on this Makefile:
      * the real recipes are ``$(LOCKED) '...' \\`` spanning 2-3 lines, with
        ``pytest`` living on a continuation — line continuations MUST be joined
        first or the scan passes vacuously;
      * ``#`` comment lines inside a recipe would otherwise yield phantom hits
        from prose that merely mentions pytest.
    """
    recipes: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        if raw.startswith("\t"):
            if current is not None:
                body = raw[1:]
                if not body.lstrip().startswith("#"):
                    recipes[current].append(body)
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _TARGET_RE.match(raw)
        if m:
            current = m.group(1)
            recipes.setdefault(current, [])
        else:
            current = None  # variable assignment, conditional, include, ...
    joined: dict[str, str] = {}
    for target, lines in recipes.items():
        # Join backslash continuations into one logical command string.
        text_ = "\n".join(lines)
        joined[target] = text_.replace("\\\n", " ")
    return joined


def _invokes_test_runner(recipe: str) -> bool:
    return bool(_PYTEST_RE.search(recipe) or _BENCHMARK_RE.search(recipe))


@pytest.fixture(scope="module")
def recipes() -> dict[str, str]:
    return _parse_recipes(MAKEFILE.read_text())


class TestMakefileCapInvariant:
    """Every Makefile target that can start pytest/a benchmark must be capped."""

    def test_parser_sees_the_known_runner_targets(self, recipes):
        """Guard-the-guard: a parser that misses continuations passes vacuously.

        ``test``/``test-ci``/``e2e`` all hide ``pytest`` on a continuation line
        inside ``$(LOCKED) '...'``. If this fails, the invariant below is not
        actually looking at anything.
        """
        found = {t for t, r in recipes.items() if _invokes_test_runner(r)}
        for expected in ("test", "test-ci", "e2e", "check", "eval", "longmemeval", "perf"):
            assert expected in found, (
                f"Makefile recipe parser did not see a test/benchmark runner in "
                f"target '{expected}' — the cap invariant would pass vacuously. "
                f"Detected: {sorted(found)}"
            )

    def test_every_runner_target_routes_through_test_capped(self, recipes):
        uncapped = [
            target
            for target, recipe in recipes.items()
            if _invokes_test_runner(recipe)
            and "scripts/test-capped.sh" not in recipe
            and target not in UNCAPPED_TARGET_ALLOWLIST
        ]
        assert not uncapped, (
            "Makefile target(s) start pytest or a benchmark WITHOUT the runaway "
            f"guard: {sorted(uncapped)}. Route the recipe through "
            "scripts/test-capped.sh (set per-target TEST_TIMEOUT / TEST_CPU_QUOTA "
            "/ TEST_MEM_MAX if the defaults do not fit), or add an entry with a "
            "rationale to UNCAPPED_TARGET_ALLOWLIST in this file."
        )

    def test_allowlist_has_no_stale_entries(self, recipes):
        stale = [
            target
            for target in UNCAPPED_TARGET_ALLOWLIST
            if target not in recipes or not _invokes_test_runner(recipes[target])
        ]
        assert not stale, (
            f"UNCAPPED_TARGET_ALLOWLIST entries no longer need an exemption: "
            f"{sorted(stale)} (target gone, or no longer invokes pytest/a "
            "benchmark). Remove them so the allowlist stays a live document."
        )

    def test_allowlist_entries_carry_a_rationale(self):
        thin = [t for t, why in UNCAPPED_TARGET_ALLOWLIST.items() if len(why.strip()) < 40]
        assert not thin, f"Allowlist entries need a real rationale (>=40 chars): {sorted(thin)}"

    @pytest.mark.parametrize(
        "target", ["check", "test", "test-ci", "e2e", "eval", "longmemeval", "perf"]
    )
    def test_runner_targets_still_parse(self, target):
        """`make -n <target>` must exit 0 — the wrapper must not break the recipe."""
        result = subprocess.run(
            ["make", "-n", target],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"make -n {target} failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "scripts/test-capped.sh" in result.stdout + result.stderr, (
            f"make -n {target} does not expand to a capped command:\n{result.stdout}"
        )


# ---------------------------------------------------------------------------
# ci-local (Car F10) — the generic scan above cannot see this target anymore
# ---------------------------------------------------------------------------

CI_LOCAL_LEG_RUNNER = REPO_ROOT / "scripts" / "ci-local-legs.sh"


class TestCiLocalDelegatesToTestCapped:
    """Car F10 rewrote `ci-local`'s recipe to delegate to
    scripts/ci-local-legs.sh (one `run_leg`/pytest process per CI subsystem
    job) instead of invoking `pytest` directly. The Makefile recipe text no
    longer contains the literal word "pytest", so `_invokes_test_runner`
    above returns False for it and `test_every_runner_target_routes_through_
    test_capped` silently stops looking at `ci-local` at all — the exact
    vacuous-pass shape Car 0108 exists to prevent, just one level removed
    (a delegated script, not a bare Makefile recipe). This class closes that
    specific gap by following the delegation instead of widening the generic
    regex (which would have to special-case "recipe merely mentions a shell
    script" — too broad, and this repo's Makefile has none of those besides
    this one today).
    """

    def test_ci_local_recipe_delegates_to_leg_runner(self, recipes):
        assert "ci-local" in recipes, "the `ci-local` target is gone or renamed"
        assert "scripts/ci-local-legs.sh" in recipes["ci-local"], (
            "`ci-local`'s recipe no longer delegates to scripts/ci-local-legs.sh. "
            "If it now invokes pytest directly again, add 'ci-local' to the "
            "expected list in test_parser_sees_the_known_runner_targets so the "
            "generic scan covers it, and this delegation-specific test can go."
        )

    def test_leg_runner_wraps_every_leg_in_test_capped(self):
        text = CI_LOCAL_LEG_RUNNER.read_text()
        assert _PYTEST_RE.search(text), (
            "scripts/ci-local-legs.sh no longer invokes pytest — has the leg "
            "runner been rewritten? Update this test to match its new shape."
        )
        assert "test-capped.sh" in text, (
            "scripts/ci-local-legs.sh invokes pytest without routing through "
            "scripts/test-capped.sh — a leg could run with no CPUQuota/MemoryMax."
        )


# ---------------------------------------------------------------------------
# scripts/test-capped.sh — fail CLOSED when the resource cap is unavailable
# ---------------------------------------------------------------------------

FAIL_CLOSED_MARKER = "systemd-run --user --scope unavailable"


def _run_capped(tmp_path, *args: str, env_extra: dict[str, str] | None = None, timeout: float = 60):
    """Run scripts/test-capped.sh with a stubbed-out `systemd-run` on PATH.

    The stub shadows systemd-run (exit 1) while leaving the rest of PATH intact
    — `timeout` must stay resolvable, so gutting PATH entirely is wrong.
    """
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "systemd-run"
    stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env.pop("TEST_ALLOW_UNCAPPED", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(TEST_CAPPED), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


class TestTestCappedFailsClosed:
    def test_refuses_to_run_uncapped(self, tmp_path):
        result = _run_capped(tmp_path, "true")
        assert result.returncode != 0, (
            "test-capped.sh ran the command with no CPUQuota/MemoryMax when "
            f"systemd-run was unavailable (fail-OPEN). stdout={result.stdout!r}"
        )
        assert FAIL_CLOSED_MARKER in result.stderr, (
            f"error message must name the missing capability; got: {result.stderr!r}"
        )
        assert "TEST_ALLOW_UNCAPPED" in result.stderr, (
            "the refusal must tell the operator how to opt out on a host that "
            f"genuinely has no user-scope systemd; got: {result.stderr!r}"
        )

    def test_opt_out_permits_the_run(self, tmp_path):
        result = _run_capped(tmp_path, "true", env_extra={"TEST_ALLOW_UNCAPPED": "1"})
        assert result.returncode == 0, (
            f"TEST_ALLOW_UNCAPPED=1 must still permit the run; "
            f"rc={result.returncode} stderr={result.stderr!r}"
        )

    def test_opt_out_still_timeout_bounded(self, tmp_path):
        """The escape hatch must not resurrect the unbounded-hang case.

        Guarantee #1 (timeout --signal=KILL) survives the opt-out even though
        guarantee #2 (CPUQuota/MemoryMax) cannot.
        """
        result = _run_capped(
            tmp_path,
            "sleep",
            "30",
            env_extra={"TEST_ALLOW_UNCAPPED": "1", "TEST_TIMEOUT": "1"},
            timeout=30,
        )
        assert result.returncode != 0, (
            "an uncapped run must still be killed at TEST_TIMEOUT; it exited 0"
        )
        assert result.returncode in (124, 137, -9), (
            f"expected a timeout/SIGKILL exit, got {result.returncode}"
        )


# ---------------------------------------------------------------------------
# conftest — `-n auto` must be clamped by CPU as well as RAM
# ---------------------------------------------------------------------------


class TestAutoWorkersCpuClamp:
    def test_auto_workers_is_min_of_ram_and_cpu_clamps(self, monkeypatch):
        from yadgar.tests import conftest as tests_conftest

        # Plenty of RAM (200GB -> 50 RAM-safe workers), few cores.
        monkeypatch.setattr(tests_conftest, "_available_ram_gb", lambda: 200.0)
        monkeypatch.setattr(tests_conftest.os, "cpu_count", lambda: 8)
        n = tests_conftest.pytest_xdist_auto_num_workers(None)
        cpu_cap = tests_conftest._cpu_safe_workers()
        assert n == cpu_cap, f"CPU clamp must bind when RAM is plentiful: {n} != {cpu_cap}"
        assert n < tests_conftest._ram_safe_workers()

    def test_ram_clamp_still_binds_when_it_is_lower(self, monkeypatch):
        from yadgar.tests import conftest as tests_conftest

        monkeypatch.setattr(tests_conftest, "_available_ram_gb", lambda: 8.0)  # -> 2
        monkeypatch.setattr(tests_conftest.os, "cpu_count", lambda: 64)
        n = tests_conftest.pytest_xdist_auto_num_workers(None)
        assert n == 2, f"RAM clamp must still bind when it is the lower of the two: {n}"

    def test_cpu_clamp_is_a_fraction_of_cpu_count(self, monkeypatch):
        from yadgar.tests import conftest as tests_conftest

        monkeypatch.setattr(tests_conftest.os, "cpu_count", lambda: 24)
        cap = tests_conftest._cpu_safe_workers()
        assert 1 <= cap < 24, f"CPU cap must be a fraction of cpu_count, got {cap}"

    def test_never_returns_zero_on_a_tiny_box(self, monkeypatch):
        from yadgar.tests import conftest as tests_conftest

        monkeypatch.setattr(tests_conftest, "_available_ram_gb", lambda: 0.5)
        monkeypatch.setattr(tests_conftest.os, "cpu_count", lambda: 1)
        assert tests_conftest.pytest_xdist_auto_num_workers(None) == 1

    def test_unknown_cpu_count_does_not_crash(self, monkeypatch):
        """`os.cpu_count()` may return None; the clamp must degrade, not raise."""
        from yadgar.tests import conftest as tests_conftest

        monkeypatch.setattr(tests_conftest, "_available_ram_gb", lambda: 16.0)
        monkeypatch.setattr(tests_conftest.os, "cpu_count", lambda: None)
        assert tests_conftest.pytest_xdist_auto_num_workers(None) >= 1
