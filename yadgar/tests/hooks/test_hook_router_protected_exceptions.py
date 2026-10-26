"""Protected exceptions file — the engine router honours yaadgaar's protected mode.

Ruling 2026-10-26 (ledger 1812): the yadgar memory engine and the yaadgaar
pre-tool-guard read ONE hook exceptions file. yaadgaar v0.1.31 (ADR-1028)
trusts `/etc/yaadgaar/<file>` only when the file and every directory above it
are owned by uid 0 and not group- or other-writable, judged by fstat on each
handle opened with O_NOFOLLOW. These tests pin the same three modes here:

* absent  — no protected DIRECTORY → the home copy is read (today's behaviour)
* valid   — the protected file is read; the home copy is ignored
* invalid — anything else → no exception from either file is honoured

No test can create a root-owned file, so the trusted uid is injected as this
process's uid and the "current" uid as a different one, exactly as yaadgaar's
own fixture does. One test pins the production default (path + uid 0).
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOK = Path(__file__).parent.parent.parent / "core" / "hooks" / "pretooluse-router.py"
_NAME = "yadgar-hook-exceptions.json"


def _load_hook():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pretooluse_router_protected", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, body: str | None, mod) -> tuple[object, Path]:
    """A valid protected tree under tmp_path/root holding *body* (None: no file)."""
    root = tmp_path / "root"
    pdir = root / "etc" / "yaadgaar"
    pdir.mkdir(parents=True)
    for d in (root, root / "etc", pdir):
        d.chmod(0o755)
    f = pdir / _NAME
    if body is not None:
        f.write_text(body)
        f.chmod(0o644)
    me = os.getuid()
    loc = mod.ProtectedLocation(
        anchor=str(root),
        dirs=("etc", "yaadgaar"),
        file=_NAME,
        trusted_uid=me,
        current_uid=me + 1,
    )
    return loc, f


def _home(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "home" / _NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg))
    return p


def _cfg(**kw) -> str:
    base = {"version": 1, "push_default_allowlist": [], "disabled_guards": []}
    base.update(kw)
    return json.dumps(base)


def _run_main(mod, stdin_json: str) -> str:
    captured: list[str] = []
    with (
        patch.object(mod.sys, "stdin", io.StringIO(stdin_json)),
        patch("builtins.print", side_effect=lambda s: captured.append(s)),
    ):
        mod.main()
    return json.loads(captured[-1])["hookSpecificOutput"]["permissionDecision"]


def _bash(cmd: str, cwd: str = "/home/x/repo") -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd})


def _edit(path: str) -> str:
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": path}, "cwd": "/x"})


def _with(mod, loc, home: Path | None):
    """Point the router at *loc* (protected) and *home* (user copy)."""
    stack = [patch.object(mod, "_protected_location", return_value=loc)]
    if home is not None:
        stack.append(patch.object(mod, "_config_path", return_value=home))
    return stack


class _Ctx:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def using(mod, loc, home=None):
    return _Ctx(_with(mod, loc, home))


# --------------------------------------------------------------------------- #
# Production default
# --------------------------------------------------------------------------- #


class TestProductionDefault:
    def test_default_location_is_etc_yaadgaar_owned_by_root(self):
        mod = _load_hook()
        loc = mod._protected_location()
        assert loc is not None
        assert loc.anchor == "/"
        expected = (
            ("private", "etc", "yaadgaar") if sys.platform == "darwin" else ("etc", "yaadgaar")
        )
        assert loc.dirs == expected
        assert loc.file == _NAME
        assert loc.trusted_uid == 0
        assert loc.current_uid == os.geteuid()


# --------------------------------------------------------------------------- #
# verify_protected — the walk
# --------------------------------------------------------------------------- #


class TestVerify:
    def test_valid_tree_returns_text(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(disabled_guards=["db_lockdown"]), mod)
        verified = mod.verify_protected(loc)
        assert verified.state == "valid"
        assert json.loads(verified.text)["disabled_guards"] == ["db_lockdown"]

    def test_absent_directory_is_absent(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        loc = mod.ProtectedLocation(
            loc.anchor, ("etc", "nope"), loc.file, loc.trusted_uid, loc.current_uid
        )
        assert mod.verify_protected(loc)[0] == "absent"

    def test_directory_without_file_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, None, mod)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_symlink_at_file_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, f = _tree(tmp_path, None, mod)
        real = tmp_path / "real.json"
        real.write_text(_cfg())
        real.chmod(0o644)
        f.symlink_to(real)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_symlink_at_ancestor_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        root = Path(loc.anchor)
        (root / "etc").rename(root / "etc-real")
        (root / "etc").symlink_to(root / "etc-real")
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_symlink_at_anchor_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        link = tmp_path / "anchor-link"
        link.symlink_to(loc.anchor)
        loc = mod.ProtectedLocation(str(link), loc.dirs, loc.file, loc.trusted_uid, loc.current_uid)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_dangling_symlink_at_protected_dir_is_invalid_not_absent(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        pdir = Path(loc.anchor) / "etc" / "yaadgaar"
        (pdir / _NAME).unlink()
        pdir.rmdir()
        pdir.symlink_to(tmp_path / "nowhere")
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_wrong_owner_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        loc = mod.ProtectedLocation(
            loc.anchor, loc.dirs, loc.file, os.getuid() + 7, os.getuid() + 1
        )
        assert mod.verify_protected(loc)[0] == "invalid"

    @pytest.mark.parametrize("mode", [0o664, 0o646])
    def test_group_or_other_writable_file_is_invalid(self, tmp_path, mode):
        mod = _load_hook()
        loc, f = _tree(tmp_path, _cfg(), mod)
        f.chmod(mode)
        assert mod.verify_protected(loc)[0] == "invalid"

    @pytest.mark.parametrize("which", ["root", "etc", "yaadgaar"])
    @pytest.mark.parametrize("mode", [0o775, 0o757])
    def test_group_or_other_writable_ancestor_is_invalid(self, tmp_path, which, mode):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        root = Path(loc.anchor)
        target = {"root": root, "etc": root / "etc", "yaadgaar": root / "etc" / "yaadgaar"}
        target[which].chmod(mode)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_fifo_in_place_of_file_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, f = _tree(tmp_path, None, mod)
        os.mkfifo(f, 0o644)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_oversized_file_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, f = _tree(tmp_path, " " * (64 * 1024 + 1), mod)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_guard_running_as_trusted_uid_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        loc = mod.ProtectedLocation(loc.anchor, loc.dirs, loc.file, os.getuid(), os.getuid())
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_unknown_current_uid_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        loc = mod.ProtectedLocation(loc.anchor, loc.dirs, loc.file, os.getuid(), None)
        assert mod.verify_protected(loc)[0] == "invalid"

    def test_permission_error_on_presence_check_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        with patch.object(mod.os, "lstat", side_effect=PermissionError(13, "denied")):
            assert mod.verify_protected(loc)[0] == "invalid"

    def test_permission_error_on_open_is_invalid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        real_open = os.open

        def deny_file(path, flags, *a, **kw):
            if str(path).endswith(_NAME):
                raise PermissionError(13, "denied")
            return real_open(path, flags, *a, **kw)

        with patch.object(mod.os, "open", side_effect=deny_file):
            assert mod.verify_protected(loc)[0] == "invalid"


class TestJudge:
    """The per-component judgement is pure, so a root-owned case is still a test."""

    @staticmethod
    def _st(uid: int, mode: int) -> os.stat_result:
        return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))

    def test_root_owned_0644_passes(self):
        mod = _load_hook()
        mod._judge("/etc/yaadgaar/x", self._st(0, 0o100644), 0)

    def test_root_owned_0755_dir_passes(self):
        mod = _load_hook()
        mod._judge("/etc", self._st(0, 0o040755), 0)

    @pytest.mark.parametrize("uid", [1, 1000, 65534])
    def test_non_root_owner_rejected(self, uid):
        mod = _load_hook()
        with pytest.raises(mod.ConfigRejectedError, match="owned by uid"):
            mod._judge("/etc/yaadgaar/x", self._st(uid, 0o100644), 0)

    @pytest.mark.parametrize("mode", [0o100664, 0o100646, 0o100666, 0o041777, 0o040775])
    def test_group_or_other_write_rejected(self, mode):
        mod = _load_hook()
        with pytest.raises(mod.ConfigRejectedError, match="writable"):
            mod._judge("/etc/yaadgaar/x", self._st(0, mode), 0)

    def test_sticky_dir_not_exempt(self):
        mod = _load_hook()
        # yaadgaar's sticky exemption is for binary paths only, never this tree.
        with pytest.raises(mod.ConfigRejectedError):
            mod._judge("/etc", self._st(0, 0o041775), 0)


# --------------------------------------------------------------------------- #
# load_config — which file wins
# --------------------------------------------------------------------------- #


class TestLoadConfig:
    def test_valid_protected_file_is_used(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(
            tmp_path, _cfg(push_default_allowlist=["myrepo"], disabled_guards=["db_lockdown"]), mod
        )
        with using(mod, loc, _home(tmp_path, {"disabled_guards": []})):
            cfg = mod.load_config()
        assert cfg["disabled_guards"] == ["db_lockdown"]
        assert cfg["push_default_allowlist"] == ["myrepo"]

    def test_home_copy_ignored_when_protected_valid(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        home = _home(tmp_path, {"disabled_guards": ["terraform_family"]})
        with using(mod, loc, home):
            cfg = mod.load_config()
            cfg_explicit = mod.load_config(home)
        assert cfg["disabled_guards"] == []
        assert cfg_explicit["disabled_guards"] == []

    def test_absent_protected_dir_falls_back_to_home(self, tmp_path):
        mod = _load_hook()
        loc = mod.ProtectedLocation(
            str(tmp_path / "missing-root"), ("etc", "yaadgaar"), _NAME, os.getuid(), os.getuid() + 1
        )
        home = _home(tmp_path, {"disabled_guards": ["terraform_family"]})
        with using(mod, loc, home):
            cfg = mod.load_config()
        assert cfg["disabled_guards"] == ["terraform_family"]

    def test_no_protected_mode_platform_falls_back_to_home(self, tmp_path):
        mod = _load_hook()
        home = _home(tmp_path, {"disabled_guards": ["terraform_family"]})
        with using(mod, None, home):
            assert mod.load_config()["disabled_guards"] == ["terraform_family"]

    def test_invalid_protected_honours_no_exceptions_and_not_home(self, tmp_path):
        mod = _load_hook()
        loc, f = _tree(tmp_path, _cfg(disabled_guards=["db_lockdown"]), mod)
        f.chmod(0o666)
        home = _home(
            tmp_path, {"push_default_allowlist": ["evil"], "disabled_guards": ["terraform_family"]}
        )
        with using(mod, loc, home):
            cfg = mod.load_config()
        # Invalid mode = exactly the no-file defaults: nothing switched off, and
        # the allowlist is the built-in one, from neither file.
        assert cfg == mod._DEFAULT_CONFIG

    def test_malformed_json_in_protected_is_fail_closed(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, "{not json", mod)
        home = _home(tmp_path, {"disabled_guards": ["terraform_family"]})
        with using(mod, loc, home):
            cfg = mod.load_config()
        assert cfg == mod._DEFAULT_CONFIG

    def test_bad_shape_in_protected_is_fail_closed(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, json.dumps({"disabled_guards": "db_lockdown"}), mod)
        home = _home(tmp_path, {"disabled_guards": ["terraform_family"]})
        with using(mod, loc, home):
            cfg = mod.load_config()
        assert cfg == mod._DEFAULT_CONFIG

    def test_invalid_protected_logs_warning(self, tmp_path, capsys):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, None, mod)
        with using(mod, loc, _home(tmp_path, {})):
            mod.load_config()
        assert "every guard stays on" in capsys.readouterr().err

    def test_yaadgaar_keys_and_unknown_keys_parse(self, tmp_path):
        mod = _load_hook()
        body = json.dumps(
            {
                "version": 1,
                "push_default_allowlist": ["nix"],
                "disabled_guards": [
                    "terraform_family",
                    "git_commit_flags",
                    "hook_config_tamper",
                    "git_push_default",
                    "db_lockdown",
                ],
                "future_yaadgaar_key": {"nested": [1, 2]},
            }
        )
        loc, _ = _tree(tmp_path, body, mod)
        with using(mod, loc, _home(tmp_path, {})):
            cfg = mod.load_config()
        assert set(cfg["disabled_guards"]) == {
            "terraform_family",
            "git_commit_flags",
            "hook_config_tamper",
            "git_push_default",
            "db_lockdown",
        }


# --------------------------------------------------------------------------- #
# Every guard call site consults the protected loader
# --------------------------------------------------------------------------- #


def _fake_git(repo: str, default: str = "master"):
    def fake(args, cwd):
        if args[:1] == ["symbolic-ref"]:
            return f"refs/remotes/origin/{default}"
        if args[:1] == ["rev-parse"] and "--show-toplevel" in args:
            return f"/home/x/{repo}"
        if args[:1] == ["rev-parse"] and "--abbrev-ref" in args:
            return default
        return None

    return fake


_CASES = [
    ("db_lockdown", lambda: _bash("docker exec yadgar-db sh")),
    ("terraform_family", lambda: _bash("terraform apply")),
    ("git_commit_flags", lambda: _bash("git commit --no-verify -m x")),
    ("hook_config_tamper", None),  # payload needs the home path; built in the test
]


class TestCallSites:
    @pytest.mark.parametrize("gid,payload", [c for c in _CASES if c[1] is not None])
    def test_protected_disable_switches_bash_guard_off(self, tmp_path, gid, payload):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(disabled_guards=[gid]), mod)
        with using(mod, loc, _home(tmp_path, {})):
            assert _run_main(mod, payload()) == "allow"

    @pytest.mark.parametrize("gid,payload", [c for c in _CASES if c[1] is not None])
    def test_home_disable_ignored_in_protected_mode(self, tmp_path, gid, payload):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        with using(mod, loc, _home(tmp_path, {"disabled_guards": [gid]})):
            assert _run_main(mod, payload()) == "deny"

    @pytest.mark.parametrize("gid,payload", [c for c in _CASES if c[1] is not None])
    def test_home_disable_ignored_in_invalid_mode(self, tmp_path, gid, payload):
        mod = _load_hook()
        loc, f = _tree(tmp_path, _cfg(disabled_guards=[gid]), mod)
        f.chmod(0o646)
        with using(mod, loc, _home(tmp_path, {"disabled_guards": [gid]})):
            assert _run_main(mod, payload()) == "deny"

    def test_git_push_default_allowlist_from_protected(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(push_default_allowlist=["myrepo"]), mod)
        with (
            using(mod, loc, _home(tmp_path, {})),
            patch.object(mod, "_git", side_effect=_fake_git("myrepo")),
        ):
            assert _run_main(mod, _bash("git push origin master", "/home/x/myrepo")) == "allow"

    def test_git_push_default_home_allowlist_ignored_in_protected_mode(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        home = _home(tmp_path, {"push_default_allowlist": ["myrepo"]})
        with using(mod, loc, home), patch.object(mod, "_git", side_effect=_fake_git("myrepo")):
            assert _run_main(mod, _bash("git push origin master", "/home/x/myrepo")) == "deny"

    def test_git_push_default_disabled_via_protected(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(disabled_guards=["git_push_default"]), mod)
        with (
            using(mod, loc, _home(tmp_path, {})),
            patch.object(mod, "_git", side_effect=_fake_git("other")),
        ):
            assert _run_main(mod, _bash("git push origin master", "/home/x/other")) == "allow"

    def test_hook_config_tamper_edit_side_consults_protected(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        home = _home(tmp_path, {"disabled_guards": ["hook_config_tamper"]})
        with using(mod, loc, home):
            assert _run_main(mod, _edit(str(home))) == "deny"
        loc2, _ = _tree(tmp_path / "b", _cfg(disabled_guards=["hook_config_tamper"]), mod)
        with using(mod, loc2, home):
            assert _run_main(mod, _edit(str(home))) == "allow"

    def test_hook_config_tamper_bash_side_consults_protected(self, tmp_path):
        mod = _load_hook()
        loc, _ = _tree(tmp_path, _cfg(), mod)
        home = _home(tmp_path, {"disabled_guards": ["hook_config_tamper"]})
        cmd = _bash(f"echo x > {home}")
        with using(mod, loc, home):
            assert _run_main(mod, cmd) == "deny"
        loc2, _ = _tree(tmp_path / "b", _cfg(disabled_guards=["hook_config_tamper"]), mod)
        with using(mod, loc2, home):
            assert _run_main(mod, cmd) == "allow"
