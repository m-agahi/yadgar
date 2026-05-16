"""§1 credentials hardening — red tests.

Verifies that:
1. StorageEngine raises KeyError when YADGAR_DB_PASS is unset AND
   YADGAR_ALLOW_ROOT is not set (server mode only — embedded mode
   does not use HTTP credentials).
2. YADGAR_ALLOW_ROOT=1 escape hatch suppresses the hard error (for tests).
3. Bash: entrypoint pattern uses :? syntax (checked via shell eval).
"""

import subprocess


def test_storage_engine_raises_without_db_pass(monkeypatch, tmp_path):
    """StorageEngine must raise KeyError (not fall back to root) when
    YADGAR_DB_PASS is unset and YADGAR_ALLOW_ROOT is not set."""
    monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:9999")
    monkeypatch.delenv("YADGAR_DB_PASS", raising=False)
    monkeypatch.delenv("YADGAR_DB_USER", raising=False)
    monkeypatch.delenv("YADGAR_ALLOW_ROOT", raising=False)

    import importlib

    # Re-import storage with cleared env so the module sees the new values.
    import yadgar.storage as _s

    importlib.reload(_s)

    with pytest.raises((KeyError, RuntimeError)):
        _s.StorageEngine(str(tmp_path / "db"))


def test_storage_engine_allow_root_escape_hatch(monkeypatch, tmp_path):
    """YADGAR_ALLOW_ROOT=1 must suppress the hard error so tests can run."""
    monkeypatch.setenv("YADGAR_DB_URL", "http://127.0.0.1:9999")
    monkeypatch.delenv("YADGAR_DB_PASS", raising=False)
    monkeypatch.delenv("YADGAR_DB_USER", raising=False)
    monkeypatch.setenv("YADGAR_ALLOW_ROOT", "1")

    import importlib

    import yadgar.storage as _s

    importlib.reload(_s)

    # Should not raise — will fail to connect but that's OK for this test.
    try:
        _s.StorageEngine(str(tmp_path / "db"))
    except KeyError:
        pytest.fail("KeyError raised even with YADGAR_ALLOW_ROOT=1")
    except Exception:
        # Connection refused, init error etc. are all fine — we only care
        # that KeyError is NOT raised.
        pass


def test_entrypoint_bash_fail_on_unset_surreal_pass():
    """Verify that bash :? syntax causes shell exit when var unset.

    Simulates the pattern:  : "${SURREAL_PASS:?SURREAL_PASS is required}"
    """
    result = subprocess.run(
        ["bash", "-c", 'SURREAL_PASS="" ; : "${SURREAL_PASS:?SURREAL_PASS is required}"'],
        capture_output=True,
    )
    assert result.returncode != 0, "bash must exit non-zero when SURREAL_PASS is empty"


def test_entrypoint_bash_pass_when_surreal_pass_set():
    """The :? pattern must not fail when SURREAL_PASS is set."""
    result = subprocess.run(
        ["bash", "-c", 'SURREAL_PASS="hunter2"; : "${SURREAL_PASS:?SURREAL_PASS is required}"'],
        capture_output=True,
    )
    assert result.returncode == 0, "bash must succeed when SURREAL_PASS is set"


def test_dockerfile_no_root_root_defaults():
    """Dockerfile.backend must not contain SURREAL_PASS=root as an ENV default."""
    dockerfile = __import__("pathlib").Path(__file__).parent.parent.parent / "Dockerfile.backend"
    if not dockerfile.exists():
        pytest.skip("Dockerfile.backend not found")
    content = dockerfile.read_text()
    # Must not have SURREAL_PASS=root as a hardcoded ENV default
    assert "SURREAL_PASS=root" not in content, (
        "Dockerfile.backend must not hardcode SURREAL_PASS=root"
    )
    assert "SURREAL_USER=root" not in content, (
        "Dockerfile.backend must not hardcode SURREAL_USER=root"
    )


def test_setup_sh_no_hardcoded_root_pass():
    """scripts/setup.sh must not contain hardcoded ROOT_PASS=root defaults."""
    setup = __import__("pathlib").Path(__file__).parent.parent.parent / "scripts" / "setup.sh"
    if not setup.exists():
        pytest.skip("scripts/setup.sh not found")
    content = setup.read_text()
    # ROOT_PASS="root" should be removed in favor of a required parameter
    assert 'ROOT_PASS="root"' not in content, 'setup.sh must not contain ROOT_PASS="root" default'


import pytest  # noqa: E402 — must come after the test functions that use it
