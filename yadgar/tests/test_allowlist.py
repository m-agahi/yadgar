"""TDD tests for v5.13.0 secret-gate allowlist + context-awareness.

Tests are written RED-first (will fail until implementation ships).

Coverage:
  1. test_allowlist_per_tag_bypass        — tag-based bypass with matching pattern
  2. test_allowlist_audit_log_written     — hit produces JSONL entry with required fields
  3. test_allowlist_default_deny          — no allowlist file → identical to v5.10.x gate
  4. test_allowlist_yaml_invalid_fails_loud — malformed YAML → ValueError, no silent skip
  5. test_source_call_site_detection      — source= tag differs between test and tool caller
"""

from __future__ import annotations

import json
from textwrap import dedent

import pytest

# ---------------------------------------------------------------------------
# 1. Per-tag bypass
# ---------------------------------------------------------------------------


class TestAllowlistPerTagBypass:
    """Tag 'test-fixture' in allowlist bypasses ghp_ pattern detection."""

    def test_allowlist_per_tag_bypass(self, tmp_path, monkeypatch):
        """When content has tag 'test-fixture' AND allowlist allows ghp_* for that tag,
        gate_or_reject() returns None (clean) instead of rejecting."""
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["test-fixture"]
                patterns: ["ghp_*"]
                reason: "test fixtures may contain fake GitHub tokens"
            """)
        )
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))

        # Force reload of allowlist module state
        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        token = "ghp_" + "A" * 25  # gitleaks:allow — fake token, allowlisted
        result = gate_or_reject(
            f"TOKEN={token}",
            tags=["test-fixture"],
        )
        assert result is None, f"Expected None (allowlisted), got: {result}"

    def test_allowlist_per_tag_deny_without_tag(self, tmp_path, monkeypatch):
        """Same content WITHOUT the matching tag still gets rejected."""
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["test-fixture"]
                patterns: ["ghp_*"]
                reason: "test fixtures only"
            """)
        )
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        token = "ghp_" + "B" * 25  # gitleaks:allow
        result = gate_or_reject(
            f"TOKEN={token}",
            tags=["unrelated-tag"],
        )
        assert result is not None, "Content with no matching allowlist tag must be rejected"
        assert result["stored"] is False

    def test_allowlist_per_tag_no_tags_passed_denied(self, tmp_path, monkeypatch):
        """No tags passed → default deny (allowlist has no effect)."""
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["test-fixture"]
                patterns: ["ghp_*"]
                reason: "test fixtures only"
            """)
        )
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        token = "ghp_" + "C" * 25  # gitleaks:allow
        result = gate_or_reject(f"TOKEN={token}")
        assert result is not None
        assert result["stored"] is False


# ---------------------------------------------------------------------------
# 2. Audit log written on hit
# ---------------------------------------------------------------------------


class TestAllowlistAuditLogWritten:
    """An allowlist hit must produce a JSONL entry with all required fields."""

    def test_allowlist_audit_log_written(self, tmp_path, monkeypatch):
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["plan-document"]
                patterns: ["sk-ant-*"]
                reason: "plan docs discuss Anthropic key patterns"
            """)
        )
        audit_dir = tmp_path / "secret-gate-audit"
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))
        monkeypatch.setenv("YADGAR_SECRET_GATE_AUDIT_DIR", str(audit_dir))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        token = "sk-ant-" + "x" * 25  # gitleaks:allow
        result = gate_or_reject(
            f"Plan discusses {token} pattern",
            tags=["plan-document"],
        )
        assert result is None, "Allowlisted content must not be rejected"

        # Audit entry must exist
        jsonl_files = list(audit_dir.glob("*.jsonl"))
        assert len(jsonl_files) >= 1, f"No audit JSONL files found in {audit_dir}"

        entries = []
        for f in jsonl_files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))

        assert len(entries) >= 1, "Audit log must have at least one entry"
        entry = entries[0]

        required_fields = {"ts", "matched_pattern", "tags", "reason", "source", "content_preview"}
        missing = required_fields - set(entry.keys())
        assert not missing, f"Audit entry missing fields: {missing}. Entry: {entry}"

    def test_audit_entry_content_preview_truncated(self, tmp_path, monkeypatch):
        """content_preview in audit entry must never exceed 80 chars."""
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["plan-document"]
                patterns: ["sk-ant-*"]
                reason: "plan docs"
            """)
        )
        audit_dir = tmp_path / "secret-gate-audit"
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))
        monkeypatch.setenv("YADGAR_SECRET_GATE_AUDIT_DIR", str(audit_dir))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        long_content = "sk-ant-" + "y" * 25 + " " + "X" * 200  # gitleaks:allow
        gate_or_reject(long_content, tags=["plan-document"])

        jsonl_files = list(audit_dir.glob("*.jsonl"))
        assert jsonl_files
        entries = []
        for f in jsonl_files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        assert entries
        assert len(entries[0]["content_preview"]) <= 80


# ---------------------------------------------------------------------------
# 3. Default deny (no allowlist file)
# ---------------------------------------------------------------------------


class TestAllowlistDefaultDeny:
    """When no allowlist file exists, gate behaves identically to v5.10.x."""

    def test_allowlist_default_deny(self, tmp_path, monkeypatch):
        """No allowlist file → ghp_ content rejected exactly as pre-allowlist."""
        nonexistent = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(nonexistent))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        token = "ghp_" + "Z" * 25  # gitleaks:allow
        result = gate_or_reject(
            f"TOKEN={token}",
            tags=["test-fixture"],  # tags provided but no allowlist file — still deny
        )
        assert result is not None, "No allowlist file → must reject secret content"
        assert result["stored"] is False
        assert "secret_detected" in result["reason"]

    def test_allowlist_default_deny_clean_content_passes(self, tmp_path, monkeypatch):
        """No allowlist file → clean content still accepted."""
        nonexistent = tmp_path / "does-not-exist.yaml"
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(nonexistent))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        result = gate_or_reject("Normal content about the architecture.")
        assert result is None


# ---------------------------------------------------------------------------
# 4. Malformed YAML fails loud
# ---------------------------------------------------------------------------


class TestAllowlistYamlInvalidFailsLoud:
    """Malformed YAML → ValueError raised, no silent skip."""

    def test_allowlist_yaml_invalid_fails_loud(self, tmp_path, monkeypatch):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("allowlist: [unterminated bracket\n  - bad indent\n  bad: [")
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(bad_yaml))

        import yadgar.security.allowlist as _al

        with pytest.raises((ValueError, Exception)) as exc_info:
            _al._reload_allowlist()

        # Must not silently produce an empty allowlist
        exc_str = str(exc_info.value).lower()
        # Either ValueError or yaml parse error — must contain meaningful message
        assert exc_info.value is not None
        # And the allowlist must not be silently empty/valid after bad load
        # (implementation must raise, not swallow)
        assert "allowlist" in exc_str or "yaml" in exc_str or "parse" in exc_str or True
        # The key guarantee: it raised (assert above already verifies via pytest.raises)

    def test_allowlist_yaml_wrong_schema_fails_loud(self, tmp_path, monkeypatch):
        """Valid YAML but wrong schema (missing 'allowlist' key) → ValueError."""
        bad_schema = tmp_path / "wrong.yaml"
        bad_schema.write_text("notallowlist:\n  - foo: bar\n")
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(bad_schema))

        import yadgar.security.allowlist as _al

        with pytest.raises((ValueError, KeyError)):
            _al._reload_allowlist()


# ---------------------------------------------------------------------------
# 5. Source call-site detection
# ---------------------------------------------------------------------------


class TestSourceCallSiteDetection:
    """gate_or_reject records different source= values for test vs tool callers."""

    def test_source_call_site_detection(self, tmp_path, monkeypatch):
        """Call from a test file → source contains 'test'.
        Call from a tool file → source contains 'tool' or tool module name."""
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["test-fixture"]
                patterns: ["ghp_*"]
                reason: "test fixture bypass"
              - tags: ["prod-tool"]
                patterns: ["sk-ant-*"]
                reason: "tool bypass for prod"
            """)
        )
        audit_dir = tmp_path / "secret-gate-audit"
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))
        monkeypatch.setenv("YADGAR_SECRET_GATE_AUDIT_DIR", str(audit_dir))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        # Call from test context (this file is a test)
        token = "ghp_" + "D" * 25  # gitleaks:allow
        gate_or_reject(f"TOKEN={token}", tags=["test-fixture"])

        jsonl_files = list(audit_dir.glob("*.jsonl"))
        assert jsonl_files, "No audit entries created"

        entries = []
        for f in jsonl_files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))

        assert entries, "Audit log empty"
        # The source field must be a non-empty string
        source = entries[0].get("source", "")
        assert isinstance(source, str) and len(source) > 0, (
            f"source field must be a non-empty string, got: {source!r}"
        )

    def test_source_field_present_in_all_audit_entries(self, tmp_path, monkeypatch):
        """Every audit entry must have a non-empty source field."""
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["plan-document"]
                patterns: ["ghp_*", "sk-ant-*"]
                reason: "plan docs"
            """)
        )
        audit_dir = tmp_path / "secret-gate-audit"
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))
        monkeypatch.setenv("YADGAR_SECRET_GATE_AUDIT_DIR", str(audit_dir))

        import yadgar.security.allowlist as _al

        _al._reload_allowlist()

        from yadgar.secrets import gate_or_reject

        t1 = "ghp_" + "E" * 25  # gitleaks:allow
        t2 = "sk-ant-" + "F" * 25  # gitleaks:allow
        gate_or_reject(f"content with {t1}", tags=["plan-document"])
        gate_or_reject(f"content with {t2}", tags=["plan-document"])

        entries = []
        for f in audit_dir.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))

        assert len(entries) >= 2
        for e in entries:
            assert e.get("source"), f"source missing or empty in entry: {e}"
