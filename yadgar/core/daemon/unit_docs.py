"""Comment blocks the rendered systemd units carry (task:0110, ADR-0190).

The parity baseline is the ``sed`` render of ``scripts/install/*.in``, and those
templates document themselves: roughly sixty comment lines per unit explaining
why each directive is shaped the way it is. The converged renderer emits them,
so they live here rather than inline in ``units.py`` — the builders would
otherwise be mostly prose and blow the function-LOC cap.

``{runtime}`` / ``{state_dir}`` placeholders are filled by
``unit_model.comments()``, mirroring what ``sed`` substituted into the same
sentences.
"""

from __future__ import annotations

__all__ = [
    "BACKEND_SERVICE_DOC",
    "BACKEND_UNIT_DOC",
    "CORE_DOCKER_HOST_DOC",
    "CORE_GATE_DOC",
    "CORE_MKDIR_DOC",
    "CORE_READINESS_DOC",
    "CORE_STATE_DOC",
    "CORE_WANTS_DOC",
]


BACKEND_UNIT_DOC = (
    "SurrealDB (:8000) is published on LOOPBACK ONLY. The nightly-cycle and vacuum",
    "units execute on the HOST (the vacuum flow interleaves phases needing different",
    "daemon states and the container image has no systemctl), and both default",
    "YADGAR_DB_URL to http://127.0.0.1:<port>. Without this publish they render,",
    "activate, fire, and connection-refuse. Mirrors flake.nix's shipped posture.",
    "Port is overridable via YADGAR_BACKEND_SURREAL_PORT at render time.",
    "",
    "ADR-0180: the ExecStart below MUST forward YADGAR_MCP_AUTH_TOKEN. The backend",
    "SERVES /admin/* and compares the presented bearer against its own copy of that",
    "variable (_require_admin_token in yadgar/backend/embed_service/embed_service.py);",
    "with it unset the gate fails closed and every admin call — seed, consolidate,",
    "dbsize, recall, restore, viz, read-query — is rejected before doing any work.",
    "EnvironmentFile below loads it into the UNIT's env; that does not reach the",
    "CONTAINER without an explicit -e. Cross-generator guard:",
    "yadgar/tests/scripts/test_admin_token_cross_generator.py.",
)

BACKEND_SERVICE_DOC = (
    "task:0105 — podman-only. DOCKER_HOST names podman's rootful socket, which the",
    "local podman CLI ignores (it reads CONTAINER_HOST) but the docker-compat shim",
    "path may use. On docker it points the docker CLI at a socket that does not",
    "exist, so every Exec* below fails; omitted there so docker uses its own",
    "default socket / active context. The renderer emits the line on podman only.",
    "task:0110 / ADR-0190: this unit now carries a readiness contract. The Type below",
    "renders `notify` for podman, whose sd_notify proxy emits READY=1 on the first",
    "HEALTHY healthcheck, and `exec` for docker, which gets a bounded /health gate",
    "instead. It was `simple` on both until now — under which systemd called the unit",
    "started the instant `podman run` FORKED, so the core's start budget had the",
    "backend's cold model load inside it (ADR-0187's premise, false on this path).",
)

CORE_WANTS_DOC = (
    "Wants=, NOT Requires= (task:0111 / ADR-0188). Requires= propagates STOP: a",
    "`systemctl --user stop yadgar-backend` — which every vacuum does to quiesce",
    "the surrealkv store — would take the core down with it and drop every",
    "connected MCP session. Wants= keeps the pull-in (starting core still starts",
    "the backend); After= above keeps the start ordering. Only the stop",
    "propagation is dropped, which is the whole point.",
)

CORE_STATE_DOC = (
    "The -v {state_dir} bind below projects the container's",
    "YADGAR_VACUUM_TRIGGER_PATH write onto the host, where",
    "yadgar-vacuum-trigger.path watches for it. Both halves are required: the env",
    'var has no code default, so unset means "no watcher on this surface" and',
    "vacuum_now() refuses rather than writing into a void.",
    "{state_dir} must be spelled IDENTICALLY here and in that unit's PathExists=.",
)

CORE_READINESS_DOC = (
    "task:0105 — readiness is runtime-conditional. The Type below renders `notify`",
    "for podman (its default sdnotify=container mode passes NOTIFY_SOCKET into the",
    "container and forwards the daemon's own READY=1) and `exec` for docker, which",
    "has no sd_notify proxy of any kind — nothing would ever send READY=1 there and",
    "the unit would sit until TimeoutStartSec. The docker-only ExecStartPost below",
    "replaces that signal with a bounded /health poll; per man systemd.service,",
    '"the execution of ExecStartPost= is taken into account for the purpose of',
    'Before=/After= ordering constraints", which is the guarantee notify buys.',
    "The renderer emits each runtime-conditional line only on the arm that needs it.",
)

CORE_DOCKER_HOST_DOC = (
    "DOCKER_HOST is a podman-only belt: it names podman's rootful socket, which the",
    "local podman CLI ignores (it reads CONTAINER_HOST) but the docker-compat shim",
    "path may use. On docker it is not merely useless, it points the docker CLI at a",
    "socket that does not exist, so every Exec* in this unit fails — hence omitted",
    "entirely there, letting docker use its own default socket / active context.",
)

CORE_MKDIR_DOC = (
    "Pre-create the state dir HERE, not only at render time in generate_systemd.sh.",
    "podman does NOT auto-create a missing `-v` host source — it fails the run with",
    "`statfs <path>: no such file or directory`. So if the dir is gone at start",
    "(user cleaned ~/.local/state, or a purge ran between install and boot) the CORE",
    "daemon would fail to start, not just the vacuum trigger. Mirrors the flake,",
    "which pre-creates it in this same unit's ExecStartPre for the same reason.",
    "`mkdir` is resolved from the unit's $PATH, like {runtime} above.",
)

CORE_GATE_DOC = (
    "Docker readiness gate. `curl` is resolved from the unit's $PATH, like `mkdir`",
    "and {runtime} above. No shell wrapper, so systemd's own `$` expansion never",
    "applies; curl's --retry does the polling. --retry-connrefused covers \"port not",
    'listening yet", --retry-all-errors covers "listening but not healthy yet"',
    "(--fail makes a 5xx an error, which plain --retry would not retry).",
    "45 * 2s = 90s, inside the TimeoutStartSec=120 below — a gate that outlives the",
    "start timeout would turn a slow start into a Restart=on-failure crashloop.",
    "/health (readiness) not /health/live (liveness, ADR-0019): podman's READY=1 is",
    "emitted last, after the full engine set, so gating on liveness would mark this",
    "unit active EARLIER than the podman arm does.",
)
