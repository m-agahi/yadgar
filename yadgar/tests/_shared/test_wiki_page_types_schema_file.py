"""TDD (RED-first) — Stage 3 item 1: PAGE_TYPES externalized to a packaged schema file.

The registry moves from literals in yadgar/_shared/wiki_meta.py to
yadgar/_shared/schemas/wiki_page_types.yaml, loaded at import via
importlib.resources (packaged resource — no $HOME copy, ships tested).

Tests:
  1. schema file loads as a package resource
  2. PAGE_TYPES derives from the yaml required lists (back-compat shape)
  3. agent_prompt schema is richer: optional sections + metadata
  4. zero schema literals left in the wiki_meta.py code body
  5. lint stays advisory: optional sections produce NO issues; missing
     required sections produce warning-severity issues only
"""

from __future__ import annotations


class TestSchemaFileLoads:
    def test_schema_resource_exists(self):
        from importlib.resources import files

        text = (
            files("yadgar._shared").joinpath("schemas").joinpath("wiki_page_types.yaml").read_text()
        )
        assert "page_types" in text

    def test_schema_data_loaded(self):
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_SCHEMAS

        assert isinstance(PAGE_TYPE_SCHEMAS, dict)
        assert set(PAGE_TYPE_SCHEMAS) == {
            "function",
            "module",
            "service",
            "architecture",
            "decision",
            "analysis",
            "adr",
            "agent_prompt",
            "task_list",
        }


class TestPageTypesDerived:
    def test_page_types_backcompat_shape(self):
        """PAGE_TYPES keeps its dict[str, list[str]] required-sections shape."""
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_SCHEMAS, PAGE_TYPES

        for page_type, sections in PAGE_TYPES.items():
            assert isinstance(sections, list)
            assert sections == list(PAGE_TYPE_SCHEMAS[page_type]["required"])

    def test_known_required_sections(self):
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPES

        assert PAGE_TYPES["agent_prompt"] == ["Purpose", "Prompt"]
        assert PAGE_TYPES["decision"] == ["Context", "Decision", "Consequences"]


class TestAgentPromptRicherSchema:
    def test_optional_sections(self):
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_SCHEMAS

        optional = PAGE_TYPE_SCHEMAS["agent_prompt"].get("optional", [])
        for section in ("Preconditions", "Failure modes", "Verification", "Composes"):
            assert section in optional, f"agent_prompt optional missing {section!r}"

    def test_metadata_keys(self):
        from yadgar._shared.wiki.wiki_meta import PAGE_TYPE_SCHEMAS

        metadata = PAGE_TYPE_SCHEMAS["agent_prompt"].get("metadata", {})
        assert "composes_with" in metadata
        assert "applies_to" in metadata


class TestZeroSchemaLiteralsInCode:
    def test_no_section_literals_in_wiki_meta_source(self):
        """The code body keeps zero schema literals — the yaml is the single source."""
        import inspect

        import yadgar._shared.wiki.wiki_meta as wiki_meta

        source = inspect.getsource(wiki_meta)
        # Spot-check section names that only ever existed as schema literals.
        for literal in ('"Signature"', '"Consequences"', '"Exports"', '"Preconditions"'):
            assert literal not in source, (
                f"schema literal {literal} still present in wiki_meta.py — "
                "schema must live only in wiki_page_types.yaml"
            )


class TestLintStaysAdvisory:
    def test_optional_sections_produce_no_issues(self):
        from yadgar._shared.wiki.wiki_meta import check_page_type_format

        content = (
            "## Purpose\n\nx\n\n## Prompt\n\ny\n\n"
            "## Preconditions\n\np\n\n## Failure modes\n\nf\n\n"
            "## Verification\n\nv\n\n## Composes\n\n- [[agent-discipline-recall-first]]\n"
        )
        issues = check_page_type_format("some-slug", "agent_prompt", content)
        assert issues == [], f"optional sections must not produce issues: {issues}"

    def test_missing_required_is_warning_only(self):
        from yadgar._shared.wiki.wiki_meta import check_page_type_format

        issues = check_page_type_format("some-slug", "agent_prompt", "## Purpose\n\nonly\n")
        assert len(issues) == 1
        assert issues[0]["severity"] == "warning"
        assert issues[0]["type"] == "missing_section"

    def test_unknown_page_type_no_issues(self):
        from yadgar._shared.wiki.wiki_meta import check_page_type_format

        assert check_page_type_format("s", "no-such-type", "anything") == []
