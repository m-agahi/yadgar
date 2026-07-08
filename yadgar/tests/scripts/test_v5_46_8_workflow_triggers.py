"""
TDD scaffolding — v5.46.8 workflow trigger gate assertions.

Updated in v5.58 paydown-A for v5.57 CI refactor (ci.yaml→ci-pr.yaml,
release.yaml→ci-release.yaml, validate.yml→validate.yaml):

New design:
1. ci-pr.yaml   on: pull_request only (no push, no workflow_dispatch)
2. ci-pr.yaml   NO 'build' job (build/publish moved to ci-release.yaml)
3. ci-release.yaml  on: push:[master] + workflow_dispatch (NO on.push.tags)
4. ci-release.yaml  jobs gated by needs.changes.outputs.release == 'true'
   (version-bump detection replaces old workflow_dispatch gate)
5. validate.yaml  on: pull_request (renamed from validate.yml)

Dropped assertions (noted inline with reason):
- ci.yaml on.push.branches test → ci-pr.yaml has NO on.push (PR-only now)
- ci.yaml on.workflow_dispatch test → ci-pr.yaml has NO workflow_dispatch
- ci.yaml build job gate test → build job removed from ci-pr.yaml entirely
- ci.yaml header sentinel → neither file has GATED header (dev gate removed)
- release.yaml on.push.tags test → ci-release.yaml has NO tag trigger
- release.yaml jobs gated to workflow_dispatch → replaced by changes.release output
- release.yaml header sentinel → header sentinel removed in v5.57 redesign
- attach-to-release job → renamed to tag-and-release

Per PD-45 (2026-06-06): internal dev workflow vs production CI separation.
Superseded by v5.57 design: always-on per-PR CI + version-bump-gated release.
"""

from pathlib import Path

import yaml

from yadgar.tests._paths import REPO_ROOT

CI_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-pr.yaml"
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci-release.yaml"
VALIDATE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "validate.yaml"

# Jobs in ci-release.yaml that must be gated by the changes.release output
RELEASE_GATED_JOBS = [
    "build-wheel",
    "build-sbom",
    "publish-pypi",
    "tag-and-release",
    "build-images",
]
EXPECTED_RELEASE_GATE = "needs.changes.outputs.release == 'true'"


def _load_yaml(path: Path) -> dict:
    """Load YAML, handle pyyaml's on: -> True key quirk."""
    return yaml.safe_load(path.read_text())


def _on_block(data: dict) -> dict:
    """Return the triggers block (keyed as True in pyyaml, 'on' in BaseLoader)."""
    return data.get(True, data.get("on", {}))


# ── ci-pr.yaml assertions ─────────────────────────────────────────────────────


class TestCiPrYamlTriggers:
    def test_ci_pr_yaml_exists(self):
        assert CI_YAML.exists(), f"Missing: {CI_YAML}"

    def test_on_pull_request_present(self):
        """pull_request trigger must be present in ci-pr.yaml."""
        data = _load_yaml(CI_YAML)
        on_block = _on_block(data)
        assert "pull_request" in on_block, "ci-pr.yaml missing pull_request trigger"

    def test_on_push_tags_absent(self):
        """ci-pr.yaml must NOT have on.push.tags — tag pushes do not fire PR checks.

        Dropped from old ci.yaml: on.push.branches assertion.
        Reason: ci-pr.yaml is pull_request-only; it has no on.push block at all.
        """
        data = _load_yaml(CI_YAML)
        on_block = _on_block(data)
        push_block = on_block.get("push", {}) or {}
        assert "tags" not in push_block, (
            f"ci-pr.yaml on.push still has 'tags' key: {push_block}. "
            "Remove it — tag pushes must not fire PR checks."
        )

    def test_no_build_job_in_ci_pr(self):
        """ci-pr.yaml must NOT have a 'build' job (build moved to ci-release.yaml).

        Dropped: build job if-gate assertion.
        Reason: build job was removed from ci-pr.yaml in v5.57 refactor.
        """
        data = _load_yaml(CI_YAML)
        jobs = data.get("jobs", {})
        assert "build" not in jobs, (
            f"ci-pr.yaml still has 'build' job — it must be removed (lives in ci-release.yaml now). "
            f"Present jobs: {list(jobs)}"
        )

    def test_subsystem_test_jobs_present_in_ci_pr(self):
        """R3 CI regroup: ci-pr.yaml must have all four subsystem test jobs + test-gate.

        Migrated from test_test_job_present_in_ci_pr (old monolithic 'test' job
        replaced by test-fast, test-shared, test-backend, test-core in R3).
        """
        data = _load_yaml(CI_YAML)
        jobs = data.get("jobs", {})
        required_jobs = ["test-fast", "test-shared", "test-backend", "test-core", "test-gate"]
        for job_id in required_jobs:
            assert job_id in jobs, (
                f"ci-pr.yaml missing required job '{job_id}'; present: {list(jobs)}"
            )
        assert "test" not in jobs, (
            f"ci-pr.yaml still has old monolithic 'test' job — must be removed (R3 regroup). "
            f"Present jobs: {list(jobs)}"
        )

    def test_ci_pr_yaml_parses_valid(self):
        """ci-pr.yaml must be valid YAML."""
        data = _load_yaml(CI_YAML)
        assert isinstance(data, dict)
        assert "jobs" in data


# ── ci-release.yaml assertions ────────────────────────────────────────────────


class TestCiReleaseYamlTriggers:
    def test_ci_release_yaml_exists(self):
        assert RELEASE_YAML.exists(), f"Missing: {RELEASE_YAML}"

    def test_on_push_master_present(self):
        """ci-release.yaml must trigger on push to master.

        Dropped: on.push.tags assertion.
        Reason: v5.57 removed the tag-push trigger entirely; release is now
        driven by version-bump detection in the 'changes' job.
        """
        data = _load_yaml(RELEASE_YAML)
        on_block = _on_block(data)
        push_branches = on_block.get("push", {}).get("branches", [])
        assert "master" in push_branches, (
            f"ci-release.yaml on.push.branches must include 'master'; got: {push_branches}"
        )

    def test_on_push_no_tags(self):
        """ci-release.yaml must NOT have on.push.tags (tag trigger removed in v5.57)."""
        data = _load_yaml(RELEASE_YAML)
        on_block = _on_block(data)
        push_tags = on_block.get("push", {}).get("tags", [])
        assert not push_tags, (
            f"ci-release.yaml on.push.tags must be absent (tag trigger removed in v5.57); "
            f"got: {push_tags}"
        )

    def test_on_workflow_dispatch_present(self):
        """workflow_dispatch trigger must be present in ci-release.yaml (manual fallback)."""
        data = _load_yaml(RELEASE_YAML)
        on_block = _on_block(data)
        assert "workflow_dispatch" in on_block, "ci-release.yaml missing workflow_dispatch trigger"

    def test_changes_job_present(self):
        """ci-release.yaml must have a 'changes' job (version-bump detection)."""
        data = _load_yaml(RELEASE_YAML)
        jobs = data.get("jobs", {})
        assert "changes" in jobs, (
            f"ci-release.yaml must have 'changes' job (version-bump detection); "
            f"present: {list(jobs)}"
        )

    def test_release_gated_jobs_present(self):
        """All key release jobs must exist in ci-release.yaml."""
        data = _load_yaml(RELEASE_YAML)
        jobs = data.get("jobs", {})
        missing = [j for j in RELEASE_GATED_JOBS if j not in jobs]
        assert not missing, (
            f"ci-release.yaml missing expected jobs: {missing}; present: {list(jobs)}"
        )

    def test_release_gated_jobs_use_changes_output(self):
        """Release jobs must be gated by needs.changes.outputs.release == 'true'.

        Dropped: jobs gated to workflow_dispatch exactly.
        Reason: v5.57 replaced per-job workflow_dispatch gates with the 'changes'
        job output so all release jobs share the same version-bump-detection gate.

        Dropped: attach-to-release job check.
        Reason: renamed to tag-and-release in v5.57.
        """
        data = _load_yaml(RELEASE_YAML)
        jobs = data["jobs"]
        failures = []
        for job_name in RELEASE_GATED_JOBS:
            if job_name not in jobs:
                failures.append(f"  {job_name}: job not found")
                continue
            job_if = str(jobs[job_name].get("if", ""))
            if "release" not in job_if:
                failures.append(
                    f"  {job_name}: if={job_if.strip()!r} — must reference 'release' output"
                )
        assert not failures, (
            "ci-release.yaml job gate mismatches (must reference changes.outputs.release):\n"
            + "\n".join(failures)
        )

    def test_ci_release_yaml_parses_valid(self):
        """ci-release.yaml must be valid YAML."""
        data = _load_yaml(RELEASE_YAML)
        assert isinstance(data, dict)
        assert "jobs" in data


# ── validate.yaml assertions ──────────────────────────────────────────────────


class TestValidateYamlTriggers:
    def test_validate_yaml_exists(self):
        """validate.yaml must exist (renamed from validate.yml in v5.57)."""
        assert VALIDATE_YAML.exists(), f"Missing: {VALIDATE_YAML}"

    def test_validate_on_pull_request_present(self):
        """validate.yaml must trigger on pull_request."""
        data = _load_yaml(VALIDATE_YAML)
        on_block = _on_block(data)
        assert "pull_request" in on_block, "validate.yaml missing pull_request trigger"

    def test_validate_yaml_parses_valid(self):
        """validate.yaml must be valid YAML."""
        data = _load_yaml(VALIDATE_YAML)
        assert isinstance(data, dict)
        assert "jobs" in data
