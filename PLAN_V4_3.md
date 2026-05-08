# Yadgar v4.3 Plan — DLQ, Retry Policy, wiki_add SQL Fix, Notifications, Backup Schedule

## Context

On 2026-05-07/08, SurrealDB inside `yadgar-backend` ran at 60–95% CPU sustained for 24+ hours and contributed to a laptop running its fan all night. Two `wiki_add` operation files sat in `~/.yadgar/queue/` and were re-attempted indefinitely by `QueueDrainer._drain_once` (`yadgar/file_queue.py:184`). Each retry hit SurrealDB's `/sql` endpoint with the same malformed payload and got `400 Bad Request`. There is no attempt counter, no backoff, and no DLQ — so the loop is effectively infinite and burns DB CPU on parsing/validation forever.

Stuck files:

- `0001778139482800_20354d97-99f4-4f29-8469-3e01c6a76231.json` (3.9 KB, AWS Route53 glossary)
- `0001778174454315_97583a53-4470-4bbd-ad38-735cb19c15d7.json` (7.1 KB)

The malformed SQL almost certainly comes from string-interpolating markdown `content` into a SurrealQL statement without escaping. The Route53 glossary content contains plenty of triggers: `[[wikilinks]]`, triple-backtick fences, single quotes, the `🚨` emoji.

This minor release closes the entire failure mode — bad payloads can never spin the DB again, the specific bug is fixed, future Claude sessions know about stuck data, and overnight backup no longer wakes the laptop.

## Points

### 1. DLQ directory + retry policy

Add a third sibling directory to `~/.yadgar/queue/` and `~/.yadgar/archive/`:

- `~/.yadgar/dlq/` — files the drainer gave up on, plus a `<filename>.error.json` sidecar describing why.

Drainer (`yadgar/file_queue.py::QueueDrainer._drain_once`) gains a per-file in-memory attempt tracker:

```python
@dataclass
class _Attempt:
    count: int = 0
    next_retry_at: float = 0.0     # epoch seconds
    last_error: str = ""
    first_failed_at: float = 0.0
```

On each pass, files whose `next_retry_at` has not been reached are skipped. On `_apply()` exception:

- Classify: HTTP 4xx → `permanent`; HTTP 5xx, ConnectError, Timeout, everything else → `transient`.
- Increment `count`, compute backoff: `next_retry_at = now + min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (count - 1))`.
- If `count >= MAX_PERMANENT_ATTEMPTS` (default **3**) for permanent, or `count >= MAX_TRANSIENT_ATTEMPTS` (default **20**) for transient, call `_move_to_dlq(path, error_meta)`.
- Log warn per failed attempt; log error with `MOVED TO DLQ` when threshold hit.

`_move_to_dlq(path, meta)`:

- `dlq_dir = self.queue.root / "dlq"` (mkdir on `FileQueue.__init__`)
- Atomic rename `path → dlq_dir / path.name`
- Atomic tmp+rename of `dlq_dir / (path.name + ".error.json")` containing:
  ```json
  {
    "op_type": "...",
    "first_failed_at": "...",
    "last_failed_at": "...",
    "attempts": 3,
    "classification": "permanent",
    "last_error": "<status_code> <body[:1000]>",
    "moved_to_dlq_at": "..."
  }
  ```
- Append a JSON-line event to `~/.yadgar/dlq/.events.log` (see point 4).

On successful drain: clear tracker entry and archive as today (existing behaviour).

Retry tracking is **in-memory only**. Acceptable because thresholds are tight enough that even from-scratch counting on container restart cannot burn a CPU core.

Cleanup parallel: extend the existing "archive cleanup every ~120 passes" branch in `_drain_once` to also prune DLQ entries older than `QUEUE_DLQ_RETENTION_DAYS` (default **90 days** — much longer than archive's 30, with a loud log line; DLQ items represent data the user should review before they vanish).

New config knobs in `yadgar/config.py` (env + YAML):

| Setting | Default | Purpose |
|---------|---------|---------|
| `QUEUE_MAX_PERMANENT_ATTEMPTS` | 3 | Threshold for 4xx failures |
| `QUEUE_MAX_TRANSIENT_ATTEMPTS` | 20 | Threshold for 5xx / network failures |
| `QUEUE_BACKOFF_BASE_S` | 30 | Initial retry delay |
| `QUEUE_BACKOFF_MAX_S` | 3600 | Cap |
| `QUEUE_DLQ_RETENTION_DAYS` | 90 | DLQ pruning |

### 2. wiki_add SQL escape fix

Suspect path: `yadgar/server.py::wiki_add` (line 1936) → `_wiki.add` / `_wiki.ingest` in the wiki module. The SQL string is sent to `http://yadgar-backend:8000/sql` (SurrealDB's REST endpoint). Most likely the frontend interpolates `content` directly into a SurrealQL statement, e.g.:

```python
db.query(f"CREATE wiki_page CONTENT {{ content: '{content}', ... }}")
```

Two acceptable fixes, in order of preference:

1. **Parameterized query.** SurrealDB's `/sql` endpoint accepts the query body and bound variables via the `?vars=...` query string (or via the SDK `.query(sql, vars)` API). Rewrite the wiki builder to bind `$content`, `$title`, `$tags`, etc. as parameters. This is the durable fix.
2. **String escaping.** If parameterization touches too many call sites, minimum viable: SurrealQL string literals use single quotes; escape `'` → `''` and reject/escape backslash. Audit identifiers built from slug for backtick (record-ID delimiter).

Add a regression test at `tests/test_wiki_sql_escape.py` that ingests the actual stuck Route53 glossary content as a fixture and asserts the resulting page round-trips through `wiki_add` → `wiki_read`.

### 3. DLQ recovery MCP tools

Two new tools in `yadgar/server.py`:

- **`dlq_inspect()`** — list files in `~/.yadgar/dlq/` with `op_type`, `attempts`, `last_error`, `moved_to_dlq_at`, file size. No DB access; pure filesystem read of `.error.json` sidecars.
- **`dlq_requeue(filename: str)`** — atomically move `dlq/<filename>` back to `queue/<filename>` and delete the `.error.json` sidecar. Reset tracker entry. Next drain pass retries it.

Recovery pattern after this release ships and the SQL bug is fixed:

```
dlq_requeue("0001778139482800_20354d97-99f4-4f29-8469-3e01c6a76231.json")
dlq_requeue("0001778174454315_97583a53-4470-4bbd-ad38-735cb19c15d7.json")
```

Both stuck wiki pages then flow through correctly. Data preserved.

### 4. Notifications when DLQ grows

Goal: nobody is surprised by an overnight CPU spike again, and the next Claude session knows if data is stuck.

Two channels, both fired from `_move_to_dlq`:

#### 4a. Claude-side (in-band, persistent)

Extend the existing `/hooks/prompt-recall` handler in `yadgar/server.py` to include a `dlq_alerts` array when `~/.yadgar/dlq/` is non-empty:

```json
{
  "context": "...existing recall payload...",
  "dlq_alerts": [
    {
      "file": "0001778139482800_20354d97-...json",
      "op_type": "wiki_add",
      "moved_at": "2026-05-08T07:00:00Z",
      "attempts": 3,
      "last_error": "400 Bad Request: ..."
    }
  ]
}
```

Claude Code surfaces `prompt-recall` results into every prompt context, so the next session reading any project sees the alert and can ask the user about it. Cheap (just a directory listing of `.error.json` sidecars). Self-clearing once the user runs `dlq_requeue` or removes the file.

#### 4b. User-side (out-of-band, one-shot)

`_move_to_dlq` appends a JSON-line event to `~/.yadgar/dlq/.events.log` (append-only, atomic per-line):

```json
{"event":"dlq_move","ts":"2026-05-08T07:00:00Z","file":"...json","op_type":"wiki_add","attempts":3,"classification":"permanent","last_error":"400 Bad Request: ..."}
```

A host-side bridge (lives in the user's nix repo, **not** in this image — keeps the container dependency-free, no dbus, no notify libraries) watches that file via a systemd path unit and fires `notify-send "Yadgar DLQ" "<op_type> moved to DLQ after <attempts> attempts: <error>"` on each new line.

Yadgar specifies the event-log format; the nix repo owns the desktop side.

### 5. Backup cron schedule change

Current cadence (visible in `yadgar-backend` logs as `Backup written: /data/backup_<TS>.surql`): every 6 hours at HH:10 — `06:10, 12:10, 18:10, 00:10`. The midnight run wakes the laptop now that auto-suspend is disabled.

Change: replace `00:10` with `21:10`. Final schedule: `06:10, 12:10, 18:10, 21:10` (irregular: 9 h overnight gap, 6 h otherwise).

Implementation: locate the schedule in `entrypoint-backend.sh` or `Dockerfile.backend` (grep for `cron`, `backup`, `*/6`). If the schedule is a Python scheduler inside the backend service rather than system cron, change the schedule constant accordingly. If practical, expose it through `yadgar/config.py` as `BACKUP_HOURS_LOCAL` with default `[6, 12, 18, 21]`.

## Files to modify

| File | Change |
|------|--------|
| `yadgar/file_queue.py` | Attempt tracker, classify/backoff/DLQ logic, `dlq/` dir setup, cleanup, append to `.events.log` |
| `yadgar/config.py` | New env-var/YAML config fields (queue thresholds + backup hours) |
| `yadgar/server.py` | `wiki_add` SQL escape OR param fix; new `dlq_inspect`/`dlq_requeue` tools; extend `prompt-recall` response with `dlq_alerts` |
| `yadgar/wiki.py` (or wherever `_wiki.add` / `_wiki.ingest` live) | Actual SurrealQL builder fix |
| `entrypoint-backend.sh` and/or `Dockerfile.backend` | Backup schedule `00:10` → `21:10` |
| `tests/test_file_queue.py` | DLQ + backoff + retry classification + event-log tests |
| `tests/test_wiki_sql_escape.py` | Regression test using Route53 glossary fixture |
| `tests/test_dlq_alerts.py` | `prompt-recall` includes `dlq_alerts` when DLQ non-empty |
| `pyproject.toml` | Bump version `4.2.4` → `4.3.0` |

Companion change in the user's nix repo (separate commit, **not** in this PR): a systemd path unit watching `~/.yadgar/dlq/.events.log` plus a small bridge script that tails new lines and fires `notify-send`.

## Reused existing code

- `FileQueue.enqueue` (`file_queue.py:73`) — atomic tmp+rename pattern; reuse for sidecar writes and queue→dlq moves.
- `_json_default` (`file_queue.py:43`) — JSON serializer fallback for the error sidecar.
- `is_draining` (`file_queue.py:38`) — no change; the thread-local guard still protects against re-enqueueing during replay.
- The existing "archive cleanup every ~120 passes" branch inside `_drain_once` — extend rather than duplicate.

## Verification

1. **Unit:** mock `_apply()` to raise `httpx.HTTPStatusError(400)`; run N+1 drain passes; assert file ends up in `dlq/` with sidecar at attempt N. Repeat with 503 and assert it survives N>3.
2. **Unit:** assert backoff math — file re-attempted after 30 s, 60 s, 120 s, … capped at 3600 s.
3. **Integration:** load the two real stuck-file payloads as fixtures. Pre-fix: assert backend returns 400. Post-fix: assert ingest succeeds, page is queryable via `wiki_read`.
4. **E2E smoke:** `podman compose up`, `memorize` something, observe round-trip queue → archive in <1 drain interval. Force a poison-pill (insert a hand-crafted bad payload) and watch it land in `dlq/` after 3 attempts.
5. **Recovery:** re-run the two currently-stuck wiki_adds via `dlq_requeue`; verify pages exist via `wiki_read`; confirm SurrealDB CPU returns to ~0% at idle.
6. **Notification — Claude side:** with at least one DLQ entry, hit `/hooks/prompt-recall` and assert response includes `dlq_alerts`. Eyeball that the next Claude session surfaces the alert in conversation.
7. **Notification — user side:** force a poison-pill into the queue, wait for DLQ move, confirm desktop `notify-send` fires (after the nix-repo bridge is in place) and that `~/.yadgar/dlq/.events.log` is appended to.
8. **Backup cron:** observe `Backup written` log lines after deploy; confirm next day's cycle runs at `06:10, 12:10, 18:10, 21:10` and skips `00:10`.

## Recovery actions before this release ships

The two stuck files can be quarantined today without waiting for the implementation:

```bash
mkdir -p ~/.yadgar/dlq
mv ~/.yadgar/queue/0001778139482800_*.json \
   ~/.yadgar/queue/0001778174454315_*.json \
   ~/.yadgar/dlq/
podman restart yadgar yadgar-backend
```

This stops the CPU spin immediately. After 4.3.0 ships and the SQL bug is fixed, `dlq_requeue` recovers the data.

## Out of scope (follow-ups)

- DLQ web UI / dashboard
- Auto-requeue on schema migration
- Per-op-type retry policy — different thresholds per `op_type`. Today everything shares one policy; once we have actual DLQ data showing certain op types misbehave more than others, revisit.
- Re-enabling KDE auto-suspend at night — a nix-repo concern, not yadgar
