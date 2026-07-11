# `_shared/config/` — configuration

Settings model, knob registry, and YAML config file I/O. Three-way sync
(env ↔ YAML ↔ registry) is lint-enforced (I25, `check-config-three-way-sync`).

- `config.py` — `Settings` + `resolve_knob`/`get_settings` (env + YAML aware)
- `config_registry.py` — `ConfigEntry` registry, startup config log + gauges
- `config_yaml.py` — YAML I/O, `FIELD_META` schema, config CLI impls

Adding a knob touches all three files + `CAPABILITY_REGISTRY.md` — the
pre-commit hook will tell you. I13-oversized (`config_yaml.py`); internal
split is task #18.
