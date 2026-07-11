"""yadgar.backend.embed_service — backend HTTP service package.

T2 Car D2 (layer-boundary train): the flat embed_service pair packaged per
the no-lone-files law (ADR-0084). ``yadgar.backend.embed_service`` IS the old
dotted path — the uvicorn entrypoint ``yadgar.backend.embed_service:app`` and
every symbol import keep working through this PEP-562 ``__init__``.

  embed_service.py         — FastAPI app: /embed, /rerank, /recall, /restore,
                             /consolidate, /admin routes (I13 oversized;
                             internal split = task #18)
  embed_service_metrics.py — Prometheus collectors + CacheStatsCollector for
                             the service (back-compat shim remains at the old
                             ``yadgar.backend.embed_service_metrics`` path)

CATCH-ALL forward (not an _EXPORTS map): the service module is the most
attribute-patched module in the test suite (os/time/model singletons/cache
knobs), so the package forwards EVERY attribute to the submodule. Tests that
REBIND module globals must target ``yadgar.backend.embed_service.embed_service``
directly — rebinding through the package does not reach the submodule.
"""


def __getattr__(name: str):
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return getattr(importlib.import_module("yadgar.backend.embed_service.embed_service"), name)


def __dir__() -> list[str]:
    import importlib  # noqa: PLC0415 — lazy by design (PEP-562 re-export)

    return list(globals()) + dir(
        importlib.import_module("yadgar.backend.embed_service.embed_service")
    )
