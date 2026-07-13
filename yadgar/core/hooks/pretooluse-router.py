#!/usr/bin/env python3
"""Yadgar PreToolUse router-guard — one standalone hook, four mechanical guards.

HOOKS train Car 1. Subsumes the single-purpose db-lockdown-check.py and turns
four prose HARD RULES into wrapper-and-global-option-aware guard blocks:

  G1  git commit --no-verify / --no-gpg-sign / -c commit.gpgsign=false  → deny
  G2  terraform/tofu/tfp (+ docker-run terraform image, nix-run terraform,
      digger in gh-pr-comment/gh-api)                                    → deny
  G3  git push to the repo default branch (JSON repo-allowlist)          → deny
  G4  docker exec into yadgar-db / yadgar-backend (subsumed db-lockdown)  → deny

Decision channel: exit-0 + stdout JSON. Schema matches the proven
db-lockdown-check.py (hookSpecificOutput.hookEventName REQUIRED). Deny carries
BOTH permissionDecisionReason (docs-canonical, agent-facing) and top-level
systemMessage (proven-working, human-facing) — extra keys are ignored.

Fail-OPEN: any router error → allow + stderr log. A router bug must never brick
every Bash call; only a positively-matched dangerous pattern denies.

Honest scope: a mechanical speed-bump against accidental / forgotten-prose
violations (leading git globals like `git -C`, transparent wrapper prefixes,
one level of `bash -c`), NOT a hardened sandbox. Shell aliases, command
substitution `$(...)`, and deeply nested subshells are out of reach (Scope-OUT).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

try:
    from yadgar._shared.observability.observe import observe
    from yadgar._shared.observability.tracing import shutdown_tracing
except ImportError:

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


def load_config(path: Path | None = None) -> dict:
    """Load exceptions config; absent/corrupt → built-in defaults (fail-open)."""
    p = path or _config_path()
    cfg = dict(_DEFAULT_CONFIG)
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict):
            if isinstance(raw.get("push_default_allowlist"), list):
                cfg["push_default_allowlist"] = raw["push_default_allowlist"]
            if isinstance(raw.get("disabled_guards"), list):
                cfg["disabled_guards"] = raw["disabled_guards"]
    except Exception:  # noqa: BLE001 — any read/parse failure → fail-open to defaults
        pass
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
        return ALLOW  # fail-open when default unresolvable

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


# id → guard function NAME (resolved from module globals at call time so that
# test-time patch.object(mod, "guard_*") is honored and a raising guard
# fail-opens via main()'s except).
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
        "default first. To allow this repo, add its name to "
        "~/.claude/yadgar-hook-exceptions.json → push_default_allowlist."
    ),
    "db_lockdown": (
        "Direct docker exec into yadgar DB/backend containers is blocked to "
        "prevent data corruption. Use yadgar MCP tools instead."
    ),
}


# --------------------------------------------------------------------------- #
# git subprocess helper (G3 only)
# --------------------------------------------------------------------------- #


def _git(args: list[str], cwd: str | None) -> str | None:
    """Run `git <args>` in cwd; return stdout str or None on any failure."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_SUBPROC_TIMEOUT,
        )
        if r.returncode != 0:
            return None
        return r.stdout
    except Exception:  # noqa: BLE001 — subprocess/OS failure → None (G3 fail-open)
        return None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


@observe(tier="boundary")
def main() -> None:
    try:
        try:
            data = json.load(sys.stdin)
        except ValueError:  # JSONDecodeError ⊂ ValueError — malformed stdin → allow
            print(json.dumps(_allow()))
            return

        if data.get("tool_name") != "Bash":
            print(json.dumps(_allow()))
            return

        tool_input = data.get("tool_input", {})
        cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if not cmd:
            print(json.dumps(_allow()))
            return

        config = load_config()
        disabled = set(config.get("disabled_guards", []))
        ctx = {"cwd": data.get("cwd"), "config": config}

        # G4 (db-lockdown): verbatim substring on the raw command.
        if "db_lockdown" not in disabled and guard_db_lockdown(cmd) is DENY:
            print(json.dumps(_deny(_DENY_REASONS["db_lockdown"])))
            return

        # G1-G3: run each segment of the compound command through the pipeline.
        segments = segment(tokenize(cmd))
        for seg in segments:
            for gid, gname in _GUARD_IDS.items():
                if gid in disabled:
                    continue
                guard = globals()[gname]
                if guard(seg, ctx) is DENY:
                    print(json.dumps(_deny(_DENY_REASONS[gid])))
                    return

        print(json.dumps(_allow()))
    except Exception as exc:  # fail-OPEN: never brick Bash on a router bug
        sys.stderr.write(f"[yadgar-pretooluse-router] error, failing open: {exc}\n")
        print(json.dumps(_allow()))
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
