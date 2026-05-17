"""config subcommand — manage Yadgar configuration."""


def cmd_config(args, config_parser):
    """Dispatch config sub-subcommands."""
    from yadgar.config_yaml import (
        cmd_config_edit,
        cmd_config_get,
        cmd_config_init,
        cmd_config_list,
        cmd_config_set,
    )

    sub = getattr(args, "config_command", None)
    if sub is None:
        config_parser.print_help()
    elif sub == "init":
        cmd_config_init(args)
    elif sub == "list":
        cmd_config_list(args)
    elif sub == "get":
        cmd_config_get(args)
    elif sub == "set":
        cmd_config_set(args)
    elif sub == "edit":
        cmd_config_edit(args)


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
    config_get_p.add_argument("key", help="Setting name (e.g. daemon_check_interval)")
    config_set_p = config_sub.add_parser("set", help="Set a setting value in config.yaml")
    config_set_p.add_argument("key", help="Setting name")
    config_set_p.add_argument("value", help="New value")
    config_sub.add_parser("edit", help="Open config.yaml in $EDITOR")
    config_parser.set_defaults(func=lambda args: cmd_config(args, config_parser))
    return config_parser
