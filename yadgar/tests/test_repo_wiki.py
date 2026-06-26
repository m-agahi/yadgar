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

from yadgar.repo_wiki.generator import (
    _slugify,
    generate_module_page,
    generate_wiki_pages,
)
from yadgar.repo_wiki.scanner import (
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
# Generator tests
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_dotted_to_slug(self) -> None:
        assert _slugify("yadgar.retrieval.core") == "mod-yadgar-retrieval-core"

    def test_single_name(self) -> None:
        assert _slugify("mypkg") == "mod-mypkg"

    def test_underscores_to_hyphens(self) -> None:
        assert _slugify("my_pkg.sub_mod") == "mod-my-pkg-sub-mod"


class TestGenerateModulePage:
    def test_slug_format(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert page["slug"] == "mod-mypkg-core"

    def test_directory_context_is_repo_root(self, fixture_repo: Path) -> None:
        """The directory_context stamp must be the repo root — never 'global'."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert page["directory_context"] == str(fixture_repo)
        assert page["directory_context"] != "global"

    def test_title_is_module_name(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert page["title"] == "mypkg.core"

    def test_content_contains_public_function(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "greet" in page["content"]

    def test_content_contains_class(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "Greeter" in page["content"]

    def test_content_contains_module_docstring(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "A simple fixture module for testing" in page["content"]

    def test_page_type_is_code(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert page["page_type"] == "code"
        assert page["category"] == "code"

    def test_tags_include_code_structure(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "code-structure" in page["tags"]
        assert "module" in page["tags"]

    def test_tags_include_package(self, fixture_repo: Path) -> None:
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "pkg-mypkg" in page["tags"]

    def test_parse_error_produces_page(self, fixture_repo: Path) -> None:
        """Even modules with syntax errors get a page (error noted in content)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "parse-error" in page["tags"]
        assert "Parse error" in page["content"] or "parse error" in page["content"].lower()

    def test_content_has_signature_code_block(self, fixture_repo: Path) -> None:
        """Function signatures must appear in fenced code blocks."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "```python" in page["content"]

    def test_private_methods_excluded_from_class_render(self, fixture_repo: Path) -> None:
        """Private methods (not __init__/__call__) should not appear in rendered class section."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        # _internal should NOT appear in the rendered page
        assert "_internal" not in page["content"]

    def test_generator_stamps_hash(self, fixture_repo: Path) -> None:
        """generate_module_page must include hash = SHA256(file bytes) + source_file."""
        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
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

        from yadgar.server.tools.project import _compute_source_hash

        rec = scan_python_module(fixture_repo / "mypkg" / "core.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        checker_hash = _compute_source_hash([page["source_file"]], hashlib)
        assert page["hash"] == checker_hash, (
            f"generator hash {page['hash']!r} != checker hash {checker_hash!r}"
        )

    def test_generator_stamps_hash_on_parse_error(self, fixture_repo: Path) -> None:
        """Even parse-error pages should get hash + source_file (file is readable even if unparseable)."""
        rec = scan_python_module(fixture_repo / "mypkg" / "broken.py", fixture_repo)
        page = generate_module_page(rec, str(fixture_repo))
        assert "hash" in page
        assert "source_file" in page
        assert len(page["hash"]) == 64


class TestGenerateWikiPages:
    def test_returns_list_of_dicts(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo))
        assert isinstance(pages, list)
        assert all(isinstance(p, dict) for p in pages)

    def test_sorted_by_slug(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo))
        slugs = [p["slug"] for p in pages]
        assert slugs == sorted(slugs)

    def test_all_pages_have_correct_directory_context(self, fixture_repo: Path) -> None:
        """Every page must be stamped with the repo root, never 'global'."""
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo))
        for page in pages:
            assert page["directory_context"] == str(fixture_repo)
            assert page["directory_context"] != "global"

    def test_skip_parse_errors(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        # With skip_parse_errors=True, broken.py should be absent
        pages_with = generate_wiki_pages(records, str(fixture_repo), skip_parse_errors=False)
        pages_without = generate_wiki_pages(records, str(fixture_repo), skip_parse_errors=True)
        broken_in_with = any("broken" in p["slug"] for p in pages_with)
        broken_in_without = any("broken" in p["slug"] for p in pages_without)
        assert broken_in_with
        assert not broken_in_without

    def test_unique_slugs(self, fixture_repo: Path) -> None:
        records = scan_repo(fixture_repo)
        pages = generate_wiki_pages(records, str(fixture_repo))
        slugs = [p["slug"] for p in pages]
        assert len(slugs) == len(set(slugs)), "Duplicate slugs detected"
