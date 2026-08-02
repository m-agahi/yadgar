# Plan (RETROSPECTIVE): detect pipx installs on the modern `share/pipx` venv layout

> **RETROSPECTIVE — written after the fact.** This car SHIPPED before its plan file existed.
> Commit `b6c66ffb`, merged into the v5.171 train as `46de2442`, core 5.170.15. This document
> records what shipped and why, so every car in the train carries a plan. **It proposes no
> changes.** The follow-on work it identified is planned separately as
> [#0112](0112-detect-install-method-sh-pipx.md).

**Date:** 2026-08-01 (retro)
**Task:** #0109
**ADR:** none
**Status:** SHIPPED

---

## 1. Problem

`yadgar update --install` was unreachable on any stock modern pipx install.

`detect_install_method()` matched pipx by a hardcoded substring of the *legacy* pipx layout:

```python
if "/.local/pipx/venvs/yadgar/" in real:
    return "pipx"
```

pipx ≥1.6 changed its default `PIPX_HOME` from `~/.local/pipx` to the XDG data dir,
`~/.local/share/pipx`. That inserts a `share` segment, so a modern install resolves to
`~/.local/share/pipx/venvs/yadgar/bin/yadgar`, which the literal above cannot match.

Fall-through consequence chain:

1. no pipx match → no brew/nix match → not a docker shim → no `.git` ancestor
2. `detect_install_method()` returns `"unknown"`
3. `can_self_install("unknown")` is `False`
4. `yadgar update --install` refuses

Reproduced live on a fresh Debian 13 VM at core 5.170.14 — the fresh-VM QA practice ADR-0174
adopted is what surfaced it. It is not visible in CI, which runs inside pre-baked containers with
pre-existing state and never performs a from-scratch pipx install.

---

## 2. What shipped

`yadgar/core/update/install_methods.py:61-73`:

```python
pipx_home = os.environ.get("PIPX_HOME")
if pipx_home:
    pipx_venv_prefix = os.path.join(os.path.realpath(pipx_home), "venvs", "yadgar") + os.sep
    if real.startswith(pipx_venv_prefix):
        return "pipx"
if "/pipx/venvs/yadgar/" in real:
    return "pipx"
```

Two rules, in order:

1. **explicit `PIPX_HOME` wins** — resolved through `realpath`, matched as a path *prefix* with a
   trailing separator. Covers custom installs, which neither default prefix would.
2. **prefix-agnostic segment fallback** — match `pipx/venvs/yadgar/` regardless of what precedes
   it. Covers both known defaults without hardcoding either.

The docstring (`install_methods.py:8-12`) was updated to state the new contract and to flag that
`scripts/install/detect_install_method.sh` still carried the legacy-only match.

### 2.1 Why prefix-agnostic rather than "add the second prefix"

Adding `*/.local/share/pipx/venvs/yadgar/*` alongside the legacy glob would have fixed the
observed case and broken again on the next `PIPX_HOME` default change. Matching the
`pipx/venvs/yadgar/` **segment** is invariant under the prefix, which is the part pipx actually
owns and changes.

### 2.2 Why `PIPX_HOME` is checked first, with a stricter match

The env var is an explicit user statement about where pipx lives, so it outranks inference. It
gets the stricter treatment — `realpath` + `startswith` on a separator-terminated prefix — because
it is anchored to a known root, where a loose `in` test would be needlessly permissive.

---

## 3. Tests that shipped

`yadgar/tests/scripts/test_update_install_methods.py` — the directory CI gates at
`.forgejo/workflows/ci-pr.yaml:79`.

| test | what it pins |
|---|---|
| `test_detects_pipx_legacy_layout` (`:27`) | the legacy `~/.local/pipx` layout still resolves — the regression guard on the old behaviour |
| `test_detects_pipx_modern_share_layout` (`:43`) | the `~/.local/share/pipx` layout — the actual bug; RED before the fix |
| `test_no_false_positive_on_path_merely_containing_pipx_substring` (`:193`) | `/opt/mypipxtool/bin/yadgar` is **not** pipx — the guard that keeps the loosened match from over-matching |

Each synthesizes a real binary under `tmp_path` and patches resolution to it, rather than
depending on the developer's actual install.

The false-positive guard is the load-bearing one. The fix *widened* a match; without a test
pinning the boundary, the natural next regression is a path that merely contains `pipx`
resolving as a pipx install.

---

## 4. What was deliberately left out

`scripts/install/detect_install_method.sh:34-38` — the non-Python mirror for Makefile/CI callers —
carries the identical legacy-only glob and was **not** fixed. The commit message says so
explicitly and scopes it out.

That is now **task #0112**, planned in
[`0112-detect-install-method-sh-pipx.md`](0112-detect-install-method-sh-pipx.md), which also adds
the parity test that would have caught the two mirrors drifting in the first place.

---

## 5. Retrospective notes

* **The bug class is "a hardcoded upstream default".** The literal `/.local/pipx/` encoded a
  choice belonging to pipx, not to yadgar. The fix is not "update the constant", it is "stop
  encoding someone else's default" — which is why the shipped form matches a segment and honors
  the env var.
* **Two mirrors of one helper drifted silently and stayed drifted through a merge**, caught only
  because the Python side was being edited at the time. Nothing compares them. #0112's parity test
  is the structural answer.
* **Fresh-VM QA found it; CI could not.** Third data point for ADR-0174's premise. CI's pre-baked
  containers cannot exercise the from-scratch install path where this class of defect lives.

## 6. Rollback (historical)

Would have been a single-commit revert — one function body and its docstring, no state, no
migration. Not exercised.
