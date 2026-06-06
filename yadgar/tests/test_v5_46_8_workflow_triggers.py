"""
TDD scaffolding — v5.46.8 workflow trigger gate assertions.

Verifies:
1. ci.yaml  `on.push.tags` NOT present (only master branch + PR + workflow_dispatch fire)
2. ci.yaml  `build` job if-gate == `workflow_dispatch` only
3. release.yaml ALL 4 jobs (build-wheel, build-sbom, attach-to-release, publish-pypi)
   have `if: github.event_name == 'workflow_dispatch'` (exact value)
4. Header comment block present in both workflows (text-grep — yaml parser strips comments)

Per PD-45 (2026-06-06): internal dev workflow vs production CI separation.
Anchors 490140 (2026-05-18) + 491179 (2026-05-19) codified.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YAML = REPO_ROOT / ".forgejo" / "workflows" / "ci.yaml"
RELEASE_YAML = REPO_ROOT / ".forgejo" / "workflows" / "release.yaml"

EXPECTED_JOB_GATE = "github.event_name == 'workflow_dispatch'"
HEADER_SENTINEL = "WORKFLOW STATE: GATED FOR INTERNAL DEV"

# Number of lines from top to search for header (before 'name:')
HEADER_SEARCH_LINES = 20


def _load_yaml(path: Path) -> dict:
    """Load YAML, handle pyyaml's on: -> True key quirk."""
    return yaml.safe_load(path.read_text())


def _on_block(data: dict) -> dict:
    """Return the triggers block (keyed as True in pyyaml, 'on' in BaseLoader)."""
    # pyyaml parses 'on:' as Python True
    return data.get(True, data.get("on", {}))


def _header_present(path: Path) -> bool:
    lines = path.read_text().splitlines()
    return any(HEADER_SENTINEL in line for line in lines[:HEADER_SEARCH_LINES])


# ── ci.yaml assertions ────────────────────────────────────────────────────────


class TestCiYamlTriggers:
    def test_ci_yaml_exists(self):
        assert CI_YAML.exists(), f"Missing: {CI_YAML}"

    def test_on_push_has_no_tags(self):
        """tags: key must be absent from on.push — no tag-push triggers in ci.yaml."""
        data = _load_yaml(CI_YAML)
        on_block = _on_block(data)
        push_block = on_block.get("push", {})
        assert "tags" not in push_block, (
            f"ci.yaml on.push still has 'tags' key: {push_block}. "
            "Remove it — tag pushes must not fire any CI jobs."
        )

    def test_on_push_retains_branches_master(self):
        """on.push.branches: [master] must still be present."""
        data = _load_yaml(CI_YAML)
        on_block = _on_block(data)
        push_branches = on_block.get("push", {}).get("branches", [])
        assert "master" in push_branches, f"ci.yaml on.push.branches lost 'master': {push_branches}"

    def test_on_pull_request_present(self):
        """pull_request trigger must still exist."""
        data = _load_yaml(CI_YAML)
        on_block = _on_block(data)
        assert "pull_request" in on_block, "ci.yaml missing pull_request trigger"

    def test_on_workflow_dispatch_present(self):
        """workflow_dispatch trigger must be present."""
        data = _load_yaml(CI_YAML)
        on_block = _on_block(data)
        assert "workflow_dispatch" in on_block, "ci.yaml missing workflow_dispatch trigger"

    def test_build_job_gated_to_workflow_dispatch_only(self):
        """build job if: must equal exactly 'github.event_name == workflow_dispatch'."""
        data = _load_yaml(CI_YAML)
        build_if = data["jobs"]["build"].get("if")
        assert build_if is not None, "ci.yaml build job has no 'if:' gate"
        assert build_if.strip() == EXPECTED_JOB_GATE, (
            f"ci.yaml build job if-gate mismatch.\n"
            f"  Expected: {EXPECTED_JOB_GATE!r}\n"
            f"  Got:      {build_if.strip()!r}\n"
            "Gate must be workflow_dispatch-only (drop the push OR clause)."
        )

    def test_ci_yaml_header_comment_present(self):
        """Header sentinel must appear in first 20 lines of ci.yaml."""
        assert _header_present(CI_YAML), (
            f"ci.yaml missing header sentinel {HEADER_SENTINEL!r} in first "
            f"{HEADER_SEARCH_LINES} lines. Add the gate-state comment block."
        )

    def test_ci_yaml_parses_valid(self):
        """ci.yaml must be valid YAML after changes."""
        data = _load_yaml(CI_YAML)
        assert isinstance(data, dict)
        assert "jobs" in data


# ── release.yaml assertions ───────────────────────────────────────────────────


class TestReleaseYamlTriggers:
    GATED_JOBS = ["build-wheel", "build-sbom", "attach-to-release", "publish-pypi"]

    def test_release_yaml_exists(self):
        assert RELEASE_YAML.exists(), f"Missing: {RELEASE_YAML}"

    def test_release_on_push_tags_still_present(self):
        """release.yaml keeps on.push.tags — production handoff trigger stays."""
        data = _load_yaml(RELEASE_YAML)
        on_block = _on_block(data)
        push_tags = on_block.get("push", {}).get("tags", [])
        assert push_tags, (
            "release.yaml on.push.tags removed — keep it. "
            "Jobs are gated individually; trigger subscription stays."
        )

    def test_release_on_workflow_dispatch_present(self):
        """workflow_dispatch trigger must be present in release.yaml."""
        data = _load_yaml(RELEASE_YAML)
        on_block = _on_block(data)
        assert "workflow_dispatch" in on_block, "release.yaml missing workflow_dispatch trigger"

    def test_all_release_jobs_gated_to_workflow_dispatch(self):
        """All 4 release jobs must have if: github.event_name == 'workflow_dispatch'."""
        data = _load_yaml(RELEASE_YAML)
        jobs = data["jobs"]
        failures = []
        for job_name in self.GATED_JOBS:
            assert job_name in jobs, f"release.yaml missing expected job: {job_name}"
            job_if = jobs[job_name].get("if")
            if job_if is None:
                failures.append(f"  {job_name}: no 'if:' gate present")
            elif job_if.strip() != EXPECTED_JOB_GATE:
                failures.append(
                    f"  {job_name}: if={job_if.strip()!r} (expected {EXPECTED_JOB_GATE!r})"
                )
        assert not failures, "release.yaml job gate mismatches:\n" + "\n".join(failures)

    def test_release_yaml_header_comment_present(self):
        """Header sentinel must appear in first 20 lines of release.yaml."""
        assert _header_present(RELEASE_YAML), (
            f"release.yaml missing header sentinel {HEADER_SENTINEL!r} in first "
            f"{HEADER_SEARCH_LINES} lines. Add the gate-state comment block."
        )

    def test_release_yaml_parses_valid(self):
        """release.yaml must be valid YAML after changes."""
        data = _load_yaml(RELEASE_YAML)
        assert isinstance(data, dict)
        assert "jobs" in data
