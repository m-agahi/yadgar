# `core/daemon/` — daemon lifecycle

- `daemon.py` — `YadgarDaemon`: docker/podman container orchestration,
  `ContainerProfile` memory/CPU sizing, image resolution
- `daemons.py` — background threads (auto-update check, …)
- `sd_notify.py` — systemd READY/RELOADING/STOPPING notifications
- `drain.py` — in-flight request draining + embed-cache snapshot on stop

Host-ops by definition (runs OUTSIDE the containers it manages) — this is
core-wheel territory; never move it behind a backend endpoint.
