"""seed subcommand — bootstrap memory for an existing project."""

import sys
from pathlib import Path


def _load_anchors_yaml(anchors_path: str) -> list[dict]:
    """Load anchor entries from a YAML file. Returns list of dicts."""
    path = Path(anchors_path)
    if not path.exists():
        raise FileNotFoundError(f"Anchors file not found: {path}")
    try:
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
    except ImportError:
        from ruamel.yaml import YAML

        yaml_parser = YAML()
        with open(path) as f:
            data = yaml_parser.load(f)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "anchors" in data:
        return list(data["anchors"])
    raise ValueError(f"anchors.yaml must be a list or dict with 'anchors' key, got: {type(data)}")


def _seed_anchors(anchors: list[dict], db_path: str | None, dry_run: bool) -> dict:
    """Seed anchor entries into yadgar memory. Returns result dict."""
    import hashlib

    results = {"loaded": len(anchors), "created": 0, "skipped": 0, "dry_run": dry_run}

    if dry_run:
        for entry in anchors:
            content = entry.get("content", "")
            tags = entry.get("tags", [])
            print(
                f"  [DRY RUN] Would seed anchor: [{', '.join(tags)}] {content[:80]}...",
                file=sys.stderr,
            )
        results["created"] = len(anchors)
        return results

    from yadgar.db import get_db

    db = get_db(db_path)
    for entry in anchors:
        content = entry.get("content", "")
        tags = entry.get("tags", [])
        if not content:
            print("  WARN: anchor entry missing 'content', skipping", file=sys.stderr)
            results["skipped"] += 1
            continue
        # Dedup by content hash
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        existing = db.execute(
            "SELECT id FROM memories WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if existing:
            results["skipped"] += 1
            continue
        db.execute(
            "INSERT INTO memories (content, tags, content_hash) VALUES (?, ?, ?)",
            (content, ",".join(tags), content_hash),
        )
        results["created"] += 1
    db.commit()
    return results


def cmd_seed(args):
    """Bootstrap memory for an existing project by scanning its structure."""
    import json

    # Handle --anchors mode
    if getattr(args, "anchors", None):
        anchors_path = args.anchors
        print(f"Loading anchors from: {anchors_path}", file=sys.stderr)
        try:
            anchors = _load_anchors_yaml(anchors_path)
        except (FileNotFoundError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        result = _seed_anchors(anchors, db_path=args.db_path, dry_run=args.dry_run)

        if args.dry_run:
            print(
                f"\n[DRY RUN] Would seed {result['created']} anchors from {anchors_path}",
                file=sys.stderr,
            )
        else:
            print(
                f"\nSeeded {result['created']} anchors ({result['skipped']} skipped, already present)",
                file=sys.stderr,
            )
        print(json.dumps(result))
        return

    from yadgar.seed import seed_project

    directory = str(Path(args.directory).resolve())
    print(f"Seeding project: {directory}", file=sys.stderr)

    result = seed_project(
        directory=directory,
        db_path=args.db_path,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(
            f"\n[DRY RUN] Would create {result['memories_generated']} memories for {result['project']}\n",
            file=sys.stderr,
        )
        for mem in result.get("memories", []):
            tags = ", ".join(mem["tags"])
            print(f"  [{tags}] {mem['content'][:120]}...", file=sys.stderr)
    else:
        replaced_msg = f", replaced {result['replaced']} old" if result.get("replaced") else ""
        print(
            f"\nSeeded {result['project']}: "
            f"{result['created']} created{replaced_msg} "
            f"(from {result['memories_generated']} total)",
            file=sys.stderr,
        )

    print(json.dumps(result))


def register(subparsers):
    p = subparsers.add_parser("seed", help="Bootstrap memory for an existing project")
    p.add_argument("directory", nargs="?", help="Project directory to scan and seed")
    p.add_argument(
        "--anchors",
        type=str,
        default=None,
        metavar="FILE",
        help="YAML file of anchor entries to seed into memory (v5.45.0+)",
    )
    p.add_argument("--db-path", type=str, default=None, help="Database path")
    p.add_argument(
        "--dry-run", action="store_true", help="Scan and show what would be stored without storing"
    )
    p.set_defaults(func=cmd_seed)
