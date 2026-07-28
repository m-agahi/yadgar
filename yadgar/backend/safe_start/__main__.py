"""Entry point for ``python -m yadgar.backend.safe_start``.

ADR-0084 (T2 Car D) converted ``safe_start`` from a flat module into a package
(``yadgar/backend/safe_start/{__init__.py, safe_start.py}``) but never added a
``__main__.py``. ``python -m <package>`` requires one — without it,
``entrypoint-backend.sh``'s ``python3 -m yadgar.backend.safe_start
preflight|recover`` invocations silently fail with ``No module named
yadgar.backend.safe_start.__main__``, killing both the split-brain preflight
guard and the torn-manifest auto-restore ``recover`` path in the packaged
image (see docs/plans/fix-systemd-generate-missing-queue-base-2026-07-28.md §5).
"""

from .safe_start import main

raise SystemExit(main())
