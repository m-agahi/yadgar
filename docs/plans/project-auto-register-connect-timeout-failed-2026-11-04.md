# Project auto-register: a connect-phase timeout is a failure, not "pending"

- **Status:** PLANNED (fleet plan, not built)
- **Date:** 2026-11-04
- **Ledger:** yaadgaar 1918 ("Project auto-register: URLError(TimeoutError) connect-phase shows still pending; should be failed")
- **Refines:** PR #90 / ADR-1131 (hook-minted project ids auto-register)

## Problem

The SessionStart hook posts the minted project id to the daemon route
`POST /hooks/project-register` (`yadgar/core/cli/hook.py::_post_project_register`,
hook.py:216-243, timeout `_REGISTER_TIMEOUT_S = 3.0`, hook.py:207). It sorts
the result into three outcomes: `answered`, `timeout`, `failed`. On `timeout`,
`_register_minted_project` (hook.py:246-287) prints a neutral line:

    yadgar: project registration for <id> still pending; check `yadgar project list`

The "pending" wording is justified by one fact only: the daemon took the
request, and its worker thread keeps running after the hook stops waiting
(hook.py:203-206, docs/reference/hooks.md:250-253).

Today both timeout shapes map to `timeout` (hook.py:238-241):

```python
except urllib.error.URLError as e:
    return ("timeout" if isinstance(e.reason, TimeoutError) else "failed"), None
except TimeoutError:
    return "timeout", None
```

The `URLError(TimeoutError)` shape comes from the connect phase. CPython's
`AbstractHTTPHandler.do_open` wraps an `OSError` raised by `h.request(...)`
(connect + send) in `URLError`; `h.getresponse()` and `resp.read()` raise a
bare `TimeoutError` [code-read: stdlib `urllib/request.py` `do_open`]. A
connect-phase timeout means the daemon never accepted the connection, so it
never saw the request and nothing will create the row. "Still pending" is
false there; the user waits for a row that never comes. It is a failure and
must print the existing `[yadgar] WARNING:` line that names the manual fix.

The existing test pins the wrong behaviour:
`yadgar/tests/hooks/test_hook_project_register.py:169-179`
(`test_post_timeout_is_classified_as_timeout`, param
`urllib.error.URLError(TimeoutError("timed out"))  # the connect phase`).

## Design (one option, recommended)

1. In `_post_project_register`, a `URLError` of any reason returns
   `("failed", None)`. Only a bare `TimeoutError` (response wait/read, after
   the daemon accepted the request) returns `("timeout", None)`.
2. `_register_minted_project` is unchanged: `failed` already yields the
   WARNING line (hook.py:275-280).
3. Update the docstrings that claim both shapes are timeouts (hook.py:216-228
   and the `_REGISTER_TIMEOUT_S` comment, hook.py:203-206) and the banner
   section of `docs/reference/hooks.md` (around lines 236-257).

Rejected: a separate "connect timed out" warning text. It adds a fourth
banner line for no new user action: the fix (start the daemon, then
`project_register`) is the same as for a refused connection.

Rejected: keep `timeout` and only reword the line. The daemon did not get the
request, so any "pending" wording stays false.

## Acceptance criteria

### AC1 [unit] `_post_project_register` classifies a connect-phase timeout as failed

Test file: `yadgar/tests/hooks/test_hook_project_register.py` (edit the
existing parametrized cases; `urlopen` is faked with the module's
`_fake_urlopen`).

- Main: Given `urllib.request.urlopen` raises
  `urllib.error.URLError(TimeoutError("timed out"))` / When
  `hk._post_project_register("quinyx/daneel")` / Then it returns
  `("failed", None)`.
- Boundary (unchanged response-phase case): Given `urlopen` raises
  `TimeoutError("timed out")` / When `hk._post_project_register("quinyx/daneel")` /
  Then it returns `("timeout", None)`.
- Boundary (socket alias): Given `urlopen` raises
  `urllib.error.URLError(socket.timeout("timed out"))` / When
  `hk._post_project_register("quinyx/daneel")` / Then it returns
  `("failed", None)`.
- Refusal cases stay failed: `URLError(ConnectionRefusedError(111, "refused"))`,
  `HTTPError(..., 401, ...)`, `ConnectionResetError()` each return
  `("failed", None)`.
- dont_care: exception messages; how many times `urlopen` is called is not
  pinned beyond "at least once".

### AC2 [unit] the banner for a connect-phase timeout is the WARNING line, not "still pending"

Test file: `yadgar/tests/hooks/test_hook_project_register_connect_timeout.py`
(new). The root conftest replaces `_register_minted_project` with a no-op for
every test; the test must bind the real function at import
(`_REAL_REGISTER = hk._register_minted_project`), as the existing module does,
and fake only `hk.urllib.request.urlopen`. No test may reach a live daemon.

- Main: Given `urlopen` raises `urllib.error.URLError(TimeoutError("timed out"))` /
  When `_REAL_REGISTER("quinyx/daneel")` / Then it returns exactly
  `[yadgar] WARNING: could not auto-register project quinyx/daneel (daemon unreachable or request refused). If writes fail with unknown project_id, call project_register("quinyx/daneel") once the daemon is up.`
  and the result does not contain `still pending`.
- Boundary: Given `urlopen` raises bare `TimeoutError("timed out")` / When
  `_REAL_REGISTER("quinyx/daneel")` / Then it returns exactly
  ``yadgar: project registration for quinyx/daneel still pending; check `yadgar project list` ``.
- Property: `_REAL_REGISTER` never raises for either exception shape.
- dont_care: log output; stderr.

## Scope fence (`allowed_additions`)

- files: `yadgar/tests/hooks/test_hook_project_register_connect_timeout.py`,
  `docs/plans/project-auto-register-connect-timeout-failed-2026-11-04.md`
- symbols: none (the change edits `_post_project_register` in place)
- deps: none
- settings: none

## Repo obligations (coder steps)

- **OB1 version_bump (core).** `scripts/check_version_bump.py` (pre-push
  `verify-version-bump-local`, CI `verify-version-bump`): any `yadgar/**`
  change needs a core bump vs the merge-base. Run
  `python3 scripts/bump_version.py --new 5.198.1`; the `sync-version` /
  `sync-uv-lock` hooks cascade to `server.json` `version`, `flake.nix`,
  `uv.lock`, `docker-compose.yml` `CORE_VERSION`. Verify with
  `pre-commit run check-versions --all-files`.
- **OB2 version_bump (compat window + backend).**
  `scripts/check_version_compat_window.py` (hook `check-version-compat-window`):
  `server.json` version 5.198.1 is outside `core.max` 5.198.0 in
  `yadgar/_shared/version_compat.json`. Set `core.max` to `5.198.1`. That file
  is a backend build input, so `scripts/check_backend_bump.py`
  (`check-backend-bump`) then demands a backend bump: set
  `server.json` `backend_version` to `5.89.1`, `backend.max` to `5.89.1`, and
  the mirrors (`yadgar/__init__.py` `BACKEND_VERSION`, `flake.nix`,
  `docker-compose.yml` `BACKEND_VERSION`). Same pattern as commits 8993f6af,
  95ad8df7 ("version bump only, no functional backend change").
- **OB3 changelog.** `docs/CHANGELOG.md` `## [Unreleased]`: add a top entry,
  bold lead sentence, no `**vX.Y.Z` inline marker
  (`check-changelog-unreleased-versions`). State: core 5.198.0 -> 5.198.1,
  backend 5.89.0 -> 5.89.1 (version bump only), and the behaviour change.
- **OB4 docs.** AGENTS.md PR rule "Every change to `yadgar/**` must update
  README.md and docs/ ... when behaviour changes": update the banner list in
  `docs/reference/hooks.md` (~lines 248-257) so "still pending" names only a
  response timeout after the daemon accepted the request, and a connect
  timeout falls under the WARNING bullet. README has no text on this banner
  (grep: no match), so no README edit.
- **OB5 other (test-weakening).** `scripts/check_test_weakening.py`
  (`check-test-weakening`): moving the `URLError(TimeoutError)` case from the
  timeout parametrize list to the failed list keeps every `assert` line; it
  must not lower the assert or strict counts. If the hook still flags the
  file, add an entry to `.test-weakening-allowlist.json` with the reason
  "spec change: ledger 1918", not a blanket entry.

Gate files checked: `.pre-commit-config.yaml` (sync-version, sync-uv-lock,
check-versions, check-backend-bump, check-version-compat-window,
check-ci-viz-version, check-capability-coverage, check-changelog-unreleased-versions,
check-test-weakening, type-ratchet, verify-version-bump-local),
`scripts/check_version_bump.py`, `scripts/check_backend_bump.py`,
`scripts/check_version_compat_window.py`, `scripts/check_test_weakening.py`,
`scripts/bump_version.py`, `AGENTS.md` PR section, `Makefile` test targets,
`pyproject.toml [tool.pytest.ini_options]`. No new Settings field, MCP tool,
migration or BC row, so `CAPABILITY_REGISTRY.md` needs no edit.

## Test command

`uv run --extra test pytest <paths> -n0` — probed on
`yadgar/tests/hooks/test_hook_project_register.py` in a throwaway worktree:
18 passed, exit 0.

## Risks

- `URLError(OSError)` in `do_open` also covers a timeout while *sending* the
  request after connect. With a ~40-byte body on loopback that is near
  impossible; if it happens, the daemon has at most a partial request and
  will not register, so "failed" is still the honest answer.
- A daemon whose listen backlog is full drops the SYN, which shows up as the
  connect timeout. That is the exact case this fix targets.

## Open decisions for the user

- None blocking. Optional: a distinct warning text for the connect timeout
  (rejected above as noise; say if you want it).
- `mypy` is not on PATH on this host; the `type-ratchet` hook may need it
  (it runs mypy over touched files). Unverified whether it runs through the
  project venv.
