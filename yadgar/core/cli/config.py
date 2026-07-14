"""config subcommand — manage Yadgar configuration."""


def _config_dispatch_table() -> dict:
    """Lazy import: build sub-command → handler mapping on first call."""
    from yadgar._shared.config.config_yaml import (
        cmd_config_edit,
        cmd_config_get,
        cmd_config_init,
        cmd_config_list,
        cmd_config_set,
    )
    from yadgar.core.config_sync import cmd_config_sync

    return {
        "init": cmd_config_init,
        "list": cmd_config_list,
        "get": cmd_config_get,
        "set": cmd_config_set,
        "edit": cmd_config_edit,
        "sync": cmd_config_sync,
    }


def cmd_config(args, config_parser):
    """Dispatch config sub-subcommands."""
    sub = getattr(args, "config_command", None)
    if sub is None:
        config_parser.print_help()
        return
    handler = _config_dispatch_table().get(sub)
    if handler is not None:
        handler(args)


def register(subparsers):
    config_parser = subparsers.add_parser("config", help="Manage Yadgar configuration")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_init_p = config_sub.add_parser(
        "init", help="Write default config.yaml with all settings commented"
    )
    config_init_p.add_argument("--force", action="store_true", help="Overwrite existing config")
    config_list_p = config_sub.add_parser(
        "list", help="List all settings with current values and sources"
    )
    config_list_p.add_argument(
        "--section", type=str, default=None, help="Filter to a section (e.g. daemon)"
    )
    config_get_p = config_sub.add_parser("get", help="Get a single setting value")
    config_get_p.add_argument("key", help="Setting name (e.g. narrative_interval_hours)")
    config_set_p = config_sub.add_parser("set", help="Set a setting value in config.yaml")
    config_set_p.add_argument("key", help="Setting name")
    config_set_p.add_argument("value", help="New value")
    config_sub.add_parser("edit", help="Open config.yaml in $EDITOR")
    config_sync_p = config_sub.add_parser(
        "sync",
        help="Incrementally sync config.yaml with current Settings model fields",
    )
    config_sync_p.add_argument(
        "--check",
        action="store_true",
        help="List missing keys without writing (exits nonzero if any found)",
    )
    config_sync_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print diff to stdout, no file change",
    )
    config_sync_p.add_argument(
        "--remove-unknown",
        action="store_true",
        help="Delete yaml keys not in current Settings (default: preserve)",
    )
    config_parser.set_defaults(func=lambda args: cmd_config(args, config_parser))
    return config_parser
