"""Core-side migration scripts — operator-invoked one-shots.

Car D (2026-08-14 train, §3): ``rekey_corpus`` is the corpus re-key
migration that walks the live ``memory`` + ``wiki_page`` tables via
the ``rekey_discover_directories`` admin op, derives a
``directory_context → project_id`` map, writes it to
``.yadgar/project-id-map.tsv`` (gitignored), and on operator
confirmation calls ``create_project_row`` per seed row + delegates
to ``project_id_backfill`` for the row-level UPDATE.

Imports stay lazy (this package is reached only via the
``yadgar migrate rekey`` CLI subcommand) so the rest of the core
does not pay the import cost on every lifecycle init.
"""

from __future__ import annotations
