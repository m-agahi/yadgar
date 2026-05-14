# Migration Notes

## v4.8 backup retention

Replace the old "keep 7 newest by mtime" policy with three-cap retention
(age + count + size). The nix module edit is staged in `/home/max/git/nix`
but not applied — the user runs `nix-update`.

### Steps

**1. One-shot manual cleanup of the 28 GB stuck pre-rebuild snapshots:**

~~~bash
find ~/.backups/yadgar/db -maxdepth 1 -name 'surreal_db_*' -type d -mtime +1 -size +1G -exec rm -rf {} +
~~~

This removes any snapshot older than 1 day that is larger than 1 GB —
targeting the five 5.5 GB pre-rebuild bloated snapshots from 2026-05-12
while leaving the two post-rebuild snapshots (137 MB, 479 MB) untouched.

**2. Apply the nix module edit to install the new retention logic in
`yadgar-backend.service:ExecStartPre`:**

~~~bash
home-manager switch
~~~

The edit is at `/home/max/git/nix/modules/home/yadgar.nix`. It:
- Adds `options.programs.yadgar.backup.{maxAgeDays,maxCount,maxGiB}` with defaults 7/7/10.
- Replaces the `tail -n +8 | xargs rm -rf` retention with the three-cap
  logic (age → count → size). Values are baked into the unit at `home-manager switch` time.

To override defaults, add to your home configuration before switching:

~~~nix
programs.yadgar.backup = {
  maxAgeDays = 14;   # keep up to 14 days
  maxCount   = 5;    # keep at most 5 snapshots
  maxGiB     = 20;   # allow up to 20 GiB
};
~~~

**3. Verify on next service restart:**

~~~bash
journalctl --user -u yadgar-backend.service -b | grep -i 'snapshot\|cleanup'
~~~

### Helper script

`scripts/cleanup-backups.sh` in this repo is the single source of truth for
the retention logic. It accepts env vars `YADGAR_BACKUP_DIR`,
`YADGAR_BACKUP_MAX_AGE_DAYS`, `YADGAR_BACKUP_MAX_COUNT`, `YADGAR_BACKUP_MAX_GIB`
and a `--dry-run` flag. Callable with a different `YADGAR_BACKUP_DIR` so the
new `cmd_vacuum` reuses it for pre-vacuum snapshot retention.

---

## v4.8 weekly SurrealKV vacuum

A new `yadgar-vacuum.service` + `.timer` runs the rewritten `yadgar vacuum`
on the proven export → swap → reimport flow every Sunday at 04:00 local
time (with a 30-minute random delay).

### Steps

**1. Apply the nix module edit:**

~~~bash
home-manager switch
~~~

This installs `yadgar-vacuum.service` (oneshot) + `yadgar-vacuum.timer`
(weekly). The vacuum binary stops/starts both `yadgar` and `yadgar-backend`
itself, so the timer unit does not declare `Conflicts=` or `PartOf=` on
the daemons.

**2. (Optional) Trigger a one-off vacuum on the current bloated DB:**

~~~bash
systemctl --user start yadgar-vacuum.service
journalctl --user -u yadgar-vacuum.service -f
~~~

Expected log lines: `Phase 1: export...`, `Phase 2: snapshot + drop...`,
`Phase 3: restart + import...`, `Phase 4: finalize...`, then
`Vacuum complete. before=N MB after=M MB saved=N-M (X%)`.

**3. Verify timer schedule:**

~~~bash
systemctl --user list-timers yadgar-vacuum.timer
~~~

**4. Rollback (if reimport fails):**

The bloated dir is retained at `~/.yadgar/surreal_db.bloated-<ts>` and
`yadgar` is NOT restarted. To recover the old state:

~~~bash
systemctl --user stop yadgar yadgar-backend
mv ~/.yadgar/surreal_db.pre-vacuum-<ts> ~/.yadgar/surreal_db
systemctl --user start yadgar-backend yadgar
~~~

**5. Disable the timer entirely:**

~~~bash
systemctl --user disable --now yadgar-vacuum.timer
~~~

---

## v4.8 log-level config (from v4.7.0, repeated for reference)

After deploying v4.8, the `[ml]` extra is now bundled in the core image
so the NLI reranker (`sentence_transformers`) works out of the box. No
action required — the WARN `Reranker disabled: install yadgar[ml] to
enable` should no longer appear in logs.

---

## v4.8 consolidation cooldown

`CONSOLIDATION_COOLDOWN_SECONDS` defaults to `1800` (30 min). Idle-triggered
consolidation cycles fire at most once per cooldown window. The daily 18:30
UTC cycle and `force_consolidate()` ignore the cooldown.

To override, edit `~/.yadgar/config.yaml`:

~~~yaml
CONSOLIDATION_COOLDOWN_SECONDS: 3600   # 1 hour instead of 30 min
# CONSOLIDATION_COOLDOWN_SECONDS: 0    # restore legacy back-to-back behaviour
~~~

Then restart: `systemctl --user restart yadgar`.
