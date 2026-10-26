#!/usr/bin/env python3
"""Yadgar PreToolUse router-guard — one standalone hook, four mechanical guards.

HOOKS train Car 1. Subsumes the single-purpose db-lockdown-check.py and turns
four prose HARD RULES into wrapper-and-global-option-aware guard blocks:

  G1  git commit --no-verify / --no-gpg-sign / -c commit.gpgsign=false  → deny
  G2  terraform/tofu/tfp (+ docker-run terraform image, nix-run terraform,
      digger in gh-pr-comment/gh-api)                                    → deny
  G3  git push to the repo default branch (JSON repo-allowlist)          → deny
  G4  docker exec into yadgar-db / yadgar-backend (subsumed db-lockdown)  → deny
  G5  any write (Bash or Edit/Write/NotebookEdit) to the hook-exceptions
      config itself — human-only, durable decision, no agent self-service → deny

Decision channel: exit-0 + stdout JSON. Schema matches the proven
db-lockdown-check.py (hookSpecificOutput.hookEventName REQUIRED). Deny carries
BOTH permissionDecisionReason (docs-canonical, agent-facing) and top-level
systemMessage (proven-working, human-facing) — extra keys are ignored.

Fail-CLOSED (ledger 1567): this is a security guard, so any router error
denies the call and logs to stderr. It used to fail open, and one fault (a
malformed exceptions file) then switched every guard off. A config file the
router cannot trust leaves every guard on. Two error paths that cannot decide
safely stay allow on purpose: a missing config file (normal on a fresh
install), and a git that ran but reported no origin/HEAD (G3 cannot name the
default branch). A git that could not run at all denies the push.

Exit code: Claude Code treats any exit other than 0 or 2, and a hook timeout,
as a non-blocking error, so the tool runs. The entry point therefore never
lets an exception escape: a fault before a decision prints a deny, and a
fault after one (tracing shutdown) leaves the printed decision in force.

Honest scope: a mechanical speed-bump against accidental / forgotten-prose
violations (leading git globals like `git -C`, transparent wrapper prefixes,
one level of `bash -c`), NOT a hardened sandbox. Shell aliases, command
substitution `$(...)`, and deeply nested subshells are out of reach (Scope-OUT).
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

try:
    from yadgar._shared.observability.observe import observe
    from yadgar._shared.observability.tracing import shutdown_tracing
except Exception:  # noqa: BLE001 — tracing is optional here: ANY import fault (not only ImportError) must degrade to no-op tracing, never crash the guard before it decides

    def observe(*_a, **_k):
        return lambda fn: fn

    def shutdown_tracing(*_a, **_k):
        pass


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

_DEFAULT_CONFIG: dict = {
    "version": 1,
    "push_default_allowlist": ["nix", "ledger", "ostad"],
    "disabled_guards": [],
}

_GIT_SUBPROC_TIMEOUT = 5


def _config_path() -> Path:
    """Global exceptions config, alongside the global hook scripts."""
    return Path(os.path.expanduser("~/.claude/yadgar-hook-exceptions.json"))


_MAX_CONFIG_BYTES = 64 * 1024
_CONFIG_LIST_KEYS = ("push_default_allowlist", "disabled_guards")
# O_NONBLOCK: opening a FIFO must not wait for a writer. O_NOFOLLOW: the
# installer writes a regular file, and a symlink would let an agent edit the
# real target, which G5 does not watch. O_NOCTTY: a terminal device at the
# path must not become the controlling tty.
_CONFIG_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_NONBLOCK
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class ConfigRejectedError(Exception):
    """The exceptions file exists but cannot be trusted; every guard stays on."""


def _log(msg: str) -> None:
    sys.stderr.write(f"[yadgar-pretooluse-router] {msg}\n")


def _read_open_fd(fd: int) -> bytes:
    """At most _MAX_CONFIG_BYTES from the open *fd*, judged by fstat on that fd."""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigRejectedError("it is not a regular file")
        if st.st_size > _MAX_CONFIG_BYTES:
            raise ConfigRejectedError(f"it is larger than {_MAX_CONFIG_BYTES} bytes")
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_CONFIG_BYTES:
            chunk = os.read(fd, _MAX_CONFIG_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_CONFIG_BYTES:
            raise ConfigRejectedError(f"it is larger than {_MAX_CONFIG_BYTES} bytes")
        return b"".join(chunks)
    except OSError as exc:
        raise ConfigRejectedError(f"cannot read it ({exc.strerror or exc})") from exc


def _read_config_bytes(p: Path) -> bytes | None:
    """Read at most _MAX_CONFIG_BYTES from a REGULAR file; None when absent."""
    try:
        fd = os.open(p, _CONFIG_OPEN_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP (a symlink under O_NOFOLLOW), EACCES, EISDIR, ...
        raise ConfigRejectedError(f"cannot open it ({exc.strerror or exc})") from exc
    try:
        return _read_open_fd(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Protected mode (ADR-1028, yaadgaar v0.1.31): one exceptions file for both guards
# --------------------------------------------------------------------------- #
# The home copy is writable by anything running as the user, an agent included.
# When the protected DIRECTORY exists, its root-owned file is verified exactly as
# yaadgaar verifies it and the home copy is ignored. absent (lstat → NotFound
# only) → home copy; valid → protected file; invalid (anything else, a directory
# without the file included) → no exception from either file. Path and uid are
# FIXED: a configurable path is one an agent can redirect. Contract:
# docs/reference/hooks.md § "PreToolUse hook exceptions file".


class ProtectedLocation(NamedTuple):
    """Where the protected exceptions file is, and whom to trust."""

    anchor: str  # the first directory judged ("/" in production)
    dirs: tuple[str, ...]  # directories between the anchor and the file, top down
    file: str
    trusted_uid: int  # the uid that must own every component
    current_uid: int | None  # the uid this guard runs as; None when unknown


# macOS's /etc is a symlink, which the walk refuses, so its real path is used.
_PROTECTED_DIR = "/private/etc/yaadgaar" if sys.platform == "darwin" else "/etc/yaadgaar"
_TRUSTED_UID = 0  # root: the one owner an agent running as the user cannot be
_GROUP_OR_OTHER_WRITE = stat.S_IWGRP | stat.S_IWOTH
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _protected_location() -> ProtectedLocation | None:
    """The production location; None where there is no protected mode (not POSIX)."""
    if os.name != "posix":
        return None
    return ProtectedLocation(
        anchor="/",
        dirs=tuple(_PROTECTED_DIR.strip("/").split("/")),
        file=_HOOK_CONFIG_BASENAME,
        trusted_uid=_TRUSTED_UID,
        current_uid=os.geteuid(),
    )


def _judge(what: str, st: os.stat_result, trusted: int) -> None:
    """One component: owned by *trusted* and not writable by group or others."""
    if st.st_uid != trusted:
        raise ConfigRejectedError(f"{what} is owned by uid {st.st_uid}, not uid {trusted}")
    if st.st_mode & _GROUP_OR_OTHER_WRITE:
        raise ConfigRejectedError(
            f"{what} is writable by its group or by others (mode {st.st_mode & 0o7777:04o})"
        )


def _open_judged(name: str, flags: int, dir_fd: int | None, what: str, trusted: int) -> int:
    """Open *name* (relative to *dir_fd*) refusing symlinks; judge the fd; return it."""
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:  # ELOOP / EMLINK (symlink), ENOTDIR, EACCES, ENOENT, ...
        raise ConfigRejectedError(
            f"cannot open {what} ({exc.strerror or exc}); a symlink is refused"
        ) from exc
    try:
        _judge(what, os.fstat(fd), trusted)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk_protected(location: ProtectedLocation) -> bytes:
    """Judge each component from the anchor down (dir_fd chained), then read the file."""
    trusted = location.trusted_uid
    if location.current_uid is None:
        raise ConfigRejectedError("the guard cannot tell which user it runs as")
    if location.current_uid == trusted:
        raise ConfigRejectedError(
            f"the guard runs as uid {trusted}, which owns the protected tree and can write it"
        )
    what = location.anchor
    fd = _open_judged(location.anchor, _DIR_OPEN_FLAGS, None, what, trusted)
    try:
        for name in location.dirs:
            what = os.path.join(what, name)
            child = _open_judged(name, _DIR_OPEN_FLAGS, fd, what, trusted)
            os.close(fd)
            fd = child
        what = os.path.join(what, location.file)
        ffd = _open_judged(location.file, _CONFIG_OPEN_FLAGS, fd, what, trusted)
    finally:
        os.close(fd)
    try:
        return _read_open_fd(ffd)
    finally:
        os.close(ffd)


class Verified(NamedTuple):
    state: str  # "absent" | "invalid" | "valid"
    text: bytes = b""  # the file's bytes when valid
    why: str = ""  # the reason when invalid


def verify_protected(location: ProtectedLocation) -> Verified:
    """Absent, invalid (with why) or valid (with the file's bytes) for *location*."""
    pdir = os.path.join(location.anchor, *location.dirs)
    try:
        os.lstat(pdir)  # NOT followed: a symlink there, even dangling, is present
    except FileNotFoundError:
        return Verified("absent")
    except OSError as exc:
        return Verified("invalid", why=f"cannot inspect {pdir} ({exc.strerror or exc})")
    try:
        return Verified("valid", text=_walk_protected(location))
    except ConfigRejectedError as exc:
        return Verified("invalid", why=str(exc))


def _parse_config(data: bytes) -> dict:
    """Validate the two fields the router uses; any defect rejects the whole file."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError ⊂ ValueError
        raise ConfigRejectedError(f"it is not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(raw, dict):
        raise ConfigRejectedError("its top level is not a JSON object")
    parsed: dict = {}
    for key in _CONFIG_LIST_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigRejectedError(f'"{key}" is not a list of strings')
        parsed[key] = list(value)
    return parsed


def load_config(path: Path | None = None) -> dict:
    """Load the exceptions config — the ONE reader every guard goes through.

    Protected mode first (ADR-1028); the home copy (*path*) only when absent.

    Fail-closed (ledger 1567): a file that is not a regular file, is larger
    than 64 KiB, is not valid JSON, or holds a field of the wrong shape is
    ignored as a WHOLE — no field of it is honoured — so every guard stays on.
    """
    cfg = {key: list(val) if isinstance(val, list) else val for key, val in _DEFAULT_CONFIG.items()}
    location = _protected_location()
    if location is not None:
        verified = verify_protected(location)
        if verified.state == "invalid":
            _log(f"protected exceptions file not trusted ({verified.why}); every guard stays on")
            return cfg
        if verified.state == "valid":
            try:
                cfg.update(_parse_config(verified.text))
            except ConfigRejectedError as exc:
                _log(f"ignoring the protected exceptions file: {exc}; every guard stays on")
            return cfg
    p = path or _config_path()
    try:
        data = _read_config_bytes(p)
        if data is None:
            return cfg
        cfg.update(_parse_config(data))
    except ConfigRejectedError as exc:
        _log(f"ignoring {p}: {exc}; every guard stays on")
    return cfg


# --------------------------------------------------------------------------- #
# Decision payloads (schema mirrors db-lockdown-check.py)
# --------------------------------------------------------------------------- #


def _allow() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _deny(agent_reason: str, human_reason: str | None = None) -> dict:
    human = human_reason if human_reason is not None else agent_reason
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": agent_reason,
        },
        "systemMessage": human,
    }


# Sentinel for guard results.
DENY = object()
ALLOW = object()


# --------------------------------------------------------------------------- #
# Stage 1-2: tokenize + segment
# --------------------------------------------------------------------------- #

_SEPARATORS = {"&&", "||", ";", "|", "&"}


def tokenize(cmd: str) -> list[str]:
    """shlex.split with a whitespace fallback on ValueError (fail-soft)."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _strip_subshell_parens(seg: list[str]) -> list[str]:
    """Strip a leading '(' / 'stuff(' and trailing ')' subshell wrapper."""
    s = list(seg)
    if s and s[0] == "(":
        s = s[1:]
    if s and s[0].startswith("(") and len(s[0]) > 1:
        s[0] = s[0][1:]
    if s and s[-1] == ")":
        s = s[:-1]
    if s and s[-1].endswith(")") and len(s[-1]) > 1:
        s[-1] = s[-1][:-1]
    return s


def segment(tokens: list[str]) -> list[list[str]]:
    """Split a token list on shell separators; strip subshell parens per segment."""
    segs: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)

    cleaned = [stripped for seg in segs if (stripped := _strip_subshell_parens(seg))]
    return cleaned


# --------------------------------------------------------------------------- #
# Stage 3: wrapper-peel
# --------------------------------------------------------------------------- #

_SUDO_ARG_FLAGS = {"-u", "-g", "-C", "-p", "-U", "-h", "-r", "-t"}
_SHELLS = {"bash", "sh", "zsh"}
_MAX_RECURSE = 3


def _basename(tok: str) -> str:
    return os.path.basename(tok)


def _skip_sudo(argv: list[str]) -> int:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return i + 1
        if tok in _SUDO_ARG_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            break
    return i


def _skip_env(argv: list[str]) -> int:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "-u":
            i += 2
        elif tok == "-i" or ("=" in tok and not tok.startswith("-")):
            i += 1
        else:
            break
    return i


def _skip_nice(argv: list[str]) -> int:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "-n":
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            break
    return i


def _skip_timeout(argv: list[str]) -> int:
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in ("-s", "-k"):
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            # first non-flag = DURATION positional; consume it, rest is command
            return i + 1
    return i


def _skip_flags_only(argv: list[str]) -> int:
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 1
    return i


# wrapper program basename → function returning index of the real command start
_WRAPPER_SKIP = {
    "sudo": _skip_sudo,
    "env": _skip_env,
    "nice": _skip_nice,
    "timeout": _skip_timeout,
    "nohup": _skip_flags_only,
    "time": _skip_flags_only,
    "stdbuf": _skip_flags_only,
    "ionice": _skip_flags_only,
    "xargs": _skip_flags_only,
}


def peel_wrappers(argv: list[str], _depth: int = 0) -> list[str]:
    """Strip leading transparent wrappers to reach the real command.

    Consumes each wrapper's own arguments so we never land on a wrapper arg.
    `bash -c "<STR>"` recurses (bounded) into the wrapped string. Best-effort:
    an unrecognized wrapper shape leaves argv as-is (→ benign non-match).
    """
    if not argv:
        return argv
    prog = _basename(argv[0])

    skip = _WRAPPER_SKIP.get(prog)
    if skip is not None:
        return peel_wrappers(argv[skip(argv) :], _depth)

    if prog in _SHELLS:
        # bash -c "<STR>" → recurse into the wrapped string (bounded).
        if _depth < _MAX_RECURSE and "-c" in argv[1:]:
            ci = argv.index("-c")
            if ci + 1 < len(argv):
                inner_tokens = tokenize(argv[ci + 1])
                if inner_tokens:
                    return peel_wrappers(inner_tokens, _depth + 1)
        return argv

    return argv


# --------------------------------------------------------------------------- #
# Stage 4: git global-option peel
# --------------------------------------------------------------------------- #

# space-form arg-consuming git global options (skip flag + next token)
_GIT_GLOBAL_ARG = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--super-prefix",
}


def peel_git_globals(argv: list[str]) -> tuple[list[str], list[str]]:
    """When argv[0] basename is git, walk past global options to the subcommand.

    Returns (resolved_argv_from_subcommand, pre_subcommand_c_pairs). If no
    subcommand is found (e.g. `git --version`), the resolved argv is empty.
    """
    if not argv or _basename(argv[0]) != "git":
        return argv, []
    pre_c: list[str] = []
    i = 1
    n = len(argv)
    while i < n:
        tok = argv[i]
        if not tok.startswith("-"):
            # first non-option token = subcommand
            return argv[i:], pre_c
        # =-joined form: single token like --git-dir=… or -c… (rare)
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in _GIT_GLOBAL_ARG:
            # space form: consume flag + its argument
            if tok == "-c" and i + 1 < n:
                pre_c.append(argv[i + 1])
            i += 2
            continue
        # standalone global (paginate/bare/etc.) or unknown → skip one token
        i += 1
    return [], pre_c


# --------------------------------------------------------------------------- #
# Guards — each takes (segment_argv, ctx) and returns DENY or ALLOW
# --------------------------------------------------------------------------- #

_NO_VERIFY_PREFIX = "--no-veri"  # git accepts unambiguous long-option prefixes
_GPGSIGN_FALSE = {"false", "0", "no", "off"}


def guard_git_commit_flags(argv: list[str], ctx: dict) -> object:
    """G1 — deny git commit with hook-bypass flags."""
    peeled = peel_wrappers(argv)
    if not peeled or _basename(peeled[0]) != "git":
        return ALLOW
    resolved, pre_c = peel_git_globals(peeled)
    if not resolved or resolved[0] != "commit":
        return ALLOW
    post = resolved[1:]

    # -c commit.gpgsign=<false|0|no|off> among PRE-subcommand globals only.
    for pair in pre_c:
        if pair.startswith("commit.gpgsign="):
            val = pair.split("=", 1)[1].strip().lower()
            if val in _GPGSIGN_FALSE:
                return DENY

    for tok in post:
        if tok == "--no-gpg-sign":
            return DENY
        if tok.startswith(_NO_VERIFY_PREFIX):  # --no-verify + unique prefixes
            return DENY
        # short flags: -n (== --no-verify for git commit), incl. bundled -nm
        if tok.startswith("-") and not tok.startswith("--") and len(tok) >= 2:
            if "n" in tok[1:]:
                return DENY
    return ALLOW


_TF_PROGRAMS = {"terraform", "tofu", "tfp"}
_DIGGER_SUBS = {"apply", "plan", "unlock", "lock", "destroy"}


def _is_terraform_container(prog: str, peeled: list[str]) -> bool:
    """docker/podman run <image containing terraform>."""
    if prog not in ("docker", "podman") or "run" not in peeled[1:]:
        return False
    return any("terraform" in tok for tok in peeled[2:])


def _is_nix_terraform(prog: str, peeled: list[str]) -> bool:
    """nix run …#terraform / nix shell -p terraform."""
    if prog != "nix":
        return False
    return any(("terraform" in tok or "opentofu" in tok) for tok in peeled[1:])


def _is_gh_digger(prog: str, peeled: list[str]) -> bool:
    """gh pr comment … / gh api … whose body carries a digger command."""
    if prog != "gh":
        return False
    rest = peeled[1:]
    is_pr_comment = len(rest) >= 2 and rest[0] == "pr" and rest[1] == "comment"
    is_api = bool(rest) and rest[0] == "api"
    if not (is_pr_comment or is_api):
        return False
    for tok in rest:
        sub = tok.split()
        if "digger" in sub and any(s in _DIGGER_SUBS for s in sub):
            return True
    return False


def guard_terraform_family(argv: list[str], ctx: dict) -> object:
    """G2 — deny terraform/tofu/tfp + container/nix spawns + digger-on-PR."""
    peeled = peel_wrappers(argv)
    if not peeled:
        return ALLOW
    prog = _basename(peeled[0])
    if (
        prog in _TF_PROGRAMS
        or _is_terraform_container(prog, peeled)
        or _is_nix_terraform(prog, peeled)
        or _is_gh_digger(prog, peeled)
    ):
        return DENY
    return ALLOW


def _repo_allowlisted(cwd: str | None, ctx: dict) -> bool:
    """True when the push target repo (toplevel basename) is on the allowlist."""
    toplevel = _git(["rev-parse", "--show-toplevel"], cwd)
    if not toplevel:
        return False
    repo = os.path.basename(toplevel.strip())
    return repo in ctx.get("config", {}).get("push_default_allowlist", [])


def _resolve_default_branch(cwd: str | None) -> str | None:
    """origin/HEAD short name, or None when unresolvable (fresh clone / CI)."""
    symref = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd)
    if not symref:
        return None
    default = symref.strip().rsplit("/", 1)[-1]
    return default or None


def _push_targets_default(positionals: list[str], default: str, cwd: str | None) -> bool:
    """True when the push refspec/current-branch writes the default branch."""
    for tok in positionals:
        if ":" in tok:
            if tok.split(":", 1)[1].rsplit("/", 1)[-1] == default:
                return True
        elif tok == default:
            return True

    # bare `git push` / `git push origin` (no explicit branch) → check HEAD.
    has_explicit_branch = any((":" in t) or (t == default) for t in positionals)
    non_remote = [t for t in positionals if t != "origin"]
    looks_non_default = any(t and t != default and ":" not in t for t in non_remote)
    if not has_explicit_branch and not looks_non_default:
        cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        return bool(cur and cur.strip() == default)
    return False


def guard_git_push_default(argv: list[str], ctx: dict) -> object:
    """G3 — deny git push to the repo default branch (allowlist carve-out)."""
    peeled = peel_wrappers(argv)
    if not peeled or _basename(peeled[0]) != "git":
        return ALLOW
    resolved, _ = peel_git_globals(peeled)
    if not resolved or resolved[0] != "push":
        return ALLOW

    cwd = ctx.get("cwd") or None
    if _repo_allowlisted(cwd, ctx):
        return ALLOW
    default = _resolve_default_branch(cwd)
    if not default:
        # git ran and reported no origin/HEAD (fresh clone, CI): there is no
        # default branch to protect. A git that could not run raised instead.
        return ALLOW

    positionals = [t for t in resolved[1:] if not t.startswith("-")]
    return DENY if _push_targets_default(positionals, default, cwd) else ALLOW


_DB_LOCKDOWN_PATTERNS = (
    "docker exec yadgar-backend",
    "docker exec yadgar-db",
    "podman exec yadgar-backend",
    "podman exec yadgar-db",
)


def guard_db_lockdown(cmd: str) -> object:
    """G4 — subsumed db-lockdown; verbatim substring match on the raw command."""
    for pattern in _DB_LOCKDOWN_PATTERNS:
        if pattern in cmd:
            return DENY
    return ALLOW


_HOOK_CONFIG_BASENAME = "yadgar-hook-exceptions.json"


def _is_protected_config_path(path_str: str | None) -> bool:
    """True when a path argument resolves to the hook-exceptions config."""
    if not path_str or _HOOK_CONFIG_BASENAME not in path_str:
        return False
    try:
        target = os.path.abspath(os.path.expanduser(path_str))
    except Exception:  # noqa: BLE001 — a path that names the config file but cannot be resolved counts as a match: G5 fails closed (ledger 1567)
        return True
    return target == os.path.abspath(str(_config_path()))


def _redirect_targets_config(seg: list[str]) -> bool:
    """True when a `> path` / `>> path` token pair in *seg* hits the config."""
    for i, tok in enumerate(seg):
        if tok in (">", ">>") and i + 1 < len(seg) and _is_protected_config_path(seg[i + 1]):
            return True
    return False


def _sed_targets_config(rest: list[str]) -> bool:
    """True for `sed -i ... <config>` (in-place edit whose arg is the config)."""
    if not any(t == "-i" or t.startswith("-i") for t in rest):
        return False
    return any(_is_protected_config_path(t) for t in rest)


def _python_c_targets_config(seg: list[str], rest: list[str]) -> bool:
    """True for `python -c '...open(<config>...)...'` (best-effort, honest-scope)."""
    if "-c" not in rest:
        return False
    ci = seg.index("-c")
    if ci + 1 >= len(seg):
        return False
    code = seg[ci + 1]
    return "open(" in code and _HOOK_CONFIG_BASENAME in code


def _segment_targets_protected_config(seg: list[str]) -> bool:
    """True when this (wrapper-peeled) segment writes to the hook config.

    Per-segment, per-program checks — NOT a whole-command substring scan.
    A whole-command scan false-positives on any command that merely mentions
    the filename in prose (e.g. a commit message describing this very guard)
    while also writing some unrelated file elsewhere in the same compound
    command; tokenizing and checking the actual write target avoids that.
    """
    if not seg:
        return False
    prog = _basename(seg[0])
    rest = seg[1:]

    if _redirect_targets_config(seg):
        return True
    if prog == "sed":
        return _sed_targets_config(rest)
    if prog in ("tee", "truncate"):
        return any(_is_protected_config_path(t) for t in rest)
    if prog in ("cp", "mv"):
        return bool(rest) and _is_protected_config_path(rest[-1])
    if prog in ("python", "python3"):
        return _python_c_targets_config(seg, rest)
    return False


def guard_hook_config_tamper(cmd: str) -> object:
    """G5 — deny raw-shell writes targeting the hook-exceptions config itself.

    2026-07-28 incident: a subagent used Edit (not Bash) to add its own repo
    to push_default_allowlist, pushed to master, then reverted the file to
    conceal the change — bypassing G3 with no guard in its way, since G3 only
    inspects `git push`, not writes to the allowlist that feeds it. This is
    the Bash-side half of the fix (raw shell writes: redirect, sed -i, tee,
    cp, mv, truncate, a python one-liner writing the file); the
    Edit/Write/NotebookEdit-side half is in main() below. Heuristic,
    honest-scope like G4 — not a hardened sandbox.
    """
    if _HOOK_CONFIG_BASENAME not in cmd:
        return ALLOW
    for seg in segment(tokenize(cmd)):
        if _segment_targets_protected_config(peel_wrappers(seg)):
            return DENY
    return ALLOW


# id → guard function NAME (resolved from module globals at call time so that
# test-time patch.object(mod, "guard_*") is honored and a raising guard
# fails closed via main()'s except).
_GUARD_IDS = {
    "git_commit_flags": "guard_git_commit_flags",
    "terraform_family": "guard_terraform_family",
    "git_push_default": "guard_git_push_default",
}

_DENY_REASONS = {
    "git_commit_flags": (
        "Blocked: git commit hook-bypass flag (--no-verify / --no-gpg-sign / "
        "-c commit.gpgsign=false). Hook failure is a signal, not an obstacle — "
        "fix the root cause and retry without bypassing."
    ),
    "terraform_family": (
        "Blocked: terraform-family invocation (terraform/tofu/tfp, docker-run "
        "terraform, nix-run terraform, or digger-on-PR). No terraform execution "
        "by any mechanism. Hand the command to the user via MIGRATION_NOTES.md."
    ),
    "git_push_default": (
        "Blocked: git push to the repository default branch. Branch off the "
        "default first. This repo is not on push_default_allowlist. Do NOT "
        "edit ~/.claude/yadgar-hook-exceptions.json yourself, even "
        "temporarily — that file is a durable, human-only decision. If a "
        "genuine exception is needed, STOP and ask the user to add it."
    ),
    "db_lockdown": (
        "Direct docker exec into yadgar DB/backend containers is blocked to "
        "prevent data corruption. Use yadgar MCP tools instead."
    ),
    "hook_config_tamper": (
        "Blocked: agents must never write to "
        "~/.claude/yadgar-hook-exceptions.json — not directly, not via a "
        "wrapper, not temporarily-then-reverted. It grants push-to-default "
        "bypass and is a human-only, durable decision. STOP and ask the "
        "user to edit it themselves."
    ),
}


# --------------------------------------------------------------------------- #
# git subprocess helper (G3 only)
# --------------------------------------------------------------------------- #


class GitUnavailableError(Exception):
    """git could not run at all; G3 cannot decide, so the push is denied."""


def _git(args: list[str], cwd: str | None) -> str | None:
    """Run `git <args>` in cwd; stdout, or None when git ran and exited non-zero.

    A git that could not RUN (missing binary, bad cwd, timeout) raises
    GitUnavailableError, which main() turns into a deny (ledger 1567). It used
    to return None, which G3 read as "no default branch" and allowed the push.
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_SUBPROC_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitUnavailableError(f"git {' '.join(args)} could not run: {exc}") from exc
    if r.returncode != 0:
        return None
    return r.stdout


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _decide_edit_family(data: dict) -> dict:
    """G5 (Edit/Write/NotebookEdit side): decide allow/deny for a file-edit tool call.

    Agents must never edit the hook-exceptions config that feeds G3's
    allowlist — human-only, durable.
    """
    ti = data.get("tool_input", {})
    fp = None
    if isinstance(ti, dict):
        fp = ti.get("file_path") or ti.get("notebook_path")
    config = load_config()
    disabled = set(config.get("disabled_guards", []))
    if "hook_config_tamper" not in disabled and _is_protected_config_path(fp):
        return _deny(_DENY_REASONS["hook_config_tamper"])
    return _allow()


def _decide_bash(data: dict) -> dict:
    """G1-G5 for a Bash tool call."""
    tool_input = data.get("tool_input", {})
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not cmd:
        return _allow()

    config = load_config()
    disabled = set(config.get("disabled_guards", []))
    ctx = {"cwd": data.get("cwd"), "config": config}

    # G4 (db-lockdown): verbatim substring on the raw command.
    if "db_lockdown" not in disabled and guard_db_lockdown(cmd) is DENY:
        return _deny(_DENY_REASONS["db_lockdown"])

    # G5 (Bash side): raw-shell writes targeting the hook-exceptions config.
    if "hook_config_tamper" not in disabled and guard_hook_config_tamper(cmd) is DENY:
        return _deny(_DENY_REASONS["hook_config_tamper"])

    # G1-G3: run each segment of the compound command through the pipeline.
    for seg in segment(tokenize(cmd)):
        for gid, gname in _GUARD_IDS.items():
            if gid in disabled:
                continue
            guard = globals()[gname]
            if guard(seg, ctx) is DENY:
                return _deny(_DENY_REASONS[gid])
    return _allow()


def _decide(data: dict) -> dict:
    tool_name = data.get("tool_name")
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return _decide_edit_family(data)
    if tool_name != "Bash":
        return _allow()
    return _decide_bash(data)


def _error_deny(what: str) -> dict:
    reason = (
        f"Blocked: the yadgar PreToolUse guard failed ({what}) and fails closed. "
        "This is a guard fault, not a verdict on the command. Report it to the "
        "user; do not work around the guard."
    )
    return _deny(reason)


# True once a decision has reached stdout. The entry point reads it so that a
# fault AFTER the decision (tracing shutdown) never replaces that decision.
_DECISION_EMITTED = False


def _emit(decision: dict) -> None:
    global _DECISION_EMITTED
    print(json.dumps(decision))
    _DECISION_EMITTED = True


def _decide_from_stdin() -> dict:
    """Read the hook payload and decide. Any fault → deny (ledger 1567)."""
    try:
        data = json.load(sys.stdin)
    except ValueError as exc:  # JSONDecodeError ⊂ ValueError
        _log(f"malformed hook input, failing closed: {exc}")
        return _error_deny("malformed hook input")
    try:
        return _decide(data)
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED top level of a security guard: any fault in any guard denies the call, with a stderr note; it used to allow, which switched every guard off (ledger 1567)
        _log(f"error, failing closed: {type(exc).__name__}: {exc}")
        return _error_deny(f"{type(exc).__name__}: {exc}")


@observe(tier="boundary")
def main() -> None:
    try:
        _emit(_decide_from_stdin())
    finally:
        try:
            shutdown_tracing()
        except Exception as exc:  # noqa: BLE001 — tracing shutdown runs after the decision is printed; its fault must not turn into a non-zero exit, which Claude Code treats as allow
            _log(f"tracing shutdown failed, decision stands: {exc}")


def _entry() -> int:
    """Process entry point: never let an exception escape (exit 1 = allow)."""
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — last resort: an escaping exception would exit 1, which Claude Code treats as a non-blocking error and runs the tool; deny instead
        if _DECISION_EMITTED:
            _log(f"error after the decision was printed, decision stands: {exc}")
            return 0
        # Most likely the tracing wrapper failed before main() read stdin.
        # Decide once more without tracing, so a tracing fault alone does not
        # block every call. If stdin was already consumed, this denies.
        _log(f"error before a decision, retrying untraced: {type(exc).__name__}: {exc}")
    if _DECISION_EMITTED:
        return 0
    try:
        _emit(_decide_from_stdin())
        return 0
    except Exception as exc:  # noqa: BLE001 — the untraced retry failed too (e.g. stdout gone); exit 2 is the one channel Claude Code always treats as a block
        sys.stderr.write(f"Blocked: the yadgar PreToolUse guard failed and fails closed: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(_entry())
