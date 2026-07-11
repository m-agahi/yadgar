"""yadgar.core.viz — visualization server package.

T2 Car D3 (layer-boundary train): the flat ``viz_*`` modules packaged per the
no-lone-files law (ADR-0084). Core keeps the viz HTTP server per the census
(verdict #11); data-assembly + layout compute forward to backend in Car E3.

  viz_server.py        — static viz HTTP server + backend proxy
  viz_meta.py          — node/edge type registries, legend + color builders
  viz_daemon_health.py — daemon-health scraper + /api/daemon-health assembly

Back-compat PEP-562 shims remain at the old ``yadgar.core.viz.viz_server`` /
``.viz_meta`` / ``.viz_daemon_health`` paths. Import the submodules directly.
"""
