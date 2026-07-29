<!-- YADGAR CODE-GRAPH-REFRESH PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory} = your current working directory (absolute path; the project root)
       {project}   = basename of {directory}
-->

Yadgar code-graph-refresh maintenance. This keeps the project's `code_graph`
architecture digest (an always-injected memory BLOCK: layers, hotspots,
entry-points, endpoints) in sync with the default-branch source. It is a DUMB
PIPE — the host CLI `yadgar code-graph` does the indexing/rendering; YOU wire its
output into the `block_*` write path. This is NOT the checkpoint and NOT the
anchor-audit; do NOT run capture or anchor steps here.

READ FOR REAL: every "run X" / "call X" below means DO it THIS turn and act on
the OUTPUT IT RETURNS. Do not reconstruct state from an earlier turn.

1. RUN THE HOST REFRESH (Bash). It resolves the default branch, indexes a clean
   `origin/<default>` checkout (never your dirty working tree), renders the
   digest, and prints ONE JSON object to stdout:

        yadgar code-graph refresh {directory}

   Output is one of:
     - `{"block_name":"code_graph","skipped":true,"reason":"..."}`
     - `{"block_name":"code_graph","directory":"<canonical_root>/<subdir>",
        "content":"<digest>","chars":N,"skipped":false}`

2. `skipped` is true → NOTHING TO DO. This is the common no-op (opted out, no
   remote, binary absent, or no change). Say so briefly (e.g. "code_graph
   refresh skipped: <reason>") and continue the conversation. Do NOT write a block.

   NOTE: offline / fetch-failure does NOT always land here. When the fetch fails
   but a cached index and a resolvable sha exist, the CLI re-emits the CACHED
   digest with `skipped:false` and a trailing `stale @ <sha>` marker — so step 3
   runs and the block IS written. That is deliberate: a silent skip would leave
   the previously-written block serving an aged digest with no marker at all.
   Nothing changes for you mechanically — branch on `skipped` exactly as below.

3. `skipped` is false → WRITE THE BLOCK (create-or-update). Use the payload's OWN
   `directory` field (the canonical_root+subdir, NOT necessarily {directory}) so
   the digest injects at the exact dir the session runs in:

     a. Try update first:
          block_update(name="code_graph", scope="project",
            directory=<payload.directory>, content=<payload.content>)
     b. If that errors because the block does not exist (not-found), create it:
          block_create(name="code_graph", scope="project",
            directory=<payload.directory>, content=<payload.content>)

   Both are secret-gated (same gate as wiki_add) — the digest is a summary
   (layer / hotspot / endpoint NAMES), so a rejection is unexpected; if the gate
   rejects, report it and STOP (do not force).

WRAP UP. Report the outcome in one line (refreshed / skipped: <reason> /
block created) and continue — if you were mid-thought, repeat your last question
so the flow resumes.
