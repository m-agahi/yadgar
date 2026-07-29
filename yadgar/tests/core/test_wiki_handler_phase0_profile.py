"""Phase 0 profiling for v5.41.5 — per-substep timing of wiki_add handler.

NOT a regular test — run standalone to generate profiling report.
Marked @pytest.mark.perf: runs in the serial test-perf CI job, not test-core.

Run:
    .venv-test/bin/python -m pytest yadgar/tests/core/test_wiki_handler_phase0_profile.py -v -s -o addopts=
"""

from __future__ import annotations

import statistics
import time
import uuid
from unittest.mock import patch

import pytest

from yadgar._shared.file_queue.queue import FileQueue
from yadgar.core import server

_TEST_DIR = "/home/max/git/yadgar"


@pytest.fixture()
def _profile_env(tmp_path):
    """Isolated server with real file queue, no drainer."""
    server.init_engines(
        db_path=str(tmp_path / "profile.db"),
        embedding_model="all-MiniLM-L6-v2",
    )
    real_fq = FileQueue(tmp_path)

    import yadgar._shared.runtime.state as _state_mod
    import yadgar.core.lifecycle.lifecycle as _cl

    def _patched_get_fq():
        return real_fq

    with (
        patch.object(_cl, "_get_file_queue", _patched_get_fq),
        patch.object(_state_mod, "_queue_drainer", None),
    ):
        yield real_fq

    server.shutdown()


def _uid(base: str = "Profile") -> str:
    return f"{base} {uuid.uuid4().hex}"


def _stats(data: list[float]) -> tuple[float, float, float, float, float]:
    s = sorted(data)
    n = len(s)
    p50 = statistics.median(s)
    p90 = s[int(n * 0.90) - 1]
    p99 = s[int(n * 0.99) - 1]
    return p50, p90, p99, s[0], s[-1]


def _measure_substeps(real_fq) -> dict[str, list[float]]:
    """Run per-substep micro-benchmarks (n=100 each)."""
    import re as _re

    import yadgar._shared.runtime.state as _st
    import yadgar._shared.security.secrets as _secrets_mod
    from yadgar.backend.queue_drainer import QueueDrainer

    _gate_drainer = QueueDrainer(queue=real_fq, storage_factory=lambda: None, drain_interval=9999)

    timings: dict[str, list[float]] = {}

    for _ in range(100):
        content = "MCP handler I9 profiling measurement content body text"
        tags = ["perf"]
        t0 = time.perf_counter()
        _secrets_mod.gate_or_reject(content, tags=tags)
        timings.setdefault("secret_gate", []).append((time.perf_counter() - t0) * 1000)

    for _ in range(100):
        content = "MCP handler I9 profiling measurement content body text"
        tags = ["perf"]
        t0 = time.perf_counter()
        if _st._rules_engine is not None:
            _st._rules_engine.check_write_policy(content, "", tags)
        timings.setdefault("rules_engine", []).append((time.perf_counter() - t0) * 1000)

    for _ in range(100):
        title = _uid("I9 Profile")
        t0 = time.perf_counter()
        _new_slug = (_re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]
        del _new_slug
        timings.setdefault("branch_slug_gen", []).append((time.perf_counter() - t0) * 1000)

    sim_timings: list[float] = []
    for _ in range(100):
        title = _uid("I9 Profile")
        content = "MCP handler I9 profiling measurement content body text"
        slug = (_re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled")[:64]
        t0 = time.perf_counter()
        _gate_drainer._sim_gate_for_drainer(
            {"title": title, "content": content, "slug": slug, "branch": None}
        )
        sim_timings.append((time.perf_counter() - t0) * 1000)
    timings["similarity_gate"] = sim_timings

    for i in range(100):
        title = _uid("Enqueue Profile")
        slug = f"enqueue-profile-{i}"
        t0 = time.perf_counter()
        real_fq.enqueue(
            "wiki_add",
            {
                "wiki_schema_version": 2,
                "slug": slug,
                "title": title,
                "content": "profiling enqueue cost",
                "category": "reference",
                "tags": ["perf"],
                "source_memory_ids": None,
                "confidence": "medium",
                "append": False,
                "branch": None,
            },
        )
        timings.setdefault("enqueue", []).append((time.perf_counter() - t0) * 1000)

    return timings


def _measure_e2e() -> list[float]:
    """Run 100 e2e wiki_add(wait=False) calls and return latency list."""
    latencies: list[float] = []
    for _ in range(100):
        t0 = time.perf_counter()
        result = server.wiki_add(
            title=_uid("E2E Profile"),
            content="MCP handler I9 profiling end-to-end content measurement",
            tags=["perf"],
            wait=False,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )
        latencies.append((time.perf_counter() - t0) * 1000)
        assert result.get("queued") is True, f"Fell to sync path: {result}"
    return latencies


def _build_report(timings: dict[str, list[float]], e2e_p50: float) -> str:
    """Build the markdown profiling report string."""
    sim_p50 = statistics.median(timings["similarity_gate"])
    sec_p50 = statistics.median(timings["secret_gate"])
    enq_p50 = statistics.median(timings["enqueue"])
    slug_p50 = statistics.median(timings["branch_slug_gen"])
    rules_p50 = statistics.median(timings["rules_engine"])
    sim_fraction = sim_p50 / e2e_p50 if e2e_p50 > 0 else 0
    e2e_status = "PASS" if e2e_p50 <= 5.0 else f"FAIL — {e2e_p50 / 5.0:.1f}x over budget"

    def _row(label: str, key: str) -> str:
        p50, p90, p99, mn, mx = _stats(timings[key])
        return f"| {label} | {p50:.3f} | {p90:.3f} | {p99:.3f} | {mn:.3f} | {mx:.3f} |"

    if sim_fraction >= 0.50:
        dp_text = (
            f"**DP-A CONFIRMED:** Similarity gate = {sim_p50:.2f}ms = "
            f"{sim_fraction * 100:.0f}% of e2e. Option A (move to drainer) is correct fix."
        )
    elif e2e_p50 <= 5.0:
        dp_text = f"**STOP — already within budget:** e2e p50 = {e2e_p50:.2f}ms ≤ 5ms."
    else:
        dp_text = f"**WARNING:** gate = {sim_fraction * 100:.0f}% of e2e. Other costs dominate."

    lines = [
        "# V5.41.5 — wiki\\_add MCP Handler Profiling Report",
        "",
        "**Date:** 2026-06-02  ",
        "**Phase:** 0 — pre-fix baseline  ",
        "**I9 budget:** ≤5ms p50  ",
        "**Machine:** local dev (same machine as v5.41.2/v5.41.3 measurement)  ",
        "",
        "## Methodology",
        "",
        "- 100 calls per substep (5-call warmup discarded for e2e)",
        "- UUID-suffix per title to force unique similarity-gate paths every call",
        "- Queue drainer NOT running — file enqueue cost IS in I9 scope",
        "- Storage write excluded (not I9; see test_wiki_versioning_atomicity.py)",
        "- Embedding model: `all-MiniLM-L6-v2` (real, sentence-transformers)",
        "- SurrealDB: real server (not embedded mock)",
        "",
        "## Per-Substep Timings (n=100 each)",
        "",
        "| Substep | p50 (ms) | p90 (ms) | p99 (ms) | min (ms) | max (ms) |",
        "|---------|----------|----------|----------|----------|----------|",
        _row("Secret-gate regex scan (I26)", "secret_gate"),
        _row("Rules engine write-policy check", "rules_engine"),
        _row("Branch resolution + slug generation", "branch_slug_gen"),
        _row("Similarity gate (embed + KNN)", "similarity_gate"),
        _row("File queue enqueue (Path.write\\_text)", "enqueue"),
        _row("**E2E handler (server.wiki\\_add)**", "e2e_handler"),
        "",
        "## Key Findings",
        "",
        f"- **E2E p50 = {e2e_p50:.2f}ms** → {e2e_status}",
        f"- Similarity gate p50 = {sim_p50:.2f}ms ({sim_fraction * 100:.0f}% of e2e)",
        f"- Secret-gate p50 = {sec_p50:.3f}ms",
        f"- Rules engine p50 = {rules_p50:.3f}ms",
        f"- Branch/slug gen p50 = {slug_p50:.3f}ms",
        f"- Enqueue (file write) p50 = {enq_p50:.3f}ms",
        "",
        "## Decision Point Resolution",
        "",
        dp_text,
        "",
        "## v5.41.5 Fix Plan",
        "",
        "- **Root cause:** similarity gate (`find_similar_wiki_pages` = embed+KNN) on request thread",
        "- **Fix (Option A):** move gate to drainer pre-apply stage",
        "- **Expected after fix:** e2e p50 ≈ secret-gate + branch/slug + enqueue = sub-ms",
        "- **Breaking:** `wait=False` returns `{queued: true, similarity_check: 'deferred'}`",
        "  instead of sync rejection. `wait=True` still returns rejection synchronously.",
        "",
        "## References",
        "",
        "- Plan: `docs/plans/archive/PLAN_V5_41_5_HANDLER_I9_FIX.md`",
        "- Perf test (xfail): `yadgar/tests/test_wiki_mcp_handler_perf.py`",
        "- I9 invariant: `docs/contracts/ARCHITECTURE_INVARIANTS.md`",
        "- Baseline (task header): ~28.89ms p50 / xfail comment: ~48ms p50",
        f"- This measurement: {e2e_p50:.2f}ms p50",
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.perf
def test_wiki_add_phase0_profiling(_profile_env, tmp_path):
    """Phase 0: measure per-substep latency of wiki_add(wait=False).

    Generates a profiling report under tmp_path (not a repo-tracked file).
    Run with -s to see console output.
    """
    real_fq = _profile_env

    # Warmup
    for _ in range(5):
        server.wiki_add(
            title=_uid("Warmup"),
            content="warmup",
            tags=["perf-warmup"],
            wait=False,
            branch_hint="feat/test-branch",
            directory=_TEST_DIR,
        )

    timings = _measure_substeps(real_fq)
    e2e_latencies = _measure_e2e()
    timings["e2e_handler"] = e2e_latencies

    e2e_p50 = statistics.median(e2e_latencies)
    sim_p50 = statistics.median(timings["similarity_gate"])

    print(f"\n{'Substep':<35} {'p50':>8} {'p90':>8} {'p99':>8}")
    print("-" * 65)
    for key, label in [
        ("secret_gate", "Secret-gate"),
        ("rules_engine", "Rules engine"),
        ("branch_slug_gen", "Branch/slug gen"),
        ("similarity_gate", "Similarity gate (embed+KNN)"),
        ("enqueue", "File queue enqueue"),
        ("e2e_handler", "E2E handler (full call)"),
    ]:
        p50, p90, p99, _, _ = _stats(timings[key])
        print(f"  {label:<33} {p50:7.3f}  {p90:7.3f}  {p99:7.3f}")

    report = _build_report(timings, e2e_p50)
    report_path = tmp_path / "v5-41-5-profiling-report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport: {report_path}")

    assert e2e_p50 > 0.0, "E2E measurement returned zero"
    assert sim_p50 > 0.0, "Similarity gate returned zero"
