"""v5.46.0 — Homebrew formula structural + syntax checks.

Ruby -c syntax check if ruby available; otherwise grep-based structural check.
RED phase: fails until Formula/yadgar.rb.in is created.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
FORMULA_TEMPLATE = REPO_ROOT / "Formula" / "yadgar.rb.in"
RUBY = shutil.which("ruby")


def test_formula_template_exists():
    """Formula/yadgar.rb.in must exist."""
    assert FORMULA_TEMPLATE.exists(), f"Missing: {FORMULA_TEMPLATE}"


def test_formula_template_has_class_declaration():
    """Formula template must have 'class Yadgar < Formula'."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "class Yadgar < Formula" in content


def test_formula_template_has_desc():
    """Formula must have a desc field."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert 'desc "' in content


def test_formula_template_has_homepage():
    """Formula must have a homepage field."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "homepage" in content


def test_formula_template_has_url_placeholder():
    """Formula template must have @VERSION@ placeholder in url."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "@VERSION@" in content


def test_formula_template_has_sha256_placeholder():
    """Formula template must have @SHA256@ placeholder."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "@SHA256@" in content


def test_formula_template_has_apache_license():
    """Formula must declare Apache-2.0 license."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert '"Apache-2.0"' in content or "Apache-2.0" in content


def test_formula_template_has_install_block():
    """Formula must have an install block."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "def install" in content


def test_formula_template_has_caveats_not_post_install():
    """Formula must use caveats (not post_install) for setup instructions."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "def caveats" in content
    # post_install auto-executes — must NOT be present (Option C contract)
    assert "def post_install" not in content, (
        "Formula must NOT use post_install — use caveats only (Option C contract)"
    )


def test_formula_template_has_test_block():
    """Formula must have a test block for brew test."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "test do" in content


def test_formula_template_mentions_yadgar_setup():
    """Formula caveats must mention yadgar-setup."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "yadgar-setup" in content


def test_formula_template_uses_python_3_13_or_3_14():
    """Formula must depend on python@3.13 or python@3.14 (Homebrew availability)."""
    assert FORMULA_TEMPLATE.exists()
    content = FORMULA_TEMPLATE.read_text()
    assert "python@3.13" in content or "python@3.14" in content or "@PYTHON_VERSION@" in content, (
        "Formula must pin a Python version (python@3.13 or python@3.14)"
    )


@pytest.mark.skipif(not RUBY, reason="ruby not in PATH")
def test_formula_template_ruby_syntax():
    """Formula template must pass 'ruby -c' syntax check (after @-placeholder strip)."""
    assert FORMULA_TEMPLATE.exists()
    # Substitute placeholders with dummy values for syntax check
    content = FORMULA_TEMPLATE.read_text()
    content = content.replace("@VERSION@", "5.46.0")
    content = content.replace("@SHA256@", "a" * 64)
    content = content.replace("@PYTHON_VERSION@", "3.13")
    result = subprocess.run(
        ["ruby", "-c", "-"],
        input=content,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Ruby syntax error:\n{result.stdout}\n{result.stderr}"
