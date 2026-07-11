# `core/install/` — install + bootstrap helpers

- `install_hooks_lib.py` — Claude Code hook installation (settings.json
  surgery, scope resolution, shebang handling). NOTE: two shebang tests
  fail by design in agent worktrees and pass in CI.
- `install_subagents_lib.py` — bundled subagent installation
- `platform_paths.py` — cross-platform Claude config-dir resolution

Paired with `core/install_assets/` (bundled agents) and `yadgar/hooks/`
(the hook scripts themselves).
