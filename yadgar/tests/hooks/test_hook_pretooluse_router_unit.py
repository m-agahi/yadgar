"""Unit tests for yadgar/core/hooks/pretooluse-router.py — importlib-based.

HOOKS train Car 1. The router subsumes db-lockdown-check.py and adds three
mechanical HARD-RULE guards (git-commit-bypass, terraform-family, git-push-to-
default). TDD: written before implementation (red → green).

Strategy: load the hyphen-named module via importlib (same trick as the old
test_hook_db_lockdown_check_unit.py). Exercise the peel pipeline as pure
functions, the guards directly, and end-to-end main() by patching sys.stdin +
print. G3 (git push) shells out to git — subprocess.run is mocked.
"""

from __future__ import annotations

import io
import json
import os
import resource
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_HOOK = Path(__file__).parent.parent.parent / "core" / "hooks" / "pretooluse-router.py"


def _load_hook():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pretooluse_router_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main(mod, stdin_json: str, *, cwd: str | None = None) -> dict:
    captured: list[str] = []
    with (
        patch.object(mod.sys, "stdin", io.StringIO(stdin_json)),
        patch("builtins.print", side_effect=lambda s: captured.append(s)),
    ):
        mod.main()
    assert captured, "main() printed nothing"
    return json.loads(captured[-1])


def _payload(command: str, *, tool_name: str = "Bash", cwd: str = "/home/x/repo") -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": {"command": command}, "cwd": cwd})


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


class TestSchema:
    def test_allow_schema(self):
        mod = _load_hook()
        out = mod._allow()
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "systemMessage" not in out

    def test_deny_schema_both_reason_fields(self):
        mod = _load_hook()
        out = mod._deny("agent reason", "human reason")
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"] == "agent reason"
        assert out["systemMessage"] == "human reason"

    def test_deny_single_arg_reason_shared(self):
        mod = _load_hook()
        out = mod._deny("blocked")
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"
        assert out["systemMessage"] == "blocked"


# ---------------------------------------------------------------------------
# Peel pipeline — pure functions
# ---------------------------------------------------------------------------


class TestSegment:
    def test_segment_on_and(self):
        mod = _load_hook()
        segs = mod.segment(mod.tokenize("cd foo && tofu plan"))
        assert ["tofu", "plan"] in segs

    def test_segment_on_semicolon_and_pipe(self):
        mod = _load_hook()
        segs = mod.segment(mod.tokenize("a ; b | c"))
        assert ["a"] in segs and ["b"] in segs and ["c"] in segs

    def test_segment_strips_subshell_parens(self):
        mod = _load_hook()
        segs = mod.segment(mod.tokenize("(terraform apply)"))
        assert ["terraform", "apply"] in segs


class TestPeelWrappers:
    def test_sudo(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["sudo", "terraform", "apply"]) == ["terraform", "apply"]

    def test_sudo_with_user_flag(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["sudo", "-u", "bob", "terraform", "apply"]) == [
            "terraform",
            "apply",
        ]

    def test_sudo_double_dash(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["sudo", "--", "terraform", "apply"]) == ["terraform", "apply"]

    def test_env_with_assignment(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["env", "FOO=bar", "terraform", "apply"]) == ["terraform", "apply"]

    def test_nice(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["nice", "terraform", "apply"]) == ["terraform", "apply"]

    def test_timeout_positional_duration(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["timeout", "300", "terraform", "apply"]) == ["terraform", "apply"]

    def test_unknown_wrapper_left_as_is(self):
        mod = _load_hook()
        assert mod.peel_wrappers(["frobnicate", "x"]) == ["frobnicate", "x"]


class TestPeelGitGlobals:
    def test_dash_c_path_space_form(self):
        mod = _load_hook()
        argv, pre_c = mod.peel_git_globals(["git", "-C", "/p", "push", "origin", "master"])
        assert argv == ["push", "origin", "master"]
        assert pre_c == []

    def test_dash_lowercase_c_config_pair(self):
        mod = _load_hook()
        argv, pre_c = mod.peel_git_globals(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "x"]
        )
        assert argv == ["commit", "-m", "x"]
        assert "commit.gpgsign=false" in pre_c

    def test_git_dir_equals_form(self):
        mod = _load_hook()
        argv, _ = mod.peel_git_globals(["git", "--git-dir=/p/.git", "push"])
        assert argv == ["push"]

    def test_standalone_paginate(self):
        mod = _load_hook()
        argv, _ = mod.peel_git_globals(["git", "-P", "push"])
        assert argv == ["push"]

    def test_no_subcommand(self):
        mod = _load_hook()
        argv, _ = mod.peel_git_globals(["git", "--version"])
        # --version is a global-ish; resolved argv has no push/commit subcommand
        assert "push" not in argv and "commit" not in argv


# ---------------------------------------------------------------------------
# End-to-end AC-UNIT matrix
# ---------------------------------------------------------------------------


class TestMatrixAllowDeny:
    def _decide(self, mod, cmd: str, *, tool_name="Bash", cwd="/home/x/repo") -> str:
        out = _run_main(mod, _payload(cmd, tool_name=tool_name, cwd=cwd))
        return out["hookSpecificOutput"]["permissionDecision"]

    # Fixtures that need no git shell-out (G1/G2/G4 + non-Bash + fail-soft)

    def test_01_benign_allow(self):
        mod = _load_hook()
        assert self._decide(mod, "ls -la /tmp") == "allow"

    def test_02_non_bash_early_exit(self):
        mod = _load_hook()
        out = _run_main(mod, json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}))
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_03_git_commit_no_verify_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "git commit --no-verify -m x") == "deny"

    def test_04_git_commit_no_gpg_sign_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "git commit --no-gpg-sign -m x") == "deny"

    def test_05_git_dash_c_gpgsign_false_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "git -c commit.gpgsign=false commit -m x") == "deny"

    def test_05b_git_commit_c_reuse_message_allow(self):
        mod = _load_hook()
        assert self._decide(mod, "git commit -c HEAD -m x") == "allow"

    def test_06_false_positive_terraform_in_message_allow(self):
        mod = _load_hook()
        assert self._decide(mod, 'git commit -m "fix terraform bug"') == "allow"

    def test_08_terraform_apply_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "terraform apply") == "deny"

    def test_09_compound_tofu_plan_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "cd foo && tofu plan") == "deny"

    def test_10_tfp_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "tfp") == "deny"

    def test_11_docker_run_terraform_image_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "docker run hashicorp/terraform:1.5 plan") == "deny"

    def test_12_nix_run_terraform_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "nix run nixpkgs#terraform -- plan") == "deny"

    def test_13_gh_pr_comment_digger_deny(self):
        mod = _load_hook()
        assert self._decide(mod, 'gh pr comment 5 --body "digger apply"') == "deny"

    def test_14_echo_digger_mention_allow(self):
        mod = _load_hook()
        assert self._decide(mod, 'echo "digger apply is scary"') == "allow"

    def test_19_docker_exec_yadgar_db_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "docker exec yadgar-db psql") == "deny"

    def test_20_docker_exec_other_container_allow(self):
        mod = _load_hook()
        assert self._decide(mod, "docker exec my-app bash") == "allow"

    def test_21_malformed_stdin_fails_closed_deny(self):
        # Ledger 1567: the router is a security guard, so an input it cannot
        # parse is denied, not waved through. Claude Code always sends JSON.
        mod = _load_hook()
        out = _run_main(mod, "{broken")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Wrapper-peel deny fixtures (26-32)

    def test_26_sudo_terraform_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "sudo terraform apply") == "deny"

    def test_27_env_terraform_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "env FOO=bar terraform apply") == "deny"

    def test_28_timeout_terraform_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "timeout 300 terraform apply") == "deny"

    def test_29_nice_terraform_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "nice terraform apply") == "deny"

    def test_30_bash_c_terraform_recursion_deny(self):
        mod = _load_hook()
        assert self._decide(mod, 'bash -c "terraform apply"') == "deny"

    def test_31_prefix_long_flag_no_verif_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "git commit --no-verif -m x") == "deny"

    def test_32_bundled_short_nm_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "git commit -nm x") == "deny"

    def test_24_git_dash_c_commit_no_verify_deny(self):
        mod = _load_hook()
        assert self._decide(mod, "git -C /p commit --no-verify -m x") == "deny"

    # Out-of-reach documented ALLOW fixtures (33, 34)

    def test_33_alias_out_of_reach_allow(self):
        mod = _load_hook()
        assert self._decide(mod, "alias tf=terraform; tf apply") == "allow"

    def test_34_command_substitution_out_of_reach_allow(self):
        mod = _load_hook()
        assert self._decide(mod, "$(terraform apply)") == "allow"

    def test_22_guard_raises_fails_closed_deny(self):
        mod = _load_hook()
        # Ledger 1567: a raising guard denies. It used to print allow, so one
        # router bug switched every guard off.
        with patch.object(mod, "guard_terraform_family", side_effect=RuntimeError("boom")):
            out = _run_main(mod, _payload("echo hello"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "boom" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# G3 — git push to default (subprocess mocked)
# ---------------------------------------------------------------------------


class TestG3PushDefault:
    """Mock _git() so no real git is needed. _git(args, cwd) returns str|None."""

    def _decide_with_git(self, mod, cmd, *, default="master", repo="yadgar", origin_head=True):
        def fake_git(args, cwd):
            if args[:1] == ["symbolic-ref"]:
                return f"refs/remotes/origin/{default}" if origin_head else None
            if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
                return f"/home/x/{repo}"
            if args[:1] == ["rev-parse"] and "--abbrev-ref" in args:
                return default  # current branch == default by default
            return None

        with patch.object(mod, "_git", side_effect=fake_git):
            out = _run_main(mod, _payload(cmd, cwd=f"/home/x/{repo}"))
        return out["hookSpecificOutput"]["permissionDecision"]

    def test_15_push_default_deny(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push origin master") == "deny"

    def test_16_allowlist_nix_allow(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push origin master", repo="nix") == "allow"

    def test_16b_allowlist_ledger_allow(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push origin master", repo="ledger") == "allow"

    def test_16c_allowlist_ostad_allow(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push origin master", repo="ostad") == "allow"

    def test_17_push_non_default_allow(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push origin feature/x") == "allow"

    def test_18_force_refspec_to_default_deny(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push --force origin HEAD:master") == "deny"

    def test_23_git_dash_C_push_default_deny(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git -C /p push origin master") == "deny"

    def test_25_git_dash_c_nongpgsign_push_default_deny(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git -c core.x=y push origin master") == "deny"

    def test_18c_origin_head_unset_fail_open_allow(self):
        mod = _load_hook()
        assert self._decide_with_git(mod, "git push origin master", origin_head=False) == "allow"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfig:
    def test_missing_config_uses_defaults(self, tmp_path):
        mod = _load_hook()
        cfg = mod.load_config(tmp_path / "nope.json")
        assert "nix" in cfg["push_default_allowlist"]
        assert cfg["disabled_guards"] == []

    def test_corrupt_config_uses_defaults(self, tmp_path):
        mod = _load_hook()
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        cfg = mod.load_config(p)
        assert "nix" in cfg["push_default_allowlist"]

    def test_user_config_read(self, tmp_path):
        mod = _load_hook()
        p = tmp_path / "ok.json"
        p.write_text(
            json.dumps({"version": 1, "push_default_allowlist": ["myrepo"], "disabled_guards": []})
        )
        cfg = mod.load_config(p)
        assert cfg["push_default_allowlist"] == ["myrepo"]

    def test_disabled_guard_skips(self, tmp_path):
        mod = _load_hook()
        # Disable terraform guard via config → terraform apply allowed.
        p = tmp_path / "cfg.json"
        p.write_text(
            json.dumps(
                {
                    "version": 1,
                    "push_default_allowlist": [],
                    "disabled_guards": ["terraform_family"],
                }
            )
        )
        with patch.object(mod, "_config_path", return_value=p):
            out = _run_main(mod, _payload("terraform apply"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# G5 — writes to the hook-exceptions config itself (2026-07-28 incident fix)
# ---------------------------------------------------------------------------


def _edit_payload(file_path: str, *, tool_name: str = "Edit") -> str:
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    return json.dumps(
        {"tool_name": tool_name, "tool_input": {key: file_path}, "cwd": "/home/x/repo"}
    )


class TestG5HookConfigTamper:
    def test_bash_echo_redirect_into_config_deny(self):
        mod = _load_hook()
        out = _run_main(mod, _payload("echo x > ~/.claude/yadgar-hook-exceptions.json"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_sed_i_on_config_deny(self):
        mod = _load_hook()
        cmd = "sed -i s/nix/yadgar/ ~/.claude/yadgar-hook-exceptions.json"
        assert _run_main(mod, _payload(cmd))["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_tee_into_config_deny(self):
        mod = _load_hook()
        cmd = "echo x | tee ~/.claude/yadgar-hook-exceptions.json"
        assert _run_main(mod, _payload(cmd))["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_plain_read_of_config_allow(self):
        mod = _load_hook()
        cmd = "cat ~/.claude/yadgar-hook-exceptions.json"
        assert _run_main(mod, _payload(cmd))["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_bash_unrelated_redirect_allow(self):
        mod = _load_hook()
        assert (
            _run_main(mod, _payload("echo x > /tmp/out.txt"))["hookSpecificOutput"][
                "permissionDecision"
            ]
            == "allow"
        )

    def test_edit_tool_targeting_config_deny(self, tmp_path):
        mod = _load_hook()
        cfg = tmp_path / "yadgar-hook-exceptions.json"
        cfg.write_text("{}")
        with patch.object(mod, "_config_path", return_value=cfg):
            out = _run_main(mod, _edit_payload(str(cfg)))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_write_tool_targeting_config_deny(self, tmp_path):
        mod = _load_hook()
        cfg = tmp_path / "yadgar-hook-exceptions.json"
        with patch.object(mod, "_config_path", return_value=cfg):
            out = _run_main(mod, _edit_payload(str(cfg), tool_name="Write"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_notebook_edit_targeting_config_deny(self, tmp_path):
        mod = _load_hook()
        cfg = tmp_path / "yadgar-hook-exceptions.json"
        with patch.object(mod, "_config_path", return_value=cfg):
            out = _run_main(mod, _edit_payload(str(cfg), tool_name="NotebookEdit"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_edit_tool_unrelated_file_allow(self):
        mod = _load_hook()
        out = _run_main(mod, _edit_payload("/home/x/repo/README.md"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_edit_tool_expands_user_tilde(self, tmp_path, monkeypatch):
        mod = _load_hook()
        cfg = tmp_path / ".claude" / "yadgar-hook-exceptions.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("{}")
        monkeypatch.setenv("HOME", str(tmp_path))
        out = _run_main(mod, _edit_payload("~/.claude/yadgar-hook-exceptions.json"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_disabled_guard_skips_edit_path(self, tmp_path):
        mod = _load_hook()
        cfg = tmp_path / "yadgar-hook-exceptions.json"
        cfg.write_text(
            json.dumps(
                {
                    "version": 1,
                    "push_default_allowlist": [],
                    "disabled_guards": ["hook_config_tamper"],
                }
            )
        )
        with patch.object(mod, "_config_path", return_value=cfg):
            out = _run_main(mod, _edit_payload(str(cfg)))
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_commit_message_mentioning_filename_allow(self):
        """Regression: prose describing this guard must not self-trigger it.

        2026-07-28: a whole-command substring scan (basename anywhere + any
        write marker anywhere) blocked a real `git commit -m "..."` whose
        message merely *described* the incident and mentioned both
        `yadgar-hook-exceptions.json` and `sed -i` in prose, while the actual
        command wrote nothing. Must be allowed — the write target (or lack of
        one) is what matters, not co-occurring words.
        """
        mod = _load_hook()
        msg = (
            "fix(hooks): close bypass of push-default guard\n\n"
            "A subagent added itself to push_default_allowlist in "
            "yadgar-hook-exceptions.json, pushed to master, then reverted it. "
            "Adds a guard against writes via redirect, sed -i, tee, cp, mv, "
            "truncate, or a python one-liner that writes the file directly."
        )
        cmd = f'git commit -m "{msg}"'
        out = _run_main(mod, _payload(cmd))
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_unrelated_write_plus_prose_mention_allow(self):
        """A command that both mentions the filename AND writes some OTHER
        file (e.g. a heredoc to a scratch file) must not be denied — only a
        write actually TARGETING the config file should deny."""
        mod = _load_hook()
        cmd = 'echo "note: see yadgar-hook-exceptions.json and sed -i usage" > /tmp/notes.txt'
        out = _run_main(mod, _payload(cmd))
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# Ledger 1567 — the exceptions config and every error path fail CLOSED
# ---------------------------------------------------------------------------
#
# Two defects in load_config made the whole guard fail open:
#   1. read_text() on a FIFO (or a symlink to /dev/zero) blocks until Claude
#      Code's hook timeout, and a timed-out hook does not block the tool call.
#   2. set(config["disabled_guards"]) raised TypeError on an unhashable item,
#      and main()'s top-level except then printed allow.
# A config the router cannot trust must leave EVERY guard on and say so on
# stderr. The FIFO and /dev/zero cases run the hook as a subprocess with a
# timeout and an address-space cap, so a regression hangs or fails one child
# process and never the test runner.

_SUBPROC_TIMEOUT = 20
_ADDRESS_SPACE_CAP = 2 * 1024**3
_NOTICE = "[yadgar-pretooluse-router]"


def _cap_address_space() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (_ADDRESS_SPACE_CAP, _ADDRESS_SPACE_CAP))


def _run_hook_subprocess(home: Path, stdin_json: str) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OTEL_SDK_DISABLED": "true",
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=stdin_json,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROC_TIMEOUT,
        preexec_fn=_cap_address_space,
        check=False,
    )


def _decision(proc: subprocess.CompletedProcess) -> str:
    """The decision Claude Code acts on: exit 2 blocks, else stdout JSON decides."""
    if proc.returncode == 2:
        return "deny"
    assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}"
    last = proc.stdout.strip().splitlines()[-1]
    return json.loads(last)["hookSpecificOutput"]["permissionDecision"]


def _claude_dir(home: Path) -> Path:
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_cfg(path: Path, obj: object) -> Path:
    path.write_text(json.dumps(obj))
    return path


def _run_with_cfg(mod, cfg_path: Path, command: str) -> dict:
    with patch.object(mod, "_config_path", return_value=cfg_path):
        return _run_main(mod, _payload(command))


class TestConfigFailsClosedSubprocess:
    """Special files at the config path: the hook must neither hang nor open up."""

    def test_fifo_does_not_hang_and_keeps_guards_on(self, tmp_path):
        os.mkfifo(_claude_dir(tmp_path) / "yadgar-hook-exceptions.json")
        proc = _run_hook_subprocess(tmp_path, _payload("terraform apply"))
        assert _decision(proc) == "deny"
        assert _NOTICE in proc.stderr

    def test_fifo_benign_command_still_allowed(self, tmp_path):
        os.mkfifo(_claude_dir(tmp_path) / "yadgar-hook-exceptions.json")
        proc = _run_hook_subprocess(tmp_path, _payload("echo hello"))
        assert _decision(proc) == "allow"
        assert _NOTICE in proc.stderr

    def test_dev_zero_symlink_does_not_hang_and_keeps_guards_on(self, tmp_path):
        (_claude_dir(tmp_path) / "yadgar-hook-exceptions.json").symlink_to("/dev/zero")
        proc = _run_hook_subprocess(tmp_path, _payload("terraform apply"))
        assert _decision(proc) == "deny"
        assert _NOTICE in proc.stderr

    def test_symlink_to_disabling_file_is_not_honoured(self, tmp_path):
        # A symlink lets an agent edit the real target, which G5 does not
        # watch. The installer writes a regular file, so a symlink is refused.
        target = _write_cfg(tmp_path / "elsewhere.json", {"disabled_guards": ["terraform_family"]})
        (_claude_dir(tmp_path) / "yadgar-hook-exceptions.json").symlink_to(target)
        proc = _run_hook_subprocess(tmp_path, _payload("terraform apply"))
        assert _decision(proc) == "deny"
        assert _NOTICE in proc.stderr

    def test_valid_file_still_disables_guard_subprocess(self, tmp_path):
        _write_cfg(
            _claude_dir(tmp_path) / "yadgar-hook-exceptions.json",
            {"version": 1, "push_default_allowlist": [], "disabled_guards": ["terraform_family"]},
        )
        proc = _run_hook_subprocess(tmp_path, _payload("terraform apply"))
        assert _decision(proc) == "allow"
        assert _NOTICE not in proc.stderr

    def test_missing_file_is_silent_defaults(self, tmp_path):
        _claude_dir(tmp_path)
        proc = _run_hook_subprocess(tmp_path, _payload("terraform apply"))
        assert _decision(proc) == "deny"
        assert _NOTICE not in proc.stderr

    def test_router_exception_fails_closed_subprocess(self, tmp_path):
        # A JSON array is valid JSON but not an object: data.get() raises
        # inside the router. That fault must deny, end to end.
        _claude_dir(tmp_path)
        proc = _run_hook_subprocess(tmp_path, "[]")
        assert _decision(proc) == "deny"
        assert _NOTICE in proc.stderr


class TestConfigFailsClosed:
    """Malformed content at the config path: every guard stays on, with a notice."""

    def _assert_rejected(self, mod, cfg_path: Path, capsys) -> None:
        cfg = mod.load_config(cfg_path)
        assert cfg["disabled_guards"] == []
        assert cfg["push_default_allowlist"] == ["nix", "ledger", "ostad"]
        assert _NOTICE in capsys.readouterr().err

    def test_oversize_file_rejected(self, tmp_path, capsys):
        mod = _load_hook()
        p = tmp_path / "big.json"
        p.write_text(json.dumps({"disabled_guards": ["terraform_family"], "pad": "x" * 70000}))
        self._assert_rejected(mod, p, capsys)

    def test_oversize_file_keeps_guard_on_end_to_end(self, tmp_path):
        mod = _load_hook()
        p = tmp_path / "big.json"
        p.write_text(json.dumps({"disabled_guards": ["terraform_family"], "pad": "x" * 70000}))
        out = _run_with_cfg(mod, p, "terraform apply")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_nested_list_in_disabled_guards_rejected(self, tmp_path, capsys):
        mod = _load_hook()
        p = _write_cfg(tmp_path / "c.json", {"disabled_guards": [["x"]]})
        self._assert_rejected(mod, p, capsys)

    def test_nested_list_keeps_guard_on_end_to_end(self, tmp_path):
        # Before the fix: set([["x"]]) raised TypeError and main() printed allow.
        mod = _load_hook()
        p = _write_cfg(tmp_path / "c.json", {"disabled_guards": [["x"]]})
        out = _run_with_cfg(mod, p, "terraform apply")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_nested_list_keeps_edit_guard_on(self, tmp_path):
        mod = _load_hook()
        p = _write_cfg(tmp_path / "yadgar-hook-exceptions.json", {"disabled_guards": [["x"]]})
        with patch.object(mod, "_config_path", return_value=p):
            out = _run_main(mod, _edit_payload(str(p)))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_non_list_disabled_guards_rejected(self, tmp_path, capsys):
        mod = _load_hook()
        p = _write_cfg(tmp_path / "c.json", {"disabled_guards": "terraform_family"})
        self._assert_rejected(mod, p, capsys)

    def test_non_string_item_rejected(self, tmp_path, capsys):
        mod = _load_hook()
        p = _write_cfg(tmp_path / "c.json", {"disabled_guards": ["terraform_family", 7]})
        self._assert_rejected(mod, p, capsys)

    def test_bad_allowlist_rejects_whole_file(self, tmp_path, capsys):
        # One bad field voids the file: its valid disabled_guards is NOT honoured.
        mod = _load_hook()
        p = _write_cfg(
            tmp_path / "c.json",
            {"push_default_allowlist": {"a": 1}, "disabled_guards": ["terraform_family"]},
        )
        self._assert_rejected(mod, p, capsys)

    def test_non_object_top_level_rejected(self, tmp_path, capsys):
        mod = _load_hook()
        p = _write_cfg(tmp_path / "c.json", ["terraform_family"])
        self._assert_rejected(mod, p, capsys)

    def test_corrupt_json_rejected_with_notice(self, tmp_path, capsys):
        mod = _load_hook()
        p = tmp_path / "c.json"
        p.write_text("{not json")
        self._assert_rejected(mod, p, capsys)

    def test_deeply_nested_json_rejected(self, tmp_path, capsys):
        # json.loads raises RecursionError, not ValueError, on deep nesting.
        mod = _load_hook()
        p = tmp_path / "c.json"
        p.write_text("[" * 30000 + "]" * 30000)
        self._assert_rejected(mod, p, capsys)

    def test_directory_at_path_rejected(self, tmp_path, capsys):
        mod = _load_hook()
        p = tmp_path / "c.json"
        p.mkdir()
        self._assert_rejected(mod, p, capsys)

    def test_valid_file_disables_listed_guard(self, tmp_path, capsys):
        mod = _load_hook()
        p = _write_cfg(
            tmp_path / "c.json",
            {
                "version": 1,
                "push_default_allowlist": ["nix"],
                "disabled_guards": ["terraform_family"],
            },
        )
        out = _run_with_cfg(mod, p, "terraform apply")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert _NOTICE not in capsys.readouterr().err

    def test_valid_file_leaves_other_guards_on(self, tmp_path):
        mod = _load_hook()
        p = _write_cfg(tmp_path / "c.json", {"disabled_guards": ["terraform_family"]})
        out = _run_with_cfg(mod, p, "git commit --no-verify -m x")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_installed_default_shape_accepted(self, tmp_path, capsys):
        # The exact shape the installer seeds must load without a notice.
        mod = _load_hook()
        p = _write_cfg(
            tmp_path / "c.json",
            {
                "version": 1,
                "push_default_allowlist": ["nix", "ledger", "ostad"],
                "disabled_guards": [],
            },
        )
        cfg = mod.load_config(p)
        assert cfg["push_default_allowlist"] == ["nix", "ledger", "ostad"]
        assert cfg["disabled_guards"] == []
        assert _NOTICE not in capsys.readouterr().err


class TestGitUnavailableFailsClosed:
    """G3: git that cannot RUN denies the push; git that ran and said no stays allow."""

    def test_git_timeout_on_push_denies(self):
        mod = _load_hook()
        boom = subprocess.TimeoutExpired(cmd="git", timeout=5)
        with patch.object(mod.subprocess, "run", side_effect=boom):
            out = _run_main(mod, _payload("git push origin master"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_git_missing_on_push_denies(self):
        mod = _load_hook()
        with patch.object(mod.subprocess, "run", side_effect=FileNotFoundError("git")):
            out = _run_main(mod, _payload("git push origin master"))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
