<!-- YADGAR REPO-WIKI-REFRESH PROTOCOL
     Substitute these placeholders throughout this file before following instructions:
       {directory}      = your current working directory (absolute path; the project root)
       {project}        = basename of {directory}
       {default_branch} = last segment of `git -C {directory} symbolic-ref refs/remotes/origin/HEAD`;
                          fall back to "master" for non-git projects or on any git error.
-->

Yadgar repo-wiki-refresh maintenance. This keeps the project's code-structure
wiki (the `{project}-mod-*` pages: AST signatures + docstrings, one per
importable module) in sync with the source on disk. It is a DUMB PIPE — the
host CLI `yadgar repo-wiki` does the scanning/hashing; YOU wire its output into
the validated `wiki_add` write path. This is NOT the checkpoint and NOT the
anchor-audit; do NOT run capture or anchor steps here.

READ FOR REAL: every "call X" below means CALL the named tool THIS turn and act
on the CONTENT IT RETURNS. Do not reconstruct wiki/memory state from an earlier
turn.

1. CHECK EXISTENCE (branch on per-project state).
   Call wiki_read("{project}-repo-wiki-index", directory="{directory}",
   branch_hint="{default_branch}").
   - Page PRESENT → repo-wiki is ENABLED for this project. Go to step 2 (refresh).
   - Page ABSENT → repo-wiki is UNSET (or was opted out). Go to step 3 (opt-out /
     ASK). Never bulk-generate silently — no surprise ~N-module runs.

2. ENABLED → REFRESH (silent when nothing drifted; NEVER asks the user).
   a. Build the stored-hash baseline. Call wiki_list(directory="{directory}") and
      construct a `{slug: hash}` map from each page's `hash` field (the
      `{project}-mod-*` code pages carry it; skip pages with no hash).
   b. Run the host stale-diff (Bash), piping the baseline on stdin:
        echo '<{slug: hash} JSON>' | yadgar repo-wiki {directory} --stale-only --stored-hashes - --json
      The CLI generates every page host-side and emits ONLY drifted/new module
      pages. Output JSON shape:
        {"stale_only": true, "pages": [<drifted/new module pages>],
         "deleted": [<slugs>], "toc_stale": <bool>, "total": N,
         "directory_context": "{directory}"}
   c. If `pages` AND `deleted` are BOTH empty → nothing drifted. Say so briefly
      ("repo-wiki up to date") and continue the conversation. This is the common
      case: a silent no-op.
   d. Otherwise WRITE BACK the drift using the single upsert form:
      - For each page in `pages` (module pages, page_type="repo_wiki"):
          wiki_add(page_type="repo_wiki", upsert=True, slug=<page.slug>,
            content=<page.content>, title=<page.title>, tags=<page.tags>,
            hash=<page.hash>, source_file=<page.source_file>,
            directory_context="{directory}", branch_hint="{default_branch}", wait=True)
        `upsert=True` + explicit `slug=` creates-or-overwrites at the caller-supplied
        slug with no title-slug fallback and no similarity-gate bypass needed
        (page_type="repo_wiki" routes through the identity gate, not the sim gate).
      - For each slug in `deleted` → wiki_delete(<slug>).
      - If `toc_stale` is True → the module SET changed; rewrite the
        `{project}-repo-wiki-index` page. Re-run WITHOUT --stale-only to get the
        full output:  yadgar repo-wiki {directory} --json  and extract the TOC page
        (slug == "{project}-repo-wiki-index"), then:
          wiki_add(upsert=True, slug="{project}-repo-wiki-index",
            content=<toc.content>, title=<toc.title>, tags=<toc.tags>,
            directory_context="{directory}", branch_hint="{default_branch}", wait=True)
   e. Report which pages you refreshed / deleted, then continue.

3. UNSET → CHECK OPT-OUT, ELSE ASK (record the decision so the cadence never nags).
   a. Check the per-project opt-out marker FIRST. Call
      recall("repo-wiki opt-out disabled", directory="{directory}",
        tags=["repo-wiki-optout"], type="memory", max_results=3).
      If a matching opt-out marker exists → this project opted out. NO-OP: do
      nothing, do not ASK, and continue the conversation. Skip silently. This is
      the no-nag guarantee — a NO answer is recorded once and honoured forever.
   b. If NO opt-out marker → ASK the user exactly once:
        "No code-structure wiki for {project}. Want an initial full run (~N
         modules, generated in a background agent)?
           yes → I'll generate + ingest all the {project}-mod-* pages + the
                 {project}-repo-wiki-index TOC, and create a pointer-anchor.
           no  → I'll disable repo-wiki for this project (I won't ask again)."
      (Estimate N via a quick `yadgar repo-wiki {directory} --json` count or just
      say "all importable modules" if unsure.)
   c. On NO → record the opt-out marker so the cadence never re-asks:
        memorize(content="repo-wiki disabled for {project} — user opted out of the
          code-structure wiki. Do NOT re-ask or auto-generate {project}-mod-* pages here.",
          context="{directory}", tags=["repo-wiki-optout"], is_protected=True,
          branch_hint="{default_branch}")
      is_protected=True keeps the marker from decaying out (a decayed marker would
      let the ASK return — that is the nag we are preventing). Then continue.
   d. On YES → dispatch a BACKGROUND AGENT to do the (potentially large) bulk
      generate + ingest off the critical path, so this session never blocks:
      - The agent runs `yadgar repo-wiki {directory} --json` (full generation,
        every page hash-stamped) and, for EACH module page (page_type="repo_wiki"):
          wiki_add(page_type="repo_wiki", upsert=True, slug=<page.slug>,
            content=<page.content>, title=<page.title>, tags=<page.tags>,
            hash=<page.hash>, source_file=<page.source_file>,
            directory_context="{directory}", branch_hint="{default_branch}", wait=True)
        And for the TOC page (slug == "{project}-repo-wiki-index"):
          wiki_add(upsert=True, slug="{project}-repo-wiki-index",
            content=<toc.content>, title=<toc.title>, tags=<toc.tags>,
            directory_context="{directory}", branch_hint="{default_branch}", wait=True)
        wait=True keeps ≤1 item in flight (schema-safe: an upgrade can't catch a
        large backlog).
      - After ingest, the agent creates ONE project pointer-anchor:
        memorize(content="code-structure reference = {project}-mod-* wiki pages (AST
          signatures + docstrings, auto-refreshed); start at
          [[{project}-repo-wiki-index]]; consult before grepping for 'where does X
          live'.", context="{directory}", tags=["_anchor"], is_protected=True,
          branch_hint="{default_branch}")
      - The TOC page's existence is itself the ENABLED marker — next cadence fire,
        step 1 finds it and takes the refresh branch. No separate "enabled" marker
        is needed.
      Report that the background regen was dispatched, then continue.

WRAP UP. Whatever branch you took, report the outcome in one line (refreshed /
up-to-date / opt-out recorded / bulk regen dispatched) and continue the
conversation — if you were mid-thought, repeat your last question so the flow
resumes.
