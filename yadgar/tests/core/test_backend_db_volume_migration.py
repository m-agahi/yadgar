"""One-time migration of the backend DB out of the legacy named volume.

Converging `yadgar daemon start` on the host bind mount (Bug 11, task 0100) is a
one-line change; the risk is entirely in what happens to the store that existing
`daemon start` users already hold inside the named volume `yadgar-db-data`.
Flipping the mount without moving the data points the backend at an empty host
directory, which presents as TOTAL DATA LOSS.

The store is surrealkv. ADR-0090 records that a half-flushed / half-copied
surrealkv directory is corrupt-on-reopen, so every guard below exists so that the
failure mode is "user is still on the named volume" (inconvenient, reversible)
rather than "user has a partial store" (unrecoverable).

The two script-semantics tests at the bottom are a PAIR and only mean something
together: `test_migrate_script_copies_into_place` proves `MIGRATE_SH` actually
produces `surreal_db`, and `test_interrupted_copy_leaves_no_partial_surreal_db`
proves a failure mid-copy leaves none. Either one alone passes trivially against
an empty script.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from yadgar.core.daemon import db_migrate

VOLUME = "yadgar-db-data"
IMAGE = "openfantasy/yadgar-backend:test"
NAMES = ("yadgar-backend", "yadgar")


class _FakeRunner:
    """Records argv and answers by matching the subcommand.

    ``volume_names`` / ``ps_names`` are the stdout of ``volume ls`` / ``ps``;
    ``probe_stdout`` is what the probe container prints. Anything unmatched
    returns rc=0 with EMPTY stdout on purpose — a migration that can be driven
    to copy by a runner which just says "0" to everything is not gated properly.
    """

    def __init__(
        self,
        *,
        volume_names: str = VOLUME,
        ps_names: str = "",
        probe_stdout: str = db_migrate.PROBE_SENTINEL,
        volume_ls_rc: int = 0,
        ps_rc: int = 0,
        copy_rc: int = 0,
        copy_stderr: str = "",
    ):
        self.calls: list[list[str]] = []
        self._cfg = SimpleNamespace(
            volume_names=volume_names,
            ps_names=ps_names,
            probe_stdout=probe_stdout,
            volume_ls_rc=volume_ls_rc,
            ps_rc=ps_rc,
            copy_rc=copy_rc,
            copy_stderr=copy_stderr,
        )

    def __call__(self, cmd, *a, **kw):
        argv = [str(c) for c in cmd]
        self.calls.append(argv)
        c = self._cfg
        if "volume" in argv and "ls" in argv:
            return SimpleNamespace(returncode=c.volume_ls_rc, stdout=c.volume_names, stderr="")
        if "ps" in argv:
            return SimpleNamespace(returncode=c.ps_rc, stdout=c.ps_names, stderr="")
        if "run" in argv:
            if any(db_migrate.PROBE_SENTINEL in x for x in argv):
                return SimpleNamespace(returncode=0, stdout=c.probe_stdout, stderr="")
            return SimpleNamespace(returncode=c.copy_rc, stdout="", stderr=c.copy_stderr)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if "run" in c]

    @property
    def copy_runs(self) -> list[list[str]]:
        return [c for c in self.runs if not any(db_migrate.PROBE_SENTINEL in x for x in c)]


def _migrate(tmp_path: Path, monkeypatch, runner: _FakeRunner, *, data_dir: Path | None = None):
    monkeypatch.setattr(db_migrate.subprocess, "run", runner)
    dd = data_dir if data_dir is not None else tmp_path / "data"
    dd.mkdir(parents=True, exist_ok=True)
    return db_migrate.migrate_named_volume_db(
        runtime="podman", volume=VOLUME, data_dir=dd, image=IMAGE, container_names=NAMES
    )


# ── (1) the happy path ────────────────────────────────────────────────────────


def test_volume_with_data_and_empty_host_dir_is_copied(tmp_path, monkeypatch):
    runner = _FakeRunner()
    result = _migrate(tmp_path, monkeypatch, runner)

    assert result["status"] == "migrated", result
    assert len(runner.copy_runs) == 1, (
        f"expected exactly one copy container, got {runner.copy_runs}"
    )
    argv = " ".join(runner.copy_runs[0])
    assert f"{VOLUME}:/src:ro" in argv, f"source volume not mounted read-only: {argv}"
    assert f"{tmp_path / 'data'}:/dst" in argv, f"host data dir not mounted rw: {argv}"
    # Rootless podman: the volume's files are owned by the container-canonical uid,
    # which the host user cannot read without these two.
    assert "--user" in runner.copy_runs[0] and "root" in runner.copy_runs[0], argv
    assert "--security-opt" in argv and "label=disable" in argv, argv
    assert "--rm" in runner.copy_runs[0], f"throwaway container is not --rm: {argv}"


def test_the_named_volume_is_never_deleted(tmp_path, monkeypatch):
    """The volume is the rollback. Nothing in this module may remove it."""
    runner = _FakeRunner()
    _migrate(tmp_path, monkeypatch, runner)
    for argv in runner.calls:
        assert not ("volume" in argv and "rm" in argv), (
            f"migration issued a volume-removal command ({argv}) — the named volume "
            f"is the ONLY rollback for a botched surrealkv copy (ADR-0090)"
        )


def test_the_host_store_is_never_read_from_podman_internal_storage(tmp_path, monkeypatch):
    """/var/lib/containers/storage/volumes/... is podman-internal, unstable, and
    unreadable under rootless podman — the copy must go through a container."""
    runner = _FakeRunner()
    _migrate(tmp_path, monkeypatch, runner)
    for argv in runner.calls:
        assert not any("containers/storage/volumes" in a for a in argv), argv


# ── (2) idempotency ───────────────────────────────────────────────────────────


def test_second_run_is_a_no_op(tmp_path, monkeypatch):
    """Run twice against the same host dir: the second run must copy nothing."""
    data_dir = tmp_path / "data"
    first = _migrate(tmp_path, monkeypatch, _FakeRunner(), data_dir=data_dir)
    assert first["status"] == "migrated"

    # The real copy happened inside the (faked) container, so materialise its
    # only externally-visible effect: the host store now exists.
    (data_dir / "surreal_db").mkdir(parents=True)
    (data_dir / "surreal_db" / "sstable-1").write_text("live data")

    runner = _FakeRunner()
    second = _migrate(tmp_path, monkeypatch, runner, data_dir=data_dir)

    assert second["status"] == "skipped", second
    assert second["reason"] == "host_db_present", second
    assert runner.copy_runs == [], f"second run tried to copy again: {runner.copy_runs}"
    assert (data_dir / "surreal_db" / "sstable-1").read_text() == "live data"


# ── (3) host already populated ────────────────────────────────────────────────


def test_populated_host_dir_warns_and_does_not_overwrite(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    (data_dir / "surreal_db").mkdir(parents=True)
    (data_dir / "surreal_db" / "sstable-1").write_text("live data")

    runner = _FakeRunner()
    result = _migrate(tmp_path, monkeypatch, runner, data_dir=data_dir)

    assert result["status"] == "skipped" and result["reason"] == "host_db_present", result
    assert runner.copy_runs == [], "refused to skip — would have overwritten live host data"
    assert (data_dir / "surreal_db" / "sstable-1").read_text() == "live data"

    warning = capsys.readouterr().err
    assert VOLUME in warning, f"warning does not name the named volume: {warning!r}"
    assert str(data_dir / "surreal_db") in warning, (
        f"warning does not name the host path: {warning!r}"
    )


# ── (4) something is holding the store ────────────────────────────────────────


@pytest.mark.parametrize("running", ["yadgar-backend", "yadgar"])
def test_refuses_while_a_container_holds_the_store(tmp_path, monkeypatch, capsys, running):
    runner = _FakeRunner(ps_names=f"some-other-container\n{running}\n")
    result = _migrate(tmp_path, monkeypatch, runner)

    assert result["status"] == "skipped", result
    assert result["reason"] == "containers_running", result
    assert runner.runs == [], (
        "migration ran a container while the store was held — copying a live "
        "surrealkv store yields a corrupt-on-reopen directory (ADR-0090)"
    )
    assert running in capsys.readouterr().err


def test_the_backend_container_override_is_checked_too(tmp_path, monkeypatch):
    """`$YADGAR_BACKEND_CONTAINER` renames the container; a name-based check that
    only knows the default would happily copy a live store."""
    monkeypatch.setattr(
        db_migrate.subprocess, "run", (runner := _FakeRunner(ps_names="my-backend"))
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = db_migrate.migrate_named_volume_db(
        runtime="podman",
        volume=VOLUME,
        data_dir=data_dir,
        image=IMAGE,
        container_names=("my-backend", "yadgar"),
    )
    assert result["reason"] == "containers_running", result
    assert runner.runs == []


# ── (5) skip vs. cannot-determine ─────────────────────────────────────────────


def test_absent_volume_skips_silently(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner(volume_names="yadgar-data\nyadgar-queue-data\n")
    result = _migrate(tmp_path, monkeypatch, runner)

    assert result["status"] == "skipped" and result["reason"] == "volume_absent", result
    assert runner.runs == []
    assert capsys.readouterr().err == "", "a fresh install must not print a migration warning"


@pytest.mark.parametrize("failing", ["volume_ls", "ps"])
def test_an_unreadable_runtime_is_indeterminate_not_absent(tmp_path, monkeypatch, capsys, failing):
    """A failing `volume ls` / `ps` is NOT proof the volume is absent — it is proof
    we cannot tell. Conflating the two makes a broken runtime look like a clean
    fresh install, so it gets its own loud reason and never proceeds to a copy."""
    kwargs = {"volume_ls_rc": 1} if failing == "volume_ls" else {"ps_rc": 1}
    runner = _FakeRunner(**kwargs)
    result = _migrate(tmp_path, monkeypatch, runner)

    assert result["status"] == "skipped", result
    assert result["reason"] == "runtime_indeterminate", result
    assert runner.runs == []
    assert "could not determine" in capsys.readouterr().err.lower()


def test_probe_must_see_its_sentinel_before_anything_is_copied(tmp_path, monkeypatch):
    """The `surreal_db`-in-volume probe is satisfied by an explicit sentinel on
    stdout, not by exit code 0. A runtime that exits 0 while printing nothing —
    or a test double that says 0 to everything — must NOT reach the copy."""
    runner = _FakeRunner(probe_stdout="")
    result = _migrate(tmp_path, monkeypatch, runner)

    assert result["status"] == "skipped" and result["reason"] == "volume_has_no_db", result
    assert runner.copy_runs == []


def test_a_failed_copy_reports_failure_and_does_not_claim_success(tmp_path, monkeypatch, capsys):
    runner = _FakeRunner(copy_rc=1, copy_stderr="cp: cannot stat")
    result = _migrate(tmp_path, monkeypatch, runner)

    assert result["status"] == "failed", result
    assert "cp: cannot stat" in capsys.readouterr().err


# ── (6) the copy script's own semantics, run for real ─────────────────────────
#
# These two exercise the SHIPPED `MIGRATE_SH` string against real directories.
# They are a pair: blank the constant and the happy-path test goes red while the
# interrupted-copy test stays green, which is what makes the latter non-vacuous.

TMP_NAME = "surreal_db.migrating-1754000000"


def _run_migrate_sh(src: Path, dst: Path, *, path_prefix: Path | None = None):
    env = dict(os.environ, SRC=str(src), DST=str(dst), TMP=TMP_NAME)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["/bin/sh", "-c", db_migrate.MIGRATE_SH], capture_output=True, text=True, env=env
    )


def _seed_store(src: Path) -> None:
    (src / "surreal_db").mkdir(parents=True)
    (src / "surreal_db" / "sstable-1").write_text("rows")
    (src / "surreal_db" / "nested").mkdir()
    (src / "surreal_db" / "nested" / "manifest").write_text("manifest")


def test_migrate_script_copies_into_place(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _seed_store(src)
    dst.mkdir()

    result = _run_migrate_sh(src, dst)

    assert result.returncode == 0, result.stderr
    assert (dst / "surreal_db" / "sstable-1").read_text() == "rows"
    assert (dst / "surreal_db" / "nested" / "manifest").read_text() == "manifest"
    assert not (dst / TMP_NAME).exists(), "temp sibling not renamed away"
    assert (src / "surreal_db" / "sstable-1").exists(), "source store was moved, not copied"


def test_interrupted_copy_leaves_no_partial_surreal_db(tmp_path):
    """A copy that dies mid-way must leave NOTHING at `surreal_db`.

    surrealkv reopens a half-written directory as a corrupt store (ADR-0090), and
    a partial `surreal_db` on the host would silently become the live DB on the
    next backend start. The temp-sibling + rename shape is what prevents that;
    this asserts it by making `cp` fail after it has already written output.
    """
    src, dst = tmp_path / "src", tmp_path / "dst"
    _seed_store(src)
    dst.mkdir()

    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "cp").write_text(
        '#!/bin/sh\ntarget="$3"\nmkdir -p "$target"\necho half > "$target/partial"\nexit 1\n'
    )
    (shim / "cp").chmod(0o755)

    result = _run_migrate_sh(src, dst, path_prefix=shim)

    assert result.returncode != 0, "a failed cp must fail the script"
    assert not (dst / "surreal_db").exists(), (
        "an interrupted copy left a partial `surreal_db` on the host — the next "
        "backend start would open it as the live store (ADR-0090)"
    )
    assert (dst / TMP_NAME / "partial").exists(), (
        "the partial output did not land in the temp sibling — the guard under "
        "test is not the one that produced the green above"
    )


def test_migrate_script_refuses_to_overwrite_an_existing_host_store(tmp_path):
    """Belt-and-braces behind the host-dir check: even if the caller's condition
    were wrong, the script itself must not clobber a live store."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _seed_store(src)
    (dst / "surreal_db").mkdir(parents=True)
    (dst / "surreal_db" / "sstable-1").write_text("live data")

    result = _run_migrate_sh(src, dst)

    assert result.returncode != 0
    assert (dst / "surreal_db" / "sstable-1").read_text() == "live data"


# ── (7) wiring: start_backend drives the migration before it starts anything ──


def test_start_backend_migrates_before_launching_the_backend(tmp_path, monkeypatch):
    """The migration's whole safety argument is that `daemon start` runs at the one
    moment nothing holds the store — so it must fire BEFORE the backend `run`."""
    from yadgar.core.daemon import daemon as daemon_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("YADGAR_BACKEND_VOLUME", raising=False)
    monkeypatch.delenv("YADGAR_BACKEND_CONTAINER", raising=False)

    seen: list[dict] = []
    order: list[str] = []

    def fake_migrate(**kwargs):
        order.append("migrate")
        seen.append(kwargs)
        return {"status": "skipped", "reason": "volume_absent"}

    def fake_run(cmd, *a, **kw):
        argv = [str(c) for c in cmd]
        if "run" in argv:
            order.append("run")
        return SimpleNamespace(returncode=0, stdout="cid", stderr="")

    monkeypatch.setattr(daemon_mod, "migrate_named_volume_db", fake_migrate)
    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_mod, "_ensure_network", lambda: None)
    monkeypatch.setattr(daemon_mod, "_get_runtime", lambda: "podman")
    monkeypatch.setattr(daemon_mod, "_container_memory_mb", lambda: 512)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda *_: None)

    d = daemon_mod.YadgarDaemon()
    monkeypatch.setattr(d, "_container_running", lambda *a, **k: False)
    monkeypatch.setattr(d, "_image_exists", lambda *a, **k: True)
    d.start_backend()

    assert order and order[0] == "migrate", f"backend started before migrating: {order}"
    assert len(seen) == 1, seen
    # The legacy volume name is `yadgar-db-data` — what `start_backend` mounted
    # before Bug 11 was finished. yadgar/_shared/config/config_registry.py declares
    # a DIFFERENT default (`yadgar-backend-data`) for the same env var; resolving
    # through it would look for a volume that never existed and migrate nothing
    # while reporting success. That mismatch is pre-existing and out of scope here.
    assert seen[0]["volume"] == "yadgar-db-data", seen[0]
    assert seen[0]["data_dir"] == Path(str(tmp_path / "data")), seen[0]
    assert "yadgar-backend" in seen[0]["container_names"], seen[0]


def test_an_unstubbed_migration_injects_no_container_before_the_backend(tmp_path, monkeypatch):
    """With the migration NOT stubbed and a naive all-zeros fake runner, the first
    `run` argv `start_backend` issues must still be the backend's.

    Several pre-existing suites (`test_daemon_cli_fixes_v5_49_1._start_backend_argv`,
    `test_daemon_queue_wiring`) reach into the captured calls and take the FIRST
    `run` as the backend's. A migration that probes or copies on a runtime that
    merely exits 0 would silently steal that slot — those suites would then assert
    against the throwaway container's argv and go green for the wrong reason. This
    pins the property they depend on: every gate here needs a positive NAME or
    SENTINEL match, which a fake that only knows how to say 0 cannot produce.
    """
    from yadgar.core.daemon import daemon as daemon_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_DATA_DIR", str(tmp_path / "data"))

    calls: list[list[str]] = []

    def zeros(cmd, *a, **kw):
        calls.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0, stdout="abc123deadbeef", stderr="")

    monkeypatch.setattr(daemon_mod.subprocess, "run", zeros)
    monkeypatch.setattr(daemon_mod, "_ensure_network", lambda: None)
    monkeypatch.setattr(daemon_mod, "_get_runtime", lambda: "podman")
    monkeypatch.setattr(daemon_mod, "_container_memory_mb", lambda: 512)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda *_: None)

    d = daemon_mod.YadgarDaemon()
    monkeypatch.setattr(d, "_container_running", lambda *a, **k: False)
    monkeypatch.setattr(d, "_image_exists", lambda *a, **k: True)
    d.start_backend()

    runs = [c for c in calls if "run" in c]
    assert runs, f"no `run` argv captured at all; calls={calls}"
    assert "--name" in runs[0] and "yadgar-backend" in runs[0], (
        f"the first `run` issued by start_backend is not the backend container "
        f"({runs[0]}) — an unstubbed migration ran a throwaway container off a "
        f"runtime that only exits 0"
    )
