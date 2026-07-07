"""rules subcommand — manage policy rules."""

import sys
from pathlib import Path


def cmd_rules_export(args):
    """Export all active rules to stdout as YAML (or JSON if ruamel.yaml missing)."""
    import json

    from yadgar._shared.config import Settings
    from yadgar._shared.rules_engine import RulesEngine
    from yadgar._shared.storage import StorageEngine

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    storage = StorageEngine(db_path)
    try:
        engine = RulesEngine(storage, settings)
        rules = engine.export_rules()
        try:
            import io

            from ruamel.yaml import YAML

            y = YAML()
            y.default_flow_style = False
            buf = io.StringIO()
            y.dump(rules, buf)
            print(buf.getvalue(), end="")
        except ImportError:
            print(json.dumps(rules, indent=2))
    finally:
        storage.close()


def cmd_rules_import(args):
    """Import rules from a YAML or JSON file."""
    import json

    from yadgar._shared.config import Settings
    from yadgar._shared.rules_engine import RulesEngine
    from yadgar._shared.storage import StorageEngine

    rules_path = Path(args.file)
    if not rules_path.exists():
        print(f"File not found: {rules_path}", file=sys.stderr)
        sys.exit(1)

    try:
        from ruamel.yaml import YAML

        y = YAML()
        with open(rules_path) as f:
            rules = y.load(f)
    except ImportError:
        with open(rules_path) as f:
            rules = json.load(f)

    if not isinstance(rules, list):
        print("Rules file must contain a YAML/JSON list of rule objects.", file=sys.stderr)
        sys.exit(1)

    settings = Settings()
    db_path = str(Path(args.db_path or settings.DB_PATH).expanduser())
    storage = StorageEngine(db_path)
    try:
        engine = RulesEngine(storage, settings)
        count = engine.import_rules(rules)
        print(f"Imported {count} of {len(rules)} rules from {rules_path}")
    finally:
        storage.close()


def cmd_rules(args, rules_parser):
    """Dispatch rules sub-subcommands."""
    from yadgar._shared.rules_engine import RulesEngine  # noqa: F401 (ensure importable)

    sub = getattr(args, "rules_command", None)
    if sub is None:
        rules_parser.print_help()
    elif sub == "export":
        cmd_rules_export(args)
    elif sub == "import":
        cmd_rules_import(args)


def register(subparsers):
    rules_parser = subparsers.add_parser("rules", help="Manage policy rules")
    rules_sub = rules_parser.add_subparsers(dest="rules_command")
    rules_export_p = rules_sub.add_parser("export", help="Export all active rules to stdout (YAML)")
    rules_export_p.add_argument("--db-path", type=str, default=None, help="Database path")
    rules_import_p = rules_sub.add_parser("import", help="Import rules from a YAML or JSON file")
    rules_import_p.add_argument("file", help="Path to the rules YAML/JSON file")
    rules_import_p.add_argument("--db-path", type=str, default=None, help="Database path")
    rules_parser.set_defaults(func=lambda args: cmd_rules(args, rules_parser))
    return rules_parser
