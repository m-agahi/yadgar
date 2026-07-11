# `core/viz/` — visualization server

Serves the 3D knowledge-graph UI and its APIs from the core container.

- `viz_server.py` — static file server + backend proxy
- `viz_meta.py` — node/edge registries, legend, colors (pure presentation)
- `viz_daemon_health.py` — daemon-health scraper + `/api/daemon-health`

Split census (verdict #11): HTTP serving stays core; data assembly + layout
compute (see `core/graph/`) forward to the backend sleep-cycle seam in
Car E3. Don't add new compute here.
