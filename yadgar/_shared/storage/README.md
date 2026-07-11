# `_shared/storage/` — SurrealDB storage engine

The ONLY code that talks to the database directly. Everything else goes
through `StorageEngine` (or, from core write paths, through the file-queue →
backend drainer seam per ADR-0078).

- `engine.py` / `ops.py` — StorageEngine composition + query helpers
- `memory.py`, `wiki.py`, `entity.py` — per-store CRUD + search
- `migrations.py` — numbered schema migrations (never edit old ones)
- `db.py` — connection pool + query instrumentation

Seams: core read-tools call it directly today (tolerated until the post-T2
storage sink); core WRITES must not — use the file queue or a backend
endpoint. Genuinely dual (both layers execute it), so it stays in `_shared`.
