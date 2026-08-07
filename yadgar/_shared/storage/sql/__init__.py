"""yadgar._shared.storage.sql — engine #2 (MariaDB, ADR-0195).

The relational set's home. Sibling of the SurrealDB ``StorageEngine`` in the
parent package, NOT a subclass, mixin or shared-ABC relative of it — ADR-0195
specifies two concrete classes selected at the composition root, and PR #32
showed what a shared MRO costs (a MariaDB mixin ordered behind SurrealDB's, so
SurrealDB won every call and the MariaDB half was dead code with green tests).

  config.py    — MySQL option-file parsing. PURE STDLIB, no third-party import.
  mariadb.py   — ``MariaStorageEngine``: async SQLAlchemy engine over the local
                 unix socket. Imports ``sqlalchemy`` lazily, inside functions.
  migrate.py   — the Alembic runner (car D). Programmatic ``Config``, no ini
                 file; ``upgrade_to_head`` is awaited from the backend lifespan.
  migrations/  — the Alembic environment + revision chain. Loaded BY PATH by
                 alembic, never imported as a module (``env.py``'s body calls
                 ``context.is_offline_mode()``, which needs a pushed context).
                 Deliberately separate from SurrealDB's hand-rolled chain in
                 ``../migrations.py`` — spine schema D34: one ordered list
                 spanning two engines has no meaningful "version N".

Why it lives under ``_shared`` and not ``yadgar/backend``: only the backend
touches it (ADR-0078/ADR-0200 — core forwards to backend admin ops and never
opens a database), but the composition root is
``_shared/runtime/lifecycle.py``, and ``_shared -> backend`` is forbidden by
the import-linter contracts save two permanently-waived DI edges. Putting it
here needs no third waiver, and it sits beside the storage class it partners.

Nothing in this package is imported at composition-root import time — see
``lifecycle._init_sql_storage``. ``sqlalchemy``/``asyncmy`` ship in the ``sql``
extra, which the yadgar-ci image does not bake (``Dockerfile.ci:116``), so a
hard import would break every CI test until that image is rebuilt.
"""

from yadgar._shared.storage.sql.config import (
    MariaClientConfig,
    default_option_file_path,
    read_client_option_file,
)
from yadgar._shared.storage.sql.mariadb import MariaStorageEngine

__all__ = [
    "MariaClientConfig",
    "MariaStorageEngine",
    "default_option_file_path",
    "read_client_option_file",
]
