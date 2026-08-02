# Plan: mirror Car 0109's pipx fix into `detect_install_method.sh`

**Date:** 2026-08-01
**Task:** #0112
**ADR:** none required — see §6
**Mirrors:** #0109 (shipped, `b6c66ffb`)
**Status:** design proposed, not started. Small car.

---

## 1. Problem

`scripts/install/detect_install_method.sh:34-38` still matches only the legacy pipx layout:

```bash
# 4. pipx: resolves into .local/pipx/venvs/yadgar/
if [[ "$REAL_PATH" == */.local/pipx/venvs/yadgar/* ]]; then
    echo "pipx"
    exit 0
fi
```

pipx ≥1.6 moved its default `PIPX_HOME` to the XDG data dir, `~/.local/share/pipx`, which inserts
a `share` segment the glob above cannot match. On a stock modern pipx install the script falls
through to the `.git`-ancestor walk (`:47-54`) and then prints `unknown` (`:56`).

This is the **same bug** Car 0109 just fixed on the Python side — verbatim, in the other language.
`yadgar/core/update/install_methods.py:3-5` even says so in its own module docstring:

> Detection order (detect_install_method.sh mirrors this, but as of this writing still only
> matches the legacy `*/.local/pipx/venvs/yadgar/*` layout — see Car 0109)

0109's commit message flagged it and scoped it out. This car closes it.

### 1.1 Who consumes the shell detector

`detect_install_method.sh:2` states its purpose — *"Detect yadgar install method for non-Python
callers (Makefile, CI)"*. Enumerate the real callers before editing (`grep -rn
detect_install_method` across `Makefile`, `scripts/`, `.forgejo/`); the blast radius of a wrong
answer is whatever branches on it. If a caller treats `unknown` as "cannot self-install", this is
the same functional loss 0109 fixed: `yadgar update --install` unreachable on a normal install.

---

## 2. Decision

**Port 0109's exact logic shape, not a re-derivation.** Two matching rules, same order:

1. If `PIPX_HOME` is set, resolve it and test whether `REAL_PATH` starts with
   `<realpath PIPX_HOME>/venvs/yadgar/`.
2. Otherwise (and as a fallback), match the `*/pipx/venvs/yadgar/*` segment prefix-agnostically.

Mirrors `install_methods.py:67-73` exactly. Same order, same fallback, same false-positive
posture.

The false-positive guard is inherent to requiring the full `pipx/venvs/yadgar/` segment: a path
like `/opt/mypipxtool/bin/yadgar` contains `pipx` but not the segment, so it does not match. That
is precisely what `test_no_false_positive_on_path_merely_containing_pipx_substring`
(`test_update_install_methods.py:193`) pins on the Python side, and the shell mirror must be
pinned the same way.

### 2.1 Alternatives rejected

| option | verdict |
|---|---|
| shell calls the Python detector (`yadgar update --detect-method` or similar) | Removes the duplication properly — but the script exists **for non-Python callers**, i.e. for contexts where the Python CLI may not resolve. That is the whole reason it is a separate file. Rejected as defeating its purpose. |
| glob on `*/share/pipx/venvs/yadgar/*` in addition to the legacy one | Two hardcoded prefixes; the third pipx default breaks it again. 0109 deliberately went prefix-agnostic; the mirror must too. |
| leave it — "nothing important reads it" | Requires proving no consumer branches on it. Do the grep in §1.1; if genuinely dead, the right car is *delete the file*, not leave a wrong one. Say which in the PR. |

---

## 3. Files to change

| file | change |
|---|---|
| `scripts/install/detect_install_method.sh:34-38` | replace the single legacy glob with the two-rule form (§2) |
| `yadgar/core/update/install_methods.py:3-5` | update the docstring — the "still only matches the legacy layout" caveat becomes false |

Nothing else. Do not touch detection order, the other branches, or the exit codes: the script's
contract is documented at `:4-9` (`0` on success even for `unknown`, `1` when yadgar is not on
`PATH`) and callers depend on it.

---

## 4. TDD story

### 4.1 Where the test goes

`yadgar/tests/scripts/` — gated by CI at `.forgejo/workflows/ci-pr.yaml:79`. This matters: it is
the directory-gated job, so a shell-detector test placed anywhere else is **never run in PR CI**.

### 4.2 The existing pattern for testing a shell script

The repo tests shell scripts by invoking them under `subprocess` with a patched env, from Python.
Precedents: `yadgar/tests/_unit_render.py:64` (`subprocess.run([BASH, str(GENERATE_SYSTEMD_SH)],
…, env=env)`), and the several `test_v5_45_detect_runtime.py` /
`test_v5_46_2_detect_runtime_error_messages.py` suites which do the same for the sibling
`detect_runtime.sh`. Read `test_v5_45_detect_runtime.py` first and copy its harness rather than
writing a new one.

`BASH = shutil.which("bash") or "/run/current-system/sw/bin/bash"` is the established resolution
(`_unit_render.py:24`) — nix hosts have no `/bin/bash`.

The synthesized-layout technique already exists on the Python side:
`test_update_install_methods.py:29` builds `tmp_path/home/.local/pipx/venvs/yadgar/bin/yadgar` as
a real file. Reuse it, and additionally put that dir on `PATH` so the script's `command -v yadgar`
(`:14`) resolves to the fake.

### 4.3 Preferred shape: one test asserting BOTH detectors agree

Strongest form — a **parity** test that runs `detect_install_method.sh` *and*
`install_methods.detect_install_method()` against the same synthesized layout and asserts equal
output. That is the anti-drift net; the point of this car is that the two mirrors drifted for a
release.

**New file:** `yadgar/tests/scripts/test_install_method_detector_parity.py`

The `yadgar-install-surface-generators` wiki page says *"Prefer extending one of these to creating
a sixth"* about the five cross-generator invariants — none of them covers install-method
detection, and none of their harnesses fits (they render units, not detect installs). A new file
is justified; say so explicitly in the module docstring so the next reader does not have to
re-derive it.

Cases, each RED before the shell change:

| case | layout | expected |
|---|---|---|
| legacy pipx | `…/.local/pipx/venvs/yadgar/bin/yadgar` | `pipx` (already GREEN — the regression guard) |
| modern pipx ≥1.6 | `…/.local/share/pipx/venvs/yadgar/bin/yadgar` | `pipx` — **RED today** |
| explicit `PIPX_HOME` | `PIPX_HOME=<tmp>/custom`, binary at `<tmp>/custom/venvs/yadgar/bin/yadgar` | `pipx` — **RED today** |
| false positive guard | `…/opt/mypipxtool/bin/yadgar` | **not** `pipx` (mirrors `test_update_install_methods.py:193`) |
| nix / brew / source / unknown | as in the Python suite | unchanged — proves the port did not perturb the other branches |

Both detectors resolve symlinks (`realpath` at `:20`; `os.path.realpath` at
`install_methods.py:51`), so build **real dirs**, not symlink chains, unless a case is
specifically about symlink resolution.

Parity caveat to handle in the harness, not to paper over: the Python detector shells out to
`which yadgar` and the shell one uses `command -v`. Under a patched `PATH` both resolve the same
fake, but assert that they did — a test where one silently found the developer's real `yadgar`
would pass for the wrong reason.

---

## 5. Verification

**Fully provable locally.** Nothing here needs the VM: the layouts are synthesized, both detectors
are pure path inspection, and there is no unit, container, or service involved.

Optional confirmation on the fresh VM (`192.168.122.101`) if a real pipx ≥1.6 install is already
present from earlier QA: `bash scripts/install/detect_install_method.sh` should print `pipx` where
it previously printed `unknown`. This is a nice-to-have, not a gate.

Also run `shellcheck` on the edited script — the repo lints shell (`code-review:linter` covers it
and pre-commit hooks may too). A `[[ ]]` glob with an unquoted RHS is intentional here; make sure
the change does not introduce an accidental quoted-RHS literal match, which is the classic way to
break a bash glob comparison silently.

---

## 6. Rollback and ADRs

**Rollback:** revert the commit. Two files, no state, no migration, no unit regeneration.

**No ADR.** This mirrors an already-decided fix into a second language. 0109 recorded no ADR
either (it was a straightforward defect fix). If anything is worth capturing durably it is the
*class* — "the shell mirrors of Python helpers drift and nothing compares them" — and the parity
test in §4.3 is the durable artifact for that, not a decision record.

---

## 7. Ordering

Independent of every other car in the train. Touches no generator, no unit, no startup path. Can
land in parallel with #0110 / #0111 / #0027c with zero conflict risk.
