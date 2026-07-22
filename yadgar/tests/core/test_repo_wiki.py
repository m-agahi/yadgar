"""Tests for yadgar.repo_wiki — scanner + generator (T8, Option A).

TDD: these tests drive the scanner/generator implementation.

Fixtures: a small in-memory Python module is written to a temp directory
and scanned to assert correct extraction of signatures, docstrings, and
directory stamps.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from yadgar._shared.wiki.repo_wiki_schema import (
    REPO_WIKI_PAGE_TYPE,
    repo_wiki_slug,
    validate_repo_wiki_page,
)
from yadgar.core.repo_wiki.generator import (
    generate_module_page,
    generate_toc_page,
    generate_wiki_pages,
)
from yadgar.core.repo_wiki.scanner import (
    scan_python_module,
    scan_repo,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_MODULE = textwrap.dedent('''\
    """A simple fixture module for testing."""

    import os
    from pathlib import Path

    CONSTANT = 42


    def greet(name: str, loud: bool = False) -> str:
        """Return a greeting string.

        Args:
            name: the person to greet.
            loud: if True, uppercase the greeting.
        """
        msg = f"Hello, {name}!"
        return msg.upper() if loud else msg


    def _private_helper(x: int) -> int:
        """Internal helper — should appear in scan but not in page public section."""
        return x * 2


    class Greeter:
        """A greeter class."""

        def __init__(self, prefix: str = "Hello") -> None:
            """Initialise with a prefix."""
            self.prefix = prefix

        def greet(self, name: str) -> str:
            """Greet name using the prefix."""
            return f"{self.prefix}, {name}!"

        def _internal(self) -> None:
            """Private method — should be excluded from rendered page."""
            pass

        @classmethod
        def from_env(cls) -> "Greeter":
            """Create from environment."""
            return cls(prefix=os.environ.get("GREETING_PREFIX", "Hi"))

        @staticmethod
        def shout(text: str) -> str:
            """Uppercase text."""
            return text.upper()
''')

ASYNC_MODULE = textwrap.dedent('''\
    """Async module fixture."""

    async def fetch(url: str, timeout: int = 30) -> bytes:
        """Fetch data from url."""
        ...
''')

SYNTAX_ERROR_MODULE = "def broken(\n"

# Canonical project name used in tests that call generate_module_page directly.
# This avoids nondeterministic tmp_path basenames in slug assertions.
_PROJECT = "mypkg"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Create a minimal fake repo with two modules."""
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""mypkg package."""\n')
    (pkg / "core.py").write_text(SIMPLE_MODULE)
    (pkg / "async_mod.py").write_text(ASYNC_MODULE)
    (pkg / "broken.py").write_text(SYNTAX_ERROR_MODULE)
    return tmp_path


# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------


class TestScanPythonModule:
    def test_docstring_extracted(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        assert rec.docstring == "A simple fixture module for testing."

    def test_function_names(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        fn_names = [f.name for f in rec.functions]
        assert "greet" in fn_names
        assert "_private_helper" in fn_names

    def test_function_signature(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        greet_fn = next(f for f in rec.functions if f.name == "greet")
        # Signature must include the param names and annotations
        assert "name" in greet_fn.signature
        assert "str" in greet_fn.signature
        assert "bool" in greet_fn.signature
        assert greet_fn.signature.startswith("def greet")

    def test_function_docstring(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        greet_fn = next(f for f in rec.functions if f.name == "greet")
        assert greet_fn.docstring is not None
        assert "greeting string" in greet_fn.docstring

    def test_class_extracted(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        assert len(rec.classes) == 1
        cls = rec.classes[0]
        assert cls.name == "Greeter"
        assert cls.docstring == "A greeter class."

    def test_class_methods_extracted(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        cls = rec.classes[0]
        method_names = [m.name for m in cls.methods]
        assert "__init__" in method_names
        assert "greet" in method_names
        assert "_internal" in method_names  # scanner includes private; generator filters

    def test_classmethod_flag(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        cls = rec.classes[0]
        from_env = next(m for m in cls.methods if m.name == "from_env")
        assert from_env.is_classmethod is True

    def test_staticmethod_flag(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        cls = rec.classes[0]
        shout = next(m for m in cls.methods if m.name == "shout")
        assert shout.is_staticmethod is True

    def test_async_function(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "async_mod.py", fixture_repo)
        assert len(rec.functions) == 1
        fetch = rec.functions[0]
        assert fetch.is_async is True
        assert "async def fetch" in fetch.signature

    def test_module_path_relative(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        # path should be repo-relative
        assert rec.module_path == "mypkg/core.py"

    def test_module_name_dotted(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        assert rec.module_name == "mypkg.core"

    def test_init_module_name(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "__init__.py", fixture_repo)
        # __init__ module name should be "mypkg" not "mypkg.__init__"
        assert rec.module_name == "mypkg"

    def test_imports_extracted(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        assert "os" in rec.imports

    def test_syntax_error_record(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        assert rec.parse_error is not None
        assert "SyntaxError" in rec.parse_error

    def test_no_parse_error_on_valid(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        assert rec.parse_error is None


class TestScanRepo:
    def test_returns_module_records(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        assert len(records) >= 2  # at least core.py and async_mod.py

    def test_sorted_by_path(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        paths = [r.module_path for r in records]
        assert paths == sorted(paths)

    def test_skips_test_dirs_by_default(self, fixture_repo: Path) -> None:
        test_dir = fixture_repo / "tests"
        test_dir.mkdir()
        (test_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        records = scan_repo(fixture_repo)
        paths = [r.module_path for r in records]
        assert not any("tests" in p for p in paths)

    def test_include_tests(self, fixture_repo: Path) -> None:
        test_dir = fixture_repo / "tests"
        test_dir.mkdir()
        (test_dir / "test_foo.py").write_text("def test_foo(): pass\n")
        records = scan_repo(fixture_repo, include_tests=True)
        paths = [r.module_path for r in records]
        assert any("tests" in p for p in paths)

    def test_invalid_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a directory"):
            scan_repo(tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# Generator tests — Car D (#83): new slug scheme + page_type
# ---------------------------------------------------------------------------


class TestRepoWikiSlug:
    """Car D: generator uses repo_wiki_slug, not the old _slugify."""

    def test_slug_uses_project_prefix(self) -> None:
        slug = repo_wiki_slug("mypkg", "mypkg.core")
        assert slug == "mypkg-mod-mypkg-core"

    def test_slug_cross_project_distinct(self) -> None:
        """Same module name in two projects → distinct slugs (no cross-project collision)."""
        slug_a = repo_wiki_slug("proj-a", "logging")
        slug_b = repo_wiki_slug("proj-b", "logging")
        assert slug_a != slug_b
        assert slug_a.startswith("proj-a-mod-")
        assert slug_b.startswith("proj-b-mod-")

    def test_slug_contains_mod_marker(self) -> None:
        slug = repo_wiki_slug("mypkg", "mypkg.async_mod")
        assert "-mod-" in slug

    def test_slug_underscores_collapsed(self) -> None:
        slug = repo_wiki_slug("mypkg", "my_pkg.sub_mod")
        assert slug == "mypkg-mod-my-pkg-sub-mod"


class TestGenerateModulePage:
    def test_slug_uses_repo_wiki_slug(self, fixture_repo: Path) -> None:
        """Car D: module page slug == repo_wiki_slug(project, module_name)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        expected = repo_wiki_slug(_PROJECT, "mypkg.core")
        assert page["slug"] == expected

    def test_slug_fallback_uses_dir_basename(self, fixture_repo: Path) -> None:
        """When project= is not supplied, basename(directory_context) is used."""
        # fixture_repo is tmp_path; we scan the mypkg subdir as directory_context
        # so the project resolves to "mypkg" (basename of the directory)
        pkg_dir = fixture_repo / "mypkg"
        rec = scan_python_module(pkg_dir / "core.py", pkg_dir)
        page = generate_module_page(rec, str(pkg_dir))
        # basename = "mypkg" → slug starts with "mypkg-mod-"
        assert page["slug"].startswith("mypkg-mod-")

    def test_page_type_is_repo_wiki(self, fixture_repo: Path) -> None:
        """Car D: module pages must carry page_type=REPO_WIKI_PAGE_TYPE."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert page["page_type"] == REPO_WIKI_PAGE_TYPE
        assert page["page_type"] == "repo_wiki"

    def test_category_is_reference(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert page["category"] == "reference"

    def test_validate_repo_wiki_page_passes(self, fixture_repo: Path) -> None:
        """Car D: every generated module page passes validate_repo_wiki_page."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        errors = validate_repo_wiki_page(
            slug=page["slug"],
            source_file=page["source_file"],
            hash=page["hash"],
        )
        assert errors == [], f"validate_repo_wiki_page failed: {errors}"

    def test_validate_repo_wiki_page_passes_on_parse_error(self, fixture_repo: Path) -> None:
        """Even parse-error pages pass validation (they carry hash+source_file+-mod-)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        errors = validate_repo_wiki_page(
            slug=page["slug"],
            source_file=page["source_file"],
            hash=page["hash"],
        )
        assert errors == [], f"parse-error page failed validation: {errors}"

    def test_directory_context_is_repo_root(self, fixture_repo: Path) -> None:
        """The directory_context stamp must be the repo root — never 'global'."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert page["directory_context"] == str(fixture_repo)
        assert page["directory_context"] != "global"

    def test_title_is_module_name(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert page["title"] == "mypkg.core"

    def test_content_contains_public_function(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "greet" in page["content"]

    def test_content_contains_class(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "Greeter" in page["content"]

    def test_content_contains_module_docstring(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "A simple fixture module for testing" in page["content"]

    def test_tags_include_code_structure(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "code-structure" in page["tags"]
        assert "module" in page["tags"]

    def test_tags_include_package(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "pkg-mypkg" in page["tags"]

    def test_parse_error_produces_page(self, fixture_repo: Path) -> None:
        """Even modules with syntax errors get a page (error noted in content)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "parse-error" in page["tags"]
        assert "Parse error" in page["content"] or "parse error" in page["content"].lower()

    def test_parse_error_page_type_is_repo_wiki(self, fixture_repo: Path) -> None:
        """Parse-error module pages also carry page_type=repo_wiki."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert page["page_type"] == REPO_WIKI_PAGE_TYPE

    def test_content_has_signature_code_block(self, fixture_repo: Path) -> None:
        """Function signatures must appear in fenced code blocks."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "```python" in page["content"]

    def test_private_methods_excluded_from_class_render(self, fixture_repo: Path) -> None:
        """Private methods (not __init__/__call__) should not appear in rendered class section."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        # _internal should NOT appear in the rendered page
        assert "_internal" not in page["content"]

    def test_generator_stamps_hash(self, fixture_repo: Path) -> None:
        """generate_module_page must include hash = SHA256(file bytes) + source_file."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "hash" in page, "page must carry a 'hash' field"
        assert "source_file" in page, "page must carry a 'source_file' field"
        # hash must be non-empty hex
        assert len(page["hash"]) == 64, f"expected 64-char sha256 hex, got: {page['hash']!r}"
        # source_file must be an absolute path pointing to the module file
        src = Path(page["source_file"])
        assert src.is_absolute(), "source_file must be absolute"
        assert src.exists(), "source_file must point to existing file"

    def test_module_hash_matches_checker_algo(self, fixture_repo: Path) -> None:
        """Generator SHA256(file bytes) must equal checker's _compute_source_hash([file])."""
        import hashlib

        from yadgar.core.server.tools.project import _compute_source_hash

        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        checker_hash = _compute_source_hash([page["source_file"]], hashlib)
        assert page["hash"] == checker_hash, (
            f"generator hash {page['hash']!r} != checker hash {checker_hash!r}"
        )

    def test_generator_stamps_hash_on_parse_error(self, fixture_repo: Path) -> None:
        """Even parse-error pages should get hash + source_file (file is readable even if unparseable)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "hash" in page
        assert "source_file" in page
        assert len(page["hash"]) == 64


class TestGenerateWikiPages:
    def test_returns_list_of_dicts(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        assert isinstance(pages, list)
        assert all(isinstance(p, dict) for p in pages)

    def test_sorted_by_slug(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        slugs = [p["slug"] for p in pages]
        assert slugs == sorted(slugs)

    def test_all_pages_have_correct_directory_context(self, fixture_repo: Path) -> None:
        """Every page must be stamped with the repo root, never 'global'."""
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        for page in pages:
            assert page["directory_context"] == str(fixture_repo)
            assert page["directory_context"] != "global"

    def test_skip_parse_errors(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        # With skip_parse_errors=True, broken.py should be absent
        pages_with = generate_wiki_pages(
            records, str(fixture_repo), skip_parse_errors=False, project=_PROJECT
        )
        pages_without = generate_wiki_pages(
            records, str(fixture_repo), skip_parse_errors=True, project=_PROJECT
        )
        broken_in_with = any("broken" in p["slug"] for p in pages_with)
        broken_in_without = any("broken" in p["slug"] for p in pages_without)
        assert broken_in_with
        assert not broken_in_without

    def test_unique_slugs(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        slugs = [p["slug"] for p in pages]
        assert len(slugs) == len(set(slugs)), "Duplicate slugs detected"

    def test_module_pages_pass_schema_validation(self, fixture_repo: Path) -> None:
        """Car D: every module page (page_type=repo_wiki) passes validate_repo_wiki_page."""
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(
            records, str(fixture_repo), skip_parse_errors=False, project=_PROJECT
        )
        module_pages = [p for p in pages if p.get("page_type") == REPO_WIKI_PAGE_TYPE]
        assert module_pages, "no repo_wiki pages found"
        for page in module_pages:
            errors = validate_repo_wiki_page(
                slug=page["slug"],
                source_file=page.get("source_file"),
                hash=page.get("hash"),
            )
            assert errors == [], f"page {page['slug']!r} failed validation: {errors}"

    def test_crossref_targets_resolve_to_emitted_slugs(self, fixture_repo: Path) -> None:
        """Car D: every [[…]] crossref in generated content resolves to an emitted slug."""
        import re

        (fixture_repo / "mypkg" / "consumer.py").write_text(
            textwrap.dedent('''\
                """Consumer module."""
                from mypkg.core import Greeter
                def use(): pass
            ''')
        )
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        emitted_slugs = {p["slug"] for p in pages}
        crossref_re = re.compile(r"\[\[([^\]]+)\]\]")
        dangling = []
        for page in pages:
            for ref in crossref_re.findall(page.get("content", "")):
                if ref not in emitted_slugs:
                    dangling.append((page["slug"], ref))
        assert dangling == [], f"Dangling crossref targets found: {dangling}"


# ---------------------------------------------------------------------------
# Car A — __all__ extraction + empty-page skip (item 2)
# ---------------------------------------------------------------------------

ALL_ONLY_INIT = textwrap.dedent("""\
    from mypkg.core import Greeter, greet

    __all__ = ["Greeter", "greet"]
""")


class TestAllExtraction:
    def test_all_extracted(self, fixture_repo: Path) -> None:
        """Scanner extracts __all__ entries onto the ModuleRecord."""
        p = fixture_repo / "mypkg" / "reexport.py"
        p.write_text(ALL_ONLY_INIT)
        rec = scan_python_module(p, fixture_repo)
        assert rec.all_exports == ["Greeter", "greet"]

    def test_all_empty_when_absent(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        assert rec.all_exports == []


class TestEmptyPageSkip:
    def test_empty_init_has_no_content(self, fixture_repo: Path) -> None:
        """An empty __init__.py (no fns/classes/docstring/__all__) is flagged skip."""
        empty = fixture_repo / "mypkg" / "empty_pkg"
        empty.mkdir()
        (empty / "__init__.py").write_text("")
        rec = scan_python_module(empty / "__init__.py", fixture_repo)
        assert rec.has_content is False

    def test_reexport_init_has_content(self, fixture_repo: Path) -> None:
        """An __init__.py with only __all__ (API re-export) is KEPT."""
        p = fixture_repo / "mypkg" / "reexport.py"
        p.write_text(ALL_ONLY_INIT)
        rec = scan_python_module(p, fixture_repo)
        assert rec.has_content is True

    def test_docstring_only_has_content(self, fixture_repo: Path) -> None:
        p = fixture_repo / "mypkg" / "doconly.py"
        p.write_text('"""Just a docstring."""\n')
        rec = scan_python_module(p, fixture_repo)
        assert rec.has_content is True

    def test_parse_error_not_skipped_by_emptiness(self, fixture_repo: Path) -> None:
        """Parse-error records keep has_content=True (governed by skip_parse_errors, not emptiness)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        assert rec.has_content is True

    def test_scan_repo_skips_empty_init(self, fixture_repo: Path) -> None:
        """scan_repo drops empty __init__.py but keeps re-exporting one."""
        empty = fixture_repo / "mypkg" / "empty_pkg"
        empty.mkdir()
        (empty / "__init__.py").write_text("")
        reexport = fixture_repo / "mypkg" / "reexport_pkg"
        reexport.mkdir()
        (reexport / "__init__.py").write_text(ALL_ONLY_INIT)
        records = scan_repo(fixture_repo)
        paths = [r.module_path for r in records]
        assert not any("empty_pkg" in p for p in paths), "empty __init__ should be dropped"
        assert any("reexport_pkg" in p for p in paths), "re-exporting __init__ should be kept"


# ---------------------------------------------------------------------------
# Car A — only page importable files (item 3)
# ---------------------------------------------------------------------------


class TestImportableFilesOnly:
    def test_hyphenated_stem_not_paged(self, fixture_repo: Path) -> None:
        """A hyphenated hook script (non-identifier stem) is NOT scanned/paged."""
        (fixture_repo / "mypkg" / "file-changed.py").write_text(
            '"""Hook script."""\ndef run(): pass\n'
        )
        (fixture_repo / "mypkg" / "file_changed.py").write_text(
            '"""Importable twin."""\ndef run(): pass\n'
        )
        records = scan_repo(fixture_repo)
        paths = [r.module_path for r in records]
        assert not any("file-changed" in p for p in paths), "hyphenated stem must be skipped"
        assert any("file_changed" in p for p in paths), "importable twin must be kept"

    def test_no_slug_collision_from_hyphen_twins(self, fixture_repo: Path) -> None:
        """Hyphen/underscore twins must not produce a colliding slug (data-loss bug)."""
        (fixture_repo / "mypkg" / "file-changed.py").write_text("def a(): pass\n")
        (fixture_repo / "mypkg" / "file_changed.py").write_text("def b(): pass\n")
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        slugs = [p["slug"] for p in pages]
        assert len(slugs) == len(set(slugs)), "slug collision — data loss on wiki_add"


# ---------------------------------------------------------------------------
# Car A — ignore layers: gitignore + extra-ignore defaults (item 4a)
# ---------------------------------------------------------------------------


class TestIgnoreLayers:
    def test_migrations_dir_skipped(self, fixture_repo: Path) -> None:
        mig = fixture_repo / "mypkg" / "migrations"
        mig.mkdir()
        (mig / "0001_initial.py").write_text("def up(): pass\n")
        records = scan_repo(fixture_repo)
        assert not any("migrations" in r.module_path for r in records)

    def test_pb2_files_skipped(self, fixture_repo: Path) -> None:
        (fixture_repo / "mypkg" / "service_pb2.py").write_text("class Msg: pass\n")
        records = scan_repo(fixture_repo)
        assert not any("_pb2" in r.module_path for r in records)

    def test_alembic_versions_skipped(self, fixture_repo: Path) -> None:
        av = fixture_repo / "alembic" / "versions"
        av.mkdir(parents=True)
        (av / "abc123.py").write_text("def upgrade(): pass\n")
        records = scan_repo(fixture_repo)
        assert not any("alembic/versions" in r.module_path for r in records)

    def test_gitignored_file_skipped(self, fixture_repo: Path) -> None:
        """git check-ignore batch excludes gitignored source files."""
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True)
        (fixture_repo / ".gitignore").write_text("mypkg/generated.py\n")
        (fixture_repo / "mypkg" / "generated.py").write_text("def gen(): pass\n")
        records = scan_repo(fixture_repo)
        assert not any("generated.py" in r.module_path for r in records)

    def test_non_git_dir_still_scans(self, fixture_repo: Path) -> None:
        """scan_repo must not crash outside a git repo (check-ignore exit 128)."""
        # fixture_repo is a plain tmp_path (no git init) — must still work.
        records = scan_repo(fixture_repo)
        assert any("core.py" in r.module_path for r in records)


# ---------------------------------------------------------------------------
# Car A — first-party module set + crossref [[{project}-mod-]] links (items 4b, 5)
# Car D (#83): crossref targets use repo_wiki_slug(project, module), not old mod-
# ---------------------------------------------------------------------------


class TestCrossrefLinks:
    def test_in_repo_import_becomes_link(self, fixture_repo: Path) -> None:
        """An import resolving to a scanned in-repo module renders [[{project}-mod-...]]."""
        (fixture_repo / "mypkg" / "consumer.py").write_text(
            textwrap.dedent('''\
                """Consumer module."""
                import os
                from mypkg.core import Greeter

                def use() -> None:
                    """Use Greeter."""
                    pass
            ''')
        )
        records = scan_repo(fixture_repo)
        first_party = {r.module_name for r in records}
        rec = next(r for r in records if r.module_name == "mypkg.consumer")
        page = generate_module_page(
            rec, str(fixture_repo), first_party=first_party, project=_PROJECT
        )
        # mypkg.core is in-repo → linked with new namespaced slug
        expected_link = f"[[{repo_wiki_slug(_PROJECT, 'mypkg.core')}]]"
        assert expected_link in page["content"], (
            f"{expected_link!r} not in content; content snippet: {page['content'][:400]}"
        )
        # os is stdlib → stays plain backtick, never linked
        assert "[[" + repo_wiki_slug(_PROJECT, "os") + "]]" not in page["content"]
        assert "`os`" in page["content"]

    def test_external_import_stays_backtick(self, fixture_repo: Path) -> None:
        (fixture_repo / "mypkg" / "ext.py").write_text(
            textwrap.dedent('''\
                """Ext module."""
                from third_party.lib import thing

                def go(): pass
            ''')
        )
        records = scan_repo(fixture_repo)
        first_party = {r.module_name for r in records}
        rec = next(r for r in records if r.module_name == "mypkg.ext")
        page = generate_module_page(
            rec, str(fixture_repo), first_party=first_party, project=_PROJECT
        )
        assert "[[" not in page["content"], "external import must not be linked"

    def test_from_import_links_module_not_symbol(self, fixture_repo: Path) -> None:
        """from mypkg.core import greet → link the module mypkg.core, not the symbol."""
        (fixture_repo / "mypkg" / "sym.py").write_text(
            textwrap.dedent('''\
                """Sym module."""
                from mypkg.core import greet

                def go(): pass
            ''')
        )
        records = scan_repo(fixture_repo)
        first_party = {r.module_name for r in records}
        rec = next(r for r in records if r.module_name == "mypkg.sym")
        page = generate_module_page(
            rec, str(fixture_repo), first_party=first_party, project=_PROJECT
        )
        expected_link = f"[[{repo_wiki_slug(_PROJECT, 'mypkg.core')}]]"
        assert expected_link in page["content"]
        # must NOT try to link the symbol greet as a module
        assert repo_wiki_slug(_PROJECT, "mypkg.core.greet") not in page["content"]

    def test_generate_wiki_pages_autobuilds_first_party(self, fixture_repo: Path) -> None:
        """generate_wiki_pages builds the first-party set from records automatically."""
        (fixture_repo / "mypkg" / "consumer.py").write_text(
            textwrap.dedent('''\
                """Consumer."""
                from mypkg.core import Greeter
                def use(): pass
            ''')
        )
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        consumer_slug = repo_wiki_slug(_PROJECT, "mypkg.consumer")
        consumer = next(p for p in pages if p["slug"] == consumer_slug)
        expected_link = f"[[{repo_wiki_slug(_PROJECT, 'mypkg.core')}]]"
        assert expected_link in consumer["content"]

    def test_no_first_party_means_plain_backticks(self, fixture_repo: Path) -> None:
        """Default first_party=None → all imports stay plain backticks (back-compat)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo), project=_PROJECT)
        assert "[[" not in page["content"]

    def test_first_party_edges_never_truncated(self, fixture_repo: Path) -> None:
        """isort orders first-party imports LAST; the 10-import cap must not drop
        crossref edges. All in-repo imports render as [[{project}-mod-]] regardless of count."""
        # 12 stdlib imports (fill the display cap) THEN one in-repo import last.
        stdlib_lines = "\n".join(
            f"import {m}"
            for m in (
                "os",
                "sys",
                "json",
                "re",
                "io",
                "abc",
                "csv",
                "math",
                "time",
                "uuid",
                "enum",
                "glob",
            )
        )
        (fixture_repo / "mypkg" / "heavy.py").write_text(
            f'"""Heavy imports."""\n{stdlib_lines}\nfrom mypkg.core import Greeter\n\ndef go(): pass\n'
        )
        records = scan_repo(fixture_repo)
        first_party = {r.module_name for r in records}
        rec = next(r for r in records if r.module_name == "mypkg.heavy")
        page = generate_module_page(
            rec, str(fixture_repo), first_party=first_party, project=_PROJECT
        )
        # the in-repo edge must survive even though it is the 13th import.
        expected_link = f"[[{repo_wiki_slug(_PROJECT, 'mypkg.core')}]]"
        assert expected_link in page["content"], "first-party edge truncated by import cap"

    def test_no_dangling_mod_prefix_in_crossrefs(self, fixture_repo: Path) -> None:
        """Car D: no crossref should reference an old-style mod-<name> slug (without project prefix)."""
        import re

        (fixture_repo / "mypkg" / "consumer.py").write_text(
            textwrap.dedent('''\
                """Consumer."""
                from mypkg.core import Greeter
                def use(): pass
            ''')
        )
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        crossref_re = re.compile(r"\[\[([^\]]+)\]\]")
        old_style = []
        for page in pages:
            for ref in crossref_re.findall(page.get("content", "")):
                # Old-style slugs started with "mod-" without a project prefix segment
                if ref.startswith("mod-"):
                    old_style.append((page["slug"], ref))
        assert old_style == [], (
            f"Old-style mod- crossrefs found (missing project prefix): {old_style}"
        )


# ---------------------------------------------------------------------------
# Car A — root TOC/index page (item 6)
# Car D (#83): TOC links use repo_wiki_slug; TOC page_type stays "module"
# ---------------------------------------------------------------------------


class TestTocPage:
    def test_toc_page_emitted(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        toc = generate_toc_page(records, str(fixture_repo), project=_PROJECT)
        assert toc["slug"] == f"{_PROJECT}-repo-wiki-index"
        assert toc["directory_context"] == str(fixture_repo)
        assert toc["category"] == "reference"
        # TOC is NOT page_type=repo_wiki — it has no -mod- in slug, no hash/source_file
        assert toc["page_type"] == "module"

    def test_toc_links_use_namespaced_slugs(self, fixture_repo: Path) -> None:
        """Car D: TOC links use repo_wiki_slug(project, module), not old mod-<name>."""
        records = scan_repo(fixture_repo)
        toc = generate_toc_page(records, str(fixture_repo), project=_PROJECT)
        expected_link = f"[[{repo_wiki_slug(_PROJECT, 'mypkg.core')}]]"
        assert expected_link in toc["content"], (
            f"TOC must link {expected_link!r}; content: {toc['content'][:400]}"
        )
        # Old-style mod-mypkg-core must not appear
        assert "[[mod-mypkg-core]]" not in toc["content"]

    def test_generate_wiki_pages_includes_toc(self, fixture_repo: Path) -> None:
        """generate_wiki_pages(..., project=...) appends the TOC index page, sorted."""
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo), project=_PROJECT)
        slugs = [p["slug"] for p in pages]
        assert f"{_PROJECT}-repo-wiki-index" in slugs
        assert slugs == sorted(slugs), "TOC must be inserted with sort preserved"
        # every page still stamped with repo root
        for p in pages:
            assert p["directory_context"] == str(fixture_repo)

    def test_toc_has_no_hash_or_source_file(self, fixture_repo: Path) -> None:
        """TOC is not a source module — no hash/source_file."""
        records = scan_repo(fixture_repo)
        toc = generate_toc_page(records, str(fixture_repo), project=_PROJECT)
        assert "hash" not in toc
        assert "source_file" not in toc

    def test_cross_project_slug_distinct(self) -> None:
        """Car D: same module name under two projects → distinct slugs."""
        slug_a = repo_wiki_slug("proj-a", "logging")
        slug_b = repo_wiki_slug("proj-b", "logging")
        assert slug_a != slug_b


# ---------------------------------------------------------------------------
# Car B0 (#83) — CLI passes project= so --json emits the TOC index page
# ---------------------------------------------------------------------------


class TestCliTocWiring:
    def test_cmd_repo_wiki_json_includes_toc(self, fixture_repo: Path, capsys) -> None:
        """cmd_repo_wiki --dry-run --json emits the <project>-repo-wiki-index page.

        The CLI must call generate_wiki_pages(..., project=<repo basename>) so the
        navigable TOC entry point is produced. Before B0 it passed no project → no TOC.
        """
        import json as _json
        from types import SimpleNamespace

        from yadgar.core.cli.repo_wiki import cmd_repo_wiki

        pkg = fixture_repo / "mypkg"
        args = SimpleNamespace(
            repo=str(pkg),
            include_tests=False,
            include_errors=False,
            json=True,
            dry_run=True,
        )
        cmd_repo_wiki(args)
        out = capsys.readouterr().out
        payload = _json.loads(out)
        slugs = [p["slug"] for p in payload["pages"]]
        assert f"{pkg.name}-repo-wiki-index" in slugs, (
            f"CLI did not emit the TOC index page; slugs={slugs}"
        )
        # module pages carry page_type=repo_wiki (Car D) and hash/source_file
        mod_pages = [p for p in payload["pages"] if p.get("page_type") == REPO_WIKI_PAGE_TYPE]
        assert mod_pages, "module pages must carry page_type=repo_wiki"
        assert all("hash" in p for p in mod_pages), "module pages must carry hash"
        assert all("source_file" in p for p in mod_pages)

    def test_cmd_repo_wiki_module_slugs_namespaced(self, fixture_repo: Path, capsys) -> None:
        """Car D: CLI-emitted module slugs must start with {project}-mod-."""
        import json as _json
        from types import SimpleNamespace

        from yadgar.core.cli.repo_wiki import cmd_repo_wiki

        pkg = fixture_repo / "mypkg"
        args = SimpleNamespace(
            repo=str(pkg),
            include_tests=False,
            include_errors=False,
            json=True,
            dry_run=False,
            stale_only=False,
        )
        cmd_repo_wiki(args)
        out = capsys.readouterr().out
        payload = _json.loads(out)
        mod_pages = [p for p in payload["pages"] if p.get("page_type") == REPO_WIKI_PAGE_TYPE]
        for page in mod_pages:
            assert page["slug"].startswith(f"{pkg.name}-mod-"), (
                f"Slug {page['slug']!r} not namespaced with project {pkg.name!r}"
            )


# ---------------------------------------------------------------------------
# Car B (#83) — --stale-only host-side hash-diff + --stored-hashes baseline
# ---------------------------------------------------------------------------


def _run_stale_only(fixture_repo, capsys, stored, *, from_stdin=False, monkeypatch=None):
    """Helper: run cmd_repo_wiki --stale-only with a stored-hashes baseline.

    stored: dict[str, str] baseline, or None to omit --stored-hashes entirely.
    from_stdin: feed the baseline JSON via stdin and pass '-' as the path.
    Returns the parsed JSON payload printed on stdout.
    """
    import io
    import json as _json
    import sys
    from types import SimpleNamespace

    from yadgar.core.cli.repo_wiki import cmd_repo_wiki

    pkg = fixture_repo / "mypkg"

    stored_arg = None
    if stored is not None:
        if from_stdin:
            assert monkeypatch is not None
            monkeypatch.setattr(sys, "stdin", io.StringIO(_json.dumps(stored)))
            stored_arg = "-"
        else:
            baseline_path = fixture_repo / "baseline.json"
            baseline_path.write_text(_json.dumps(stored))
            stored_arg = str(baseline_path)

    args = SimpleNamespace(
        repo=str(pkg),
        include_tests=False,
        include_errors=False,
        json=True,
        dry_run=False,
        stale_only=True,
        stored_hashes=stored_arg,
    )
    cmd_repo_wiki(args)
    out = capsys.readouterr().out
    return _json.loads(out)


def _current_hashes(fixture_repo):
    """Generate current host-side {slug: hash} for hash-bearing module pages.

    Car D: uses project=pkg.name so slugs are namespaced (mypkg-mod-*).
    """
    from yadgar.core.repo_wiki.generator import generate_wiki_pages
    from yadgar.core.repo_wiki.scanner import scan_repo

    pkg = fixture_repo / "mypkg"
    records = scan_repo(pkg)
    # Mirror the CLI default (skip_parse_errors=True) so the baseline reflects
    # exactly the hash-bearing pages the ingest agent would have written.
    pages = generate_wiki_pages(records, str(pkg), skip_parse_errors=True, project=pkg.name)
    return {p["slug"]: p["hash"] for p in pages if "hash" in p}


def _core_slug(fixture_repo: Path) -> str:
    """Return the namespaced slug for the 'core' module when the scan root is fixture_repo/mypkg.

    The CLI scans `fixture_repo / "mypkg"` as the repo root, so module names are
    "core", "async_mod", etc. (not "mypkg.core").  Project = pkg.name = "mypkg".
    """
    return repo_wiki_slug((fixture_repo / "mypkg").name, "core")


def _async_mod_slug(fixture_repo: Path) -> str:
    return repo_wiki_slug((fixture_repo / "mypkg").name, "async_mod")


class TestStaleOnly:
    def test_all_current_emits_nothing(self, fixture_repo: Path, capsys) -> None:
        """Baseline matching every current hash → 0 drifted/new pages emitted."""
        baseline = _current_hashes(fixture_repo)
        payload = _run_stale_only(fixture_repo, capsys, baseline)
        assert payload["stale_only"] is True
        assert payload["pages"] == [], f"nothing drifted but got: {payload['pages']}"
        assert payload["deleted"] == []

    def test_one_changed_hash_emits_only_that_page(self, fixture_repo: Path, capsys) -> None:
        """One source file's stored hash differs → only that page emitted."""
        baseline = _current_hashes(fixture_repo)
        core_slug = _core_slug(fixture_repo)
        assert core_slug in baseline, f"expected {core_slug!r} in baseline; got: {list(baseline)}"
        baseline[core_slug] = "0" * 64
        payload = _run_stale_only(fixture_repo, capsys, baseline)
        slugs = [p["slug"] for p in payload["pages"]]
        assert slugs == [core_slug], f"expected only the drifted page, got {slugs}"
        assert payload["deleted"] == []

    def test_new_module_emitted(self, fixture_repo: Path, capsys) -> None:
        """A module absent from the baseline is emitted as new."""
        baseline = _current_hashes(fixture_repo)
        core_slug = _core_slug(fixture_repo)
        del baseline[core_slug]
        payload = _run_stale_only(fixture_repo, capsys, baseline)
        slugs = [p["slug"] for p in payload["pages"]]
        assert core_slug in slugs
        # unchanged modules (still in baseline) must NOT be emitted
        assert _async_mod_slug(fixture_repo) not in slugs

    def test_deleted_slug_reported(self, fixture_repo: Path, capsys) -> None:
        """A baseline slug with no corresponding source module → listed under deleted."""
        baseline = _current_hashes(fixture_repo)
        # Use module name "gone" (no package prefix, since scan root = mypkg/).
        gone_slug = repo_wiki_slug((fixture_repo / "mypkg").name, "gone")
        baseline[gone_slug] = "a" * 64  # no source module named "gone"
        payload = _run_stale_only(fixture_repo, capsys, baseline)
        assert gone_slug in payload["deleted"]
        # a real, unchanged module must not appear in deleted
        assert _core_slug(fixture_repo) not in payload["deleted"]

    def test_toc_never_in_pages(self, fixture_repo: Path, capsys) -> None:
        """The hashless TOC index page is never emitted in `pages` (no hash to diff)."""
        baseline = _current_hashes(fixture_repo)
        core_slug = _core_slug(fixture_repo)
        baseline[core_slug] = "0" * 64  # force one drift
        payload = _run_stale_only(fixture_repo, capsys, baseline)
        slugs = [p["slug"] for p in payload["pages"]]
        assert f"{(fixture_repo / 'mypkg').name}-repo-wiki-index" not in slugs

    def test_toc_stale_flag_on_new(self, fixture_repo: Path, capsys) -> None:
        """toc_stale True when the module set changed (new module), else the TOC is current."""
        baseline = _current_hashes(fixture_repo)
        core_slug = _core_slug(fixture_repo)
        # content drift only (set unchanged) → toc not stale
        drift = dict(baseline)
        drift[core_slug] = "0" * 64
        payload = _run_stale_only(fixture_repo, capsys, drift)
        assert payload["toc_stale"] is False
        # new module (set changed) → toc stale
        missing = dict(baseline)
        del missing[core_slug]
        payload2 = _run_stale_only(fixture_repo, capsys, missing)
        assert payload2["toc_stale"] is True

    def test_no_stored_hashes_treats_all_new(self, fixture_repo: Path, capsys) -> None:
        """Omitting --stored-hashes → empty baseline → every module page is new."""
        payload = _run_stale_only(fixture_repo, capsys, None)
        slugs = [p["slug"] for p in payload["pages"]]
        core_slug = _core_slug(fixture_repo)
        assert core_slug in slugs
        assert payload["deleted"] == []

    def test_stdin_baseline(self, fixture_repo: Path, capsys, monkeypatch) -> None:
        """Baseline via stdin ('-') works identically to a file path."""
        baseline = _current_hashes(fixture_repo)
        payload = _run_stale_only(
            fixture_repo, capsys, baseline, from_stdin=True, monkeypatch=monkeypatch
        )
        assert payload["stale_only"] is True
        assert payload["pages"] == []


# ---------------------------------------------------------------------------
# Car A — multi-language extractor registry seam (item 7)
# ---------------------------------------------------------------------------


class TestExtractorRegistry:
    def test_registry_maps_py(self) -> None:
        from yadgar.core.repo_wiki.scanner import _EXTRACTOR_REGISTRY, scan_python_module

        assert _EXTRACTOR_REGISTRY[".py"] is scan_python_module

    def test_unregistered_suffix_yields_no_page(self, fixture_repo: Path) -> None:
        """A .go file (no registered extractor) produces no ModuleRecord."""
        (fixture_repo / "mypkg" / "main.go").write_text("package main\nfunc main() {}\n")
        records = scan_repo(fixture_repo)
        assert not any(r.module_path.endswith(".go") for r in records)

    def test_registered_py_still_scanned(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        assert any(r.module_path.endswith(".py") for r in records)


# ---------------------------------------------------------------------------
# Car E (#83) — no-churn integration proof (headline test for bug #83)
# ---------------------------------------------------------------------------
#
# Belt-and-suspenders assertions that COULD duplicate existing tests are
# intentionally omitted here — they already exist:
#   • slug prefix:    TestRepoWikiSlug::test_slug_cross_project_distinct (L250)
#   • crossref targets: TestGenerateWikiPages::test_crossref_targets_resolve_to_emitted_slugs (L475)
# This class covers the INTEGRATION PROOF: generate → feed own output back as
# baseline → zero drift.  The bug (#83) was: generator mod-* slugs never
# reconciled with stored title-slugs → churn on every cadence.


class TestNoChurnIntegrationProof:
    """Car E integration proof: generate then stale-diff own output ⇒ zero drift.

    This is the headline regression test for bug #83.  Before the fix, the
    generator emitted ``{project}-mod-*`` slugs but wiki_add stored pages at
    title-slugs.  Feeding the just-generated slug→hash map back as the baseline
    would always produce non-empty pages (slug mismatch) and non-empty deleted
    lists — i.e., churn on every cadence.

    With Cars A–D in place the slug scheme is unified end-to-end: generate →
    stale-diff → zero drift.
    """

    def test_generate_then_stale_diff_own_output_is_zero(self, fixture_repo: Path, capsys) -> None:
        """Generate via --json, build baseline from that output, diff back → zero.

        Steps:
        1. Run cmd_repo_wiki --json to get the full page list (non-stale path).
        2. Parse the emitted JSON and build {slug: hash} from module pages only
           (skip the TOC which has no hash).
        3. Feed that map back as the stored baseline via --stale-only --stored-hashes.
        4. Assert pages==[], deleted==[], toc_stale==False.

        This proves:
        - The generator's slug scheme matches what the stale-diff expects.
        - No phantom "new" pages from slug mismatch (pre-#83 churn mode).
        - No phantom "deleted" entries from unresolvable stored keys.
        - The TOC is not mis-flagged as stale when the module set is unchanged.
        """
        import json as _json
        from types import SimpleNamespace

        from yadgar.core.cli.repo_wiki import cmd_repo_wiki

        pkg = fixture_repo / "mypkg"

        # ── Step 1: full generate (non-stale --json) ──────────────────────
        args_gen = SimpleNamespace(
            repo=str(pkg),
            include_tests=False,
            include_errors=False,
            json=True,
            dry_run=False,
            stale_only=False,
        )
        cmd_repo_wiki(args_gen)
        gen_out = capsys.readouterr().out  # capture + clear buffer

        gen_payload = _json.loads(gen_out)
        assert "pages" in gen_payload, "generator must emit a 'pages' key"

        # ── Step 2: build baseline from generated module pages (skip hashless TOC) ──
        baseline = {
            p["slug"]: p["hash"]
            for p in gen_payload["pages"]
            if "hash" in p  # TOC page has no hash — skip it
        }
        assert baseline, "baseline must be non-empty (generator produced module pages)"

        # Assert every module-page slug uses the {project}-mod- prefix.
        project = pkg.name  # "mypkg" for the fixture
        for slug in baseline:
            assert slug.startswith(f"{project}-mod-"), (
                f"module-page slug {slug!r} lacks {{project}}-mod- prefix "
                f"(expected {project!r} namespace)"
            )

        # ── Step 3: stale-diff against own output ──────────────────────────
        baseline_path = fixture_repo / "no_churn_baseline.json"
        baseline_path.write_text(_json.dumps(baseline))

        args_stale = SimpleNamespace(
            repo=str(pkg),
            include_tests=False,
            include_errors=False,
            json=True,
            dry_run=False,
            stale_only=True,
            stored_hashes=str(baseline_path),
        )
        cmd_repo_wiki(args_stale)
        stale_out = capsys.readouterr().out

        stale_payload = _json.loads(stale_out)

        # ── Step 4: assert zero drift ──────────────────────────────────────
        assert stale_payload["pages"] == [], (
            f"Expected zero drifted/new pages when diffing own output; "
            f"got: {[p['slug'] for p in stale_payload['pages']]}"
        )
        assert stale_payload["deleted"] == [], (
            f"Expected zero deleted slugs when diffing own output; got: {stale_payload['deleted']}"
        )
        assert stale_payload["toc_stale"] is False, (
            "Expected toc_stale=False when module set is unchanged; "
            f"got: {stale_payload['toc_stale']}"
        )
