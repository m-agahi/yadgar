"""v5.46.13 — yadgar-setup.sh step 8 inits config.yaml if missing before sync.

Bug: on fresh install (no ~/.yadgar/config.yaml), ``yadgar config sync`` fails with:
  Config file not found: /root/.yadgar/config.yaml. Run 'yadgar config init' to create it.
Setup exits at step 8.

Fix: _step_config_sync() checks for config.yaml existence. If absent → run
``yadgar config init`` first (creates default), then run ``yadgar config sync``.
On reinstall (config already exists) only ``sync`` runs — preserves user edits.

Data dir convention: uses YADGAR_DIR env var (fallback ${HOME}/.yadgar),
consistent with _step_generate_units() and other setup.sh functions.

Runner note: pure static tests — no server/MCP dependencies.
Run with --noconftest to avoid autouse fixture failures:
  pytest yadgar/tests/test_v5_46_13_config_init_idempotent.py --noconftest
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"


@pytest.fixture(scope="module")
def setup_sh_text() -> str:
    assert SETUP_SH.exists(), f"yadgar-setup.sh not found at {SETUP_SH}"
    return SETUP_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def step_config_sync_body(setup_sh_text: str) -> str:
    """Extract _step_config_sync() function body from setup.sh."""
    match = re.search(
        r"_step_config_sync\(\)\s*\{(.+?)^}",
        setup_sh_text,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, (
        "_step_config_sync() function body not found in yadgar-setup.sh.\n"
        "Expected closing '}' at column 0."
    )
    return match.group(1)


class TestStepConfigSyncExistenceCheck:
    """Test 1: _step_config_sync checks for config.yaml existence before sync."""

    def test_step_checks_config_file_existence(self, step_config_sync_body: str) -> None:
        """_step_config_sync must check if config.yaml exists ([ ! -f ... ] or [ -f ... ])."""
        has_file_test = re.search(r"\[\s*[!-]\s*-f\s+|test\s+-f\s+", step_config_sync_body)
        assert has_file_test is not None, (
            "_step_config_sync() does not check for config.yaml existence.\n"
            "Fix: add 'if [ ! -f \"$config_file\" ]; then' guard before yadgar config init.\n"
            "Expected pattern: [ ! -f ... ] or [ -f ... ] or test -f ..."
        )


class TestStepConfigSyncFreshInstall:
    """Tests 2-3: fresh install (config absent) → init THEN sync."""

    def test_contains_config_init_call(self, step_config_sync_body: str) -> None:
        """Test 2a: _step_config_sync must call 'yadgar config init'."""
        assert "yadgar config init" in step_config_sync_body, (
            "_step_config_sync() does not call 'yadgar config init'.\n"
            "Fix: add 'run yadgar config init' inside the '! -f config_file' branch.\n"
            "Fresh install fails without this — yadgar config sync requires an existing file."
        )

    def test_contains_config_sync_call(self, step_config_sync_body: str) -> None:
        """Test 2b: _step_config_sync must call 'yadgar config sync'."""
        assert "yadgar config sync" in step_config_sync_body, (
            "_step_config_sync() does not call 'yadgar config sync'.\n"
            "Fix: keep 'run yadgar config sync' in _step_config_sync()."
        )

    def test_init_before_sync_in_function(self, step_config_sync_body: str) -> None:
        """Test 3: 'yadgar config init' must appear before 'yadgar config sync' in the function."""
        init_pos = step_config_sync_body.find("yadgar config init")
        sync_pos = step_config_sync_body.find("yadgar config sync")
        assert init_pos != -1, "yadgar config init not found in _step_config_sync()"
        assert sync_pos != -1, "yadgar config sync not found in _step_config_sync()"
        assert init_pos < sync_pos, (
            f"'yadgar config init' (pos {init_pos}) appears AFTER 'yadgar config sync' "
            f"(pos {sync_pos}) in _step_config_sync().\n"
            "Fix: run init inside the missing-file guard, then sync unconditionally after."
        )


class TestStepConfigSyncReinstall:
    """Test 4: reinstall (config present) → init NOT called unconditionally."""

    def test_init_is_conditional_not_unconditional(self, step_config_sync_body: str) -> None:
        """Test 4: 'yadgar config init' must be inside a conditional block (not always run).

        Pattern: init call must be preceded by a file-existence test in the same function.
        The init call must not appear at the top level (unconditionally) before the if block.
        """
        # Find the file-test guard and init call positions
        file_test_match = re.search(r"\[\s*!\s*-f\s+", step_config_sync_body)
        init_pos = step_config_sync_body.find("yadgar config init")

        assert file_test_match is not None, (
            "No '[ ! -f ... ]' guard found — init is unconditional.\n"
            "Fix: wrap 'run yadgar config init' in 'if [ ! -f \"$config_file\" ]; then ... fi'.\n"
            "Reinstall must NOT re-run init (it would overwrite user edits)."
        )
        guard_pos = file_test_match.start()
        assert guard_pos < init_pos, (
            f"File-existence guard (pos {guard_pos}) appears AFTER init call (pos {init_pos}).\n"
            "Fix: the '[ ! -f ... ]' check must precede the 'yadgar config init' call."
        )


class TestStaticSetupShChecks:
    """Test 5: static checks on the full setup.sh file."""

    def test_setup_sh_contains_config_init(self, setup_sh_text: str) -> None:
        """Test 5a: setup.sh must contain 'yadgar config init' (global static check)."""
        assert "yadgar config init" in setup_sh_text, (
            "setup.sh does not contain 'yadgar config init' anywhere.\n"
            "Fix: add init-if-missing logic to _step_config_sync()."
        )

    def test_setup_sh_contains_config_sync(self, setup_sh_text: str) -> None:
        """Test 5b: setup.sh must contain 'yadgar config sync' (global static check)."""
        assert "yadgar config sync" in setup_sh_text, (
            "setup.sh does not contain 'yadgar config sync' anywhere.\n"
            "Fix: _step_config_sync() must call yadgar config sync."
        )

    def test_config_init_appears_before_config_sync_globally(self, setup_sh_text: str) -> None:
        """Test 5c: in setup.sh, first 'config init' appears before first 'config sync'."""
        init_pos = setup_sh_text.find("yadgar config init")
        sync_pos = setup_sh_text.find("yadgar config sync")
        assert init_pos != -1, "yadgar config init not found in setup.sh"
        assert sync_pos != -1, "yadgar config sync not found in setup.sh"
        assert init_pos < sync_pos, (
            f"First 'yadgar config init' (pos {init_pos}) appears AFTER "
            f"first 'yadgar config sync' (pos {sync_pos}) in setup.sh.\n"
            "Fix: ensure init-if-missing runs before sync in _step_config_sync()."
        )


class TestDataDirConvention:
    """Test 6: step 8 uses YADGAR_DIR (consistent with other setup.sh steps)."""

    def test_step_uses_yadgar_dir_variable(self, step_config_sync_body: str) -> None:
        """Test 6: _step_config_sync must use YADGAR_DIR env var (not YADGAR_DATA_DIR).

        Other setup.sh functions (e.g. _step_generate_units) use:
            local yadgar_dir="${YADGAR_DIR:-${HOME}/.yadgar}"
        Step 8 must be consistent.
        """
        uses_yadgar_dir = "YADGAR_DIR" in step_config_sync_body
        assert uses_yadgar_dir, (
            "_step_config_sync() does not reference YADGAR_DIR.\n"
            "Fix: use 'local yadgar_dir=\"${YADGAR_DIR:-${HOME}/.yadgar}\"' consistent "
            "with _step_generate_units() and other setup.sh functions.\n"
            "Do NOT use YADGAR_DATA_DIR — that variable is not used elsewhere in setup.sh."
        )
