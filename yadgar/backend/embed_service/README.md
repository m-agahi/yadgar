# `backend/embed_service/` — the backend HTTP service

The FastAPI app core talks to: `/embed`, `/rerank`, `/recall` (fan-out +
landscape), `/restore`, `/consolidate`, `/admin/*`, `/metrics`, `/health`.
Started by `entrypoint-backend.sh` as `uvicorn yadgar.backend.embed_service:app`.

- `embed_service.py` — routes + model lifecycle + caches (I13-oversized;
  internal split = task #18)
- `embed_service_metrics.py` — service Prometheus collectors +
  `CacheStatsCollector`

This is THE forward seam for core→backend compute: new cross-layer
functionality = new endpoint here (+ `BACKEND_VERSION` bump), never a direct
import in either direction.
