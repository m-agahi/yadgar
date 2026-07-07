"""Shebang-pinning tests for `_copy_hook`.

`#!/usr/bin/env python3` resolves on PATH, which on systems with a pipx-
installed yadgar plus a separate system python3 (e.g. NixOS) points at a
python that does NOT have yadgar importable. `_copy_hook` must rewrite
such shebangs to `#!<sys.executable>` so yadgar-bundled hooks can
`import yadgar.paths` on the installer's runtime.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from yadgar.core.install_hooks_lib import _copy_hook


def test_copy_hook_rewrites_env_python3_shebang(tmp_path: Path) -> None:
    src = tmp_path / "hook.py"
    src.write_text("#!/usr/bin/env python3\nimport yadgar.paths\n")
    dst = tmp_path / "out.py"

    _copy_hook(src, dst, dry_run=False)

    assert dst.exists()
    first_line = dst.read_text().splitlines()[0]
    assert first_line == f"#!{sys.executable}", (
        f"shebang not rewritten to sys.executable: got {first_line!r}"
    )
    # File should be executable
    assert dst.stat().st_mode & stat.S_IXUSR


def test_copy_hook_rewrites_env_python_shebang_no_3(tmp_path: Path) -> None:
    src = tmp_path / "hook.py"
    src.write_text("#!/usr/bin/env python\nprint('hi')\n")
    dst = tmp_path / "out.py"

    _copy_hook(src, dst, dry_run=False)

    assert dst.read_text().splitlines()[0] == f"#!{sys.executable}"


def test_copy_hook_preserves_non_env_python_shebang(tmp_path: Path) -> None:
    """Already-pinned shebangs (absolute python path) stay as-is."""
    src = tmp_path / "hook.py"
    src.write_text("#!/usr/local/bin/python3.14\nprint('hi')\n")
    dst = tmp_path / "out.py"

    _copy_hook(src, dst, dry_run=False)

    assert dst.read_text().splitlines()[0] == "#!/usr/local/bin/python3.14"


def test_copy_hook_preserves_non_python_shebang(tmp_path: Path) -> None:
    """Bash / sh / posix shell hooks are untouched."""
    src = tmp_path / "hook.sh"
    src.write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    dst = tmp_path / "out.sh"

    _copy_hook(src, dst, dry_run=False)

    assert dst.read_text().splitlines()[0] == "#!/usr/bin/env bash"


def test_copy_hook_preserves_body_content(tmp_path: Path) -> None:
    """Only the first line changes; rest of file is byte-for-byte identical."""
    src = tmp_path / "hook.py"
    body = "import yadgar.paths\n\nprint('body 🦣 line')\nprint('utf-8 ok')\n"
    src.write_text("#!/usr/bin/env python3\n" + body)
    dst = tmp_path / "out.py"

    _copy_hook(src, dst, dry_run=False)

    assert dst.read_text() == f"#!{sys.executable}\n{body}"


def test_copy_hook_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "hook.py"
    src.write_text("#!/usr/bin/env python3\n")
    dst = tmp_path / "out.py"

    _copy_hook(src, dst, dry_run=True)

    assert not dst.exists()


def test_copy_hook_missing_source_is_silent(tmp_path: Path) -> None:
    """Match the pre-existing contract: missing source is a no-op, not error."""
    src = tmp_path / "missing.py"
    dst = tmp_path / "out.py"

    _copy_hook(src, dst, dry_run=False)

    assert not dst.exists()
