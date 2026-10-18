"""The backend image's DECLARED dependency set must carry greenlet.

Backend 5.87.3 crashlooped: engine #2's migration imports
``sqlalchemy.ext.asyncio``, which raises ImportError without ``greenlet``.
SQLAlchemy 2.0.x pulled greenlet in as a plain dependency on x86_64/aarch64;
2.1.x does not — it now comes only through the ``asyncio`` extra.
Dockerfile.backend resolves with an unpinned ``pip install "/app[<extras>]"``
(uv.lock is not used there), so the image picked up 2.1.x and lost greenlet.

An import test cannot catch this: the dev venv installs from uv.lock, which
still pins a 2.0.x SQLAlchemy that drags greenlet in. So the test asserts on
what the image actually installs — the extras named in Dockerfile.backend's
pip line — and requires greenlet to be declared there explicitly, either as
``sqlalchemy[asyncio]`` or as ``greenlet`` itself.
"""

import re
import tomllib

from packaging.requirements import Requirement

from yadgar.tests._paths import REPO_ROOT

DOCKERFILE = REPO_ROOT / "Dockerfile.backend"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _backend_image_extras() -> list[str]:
    text = DOCKERFILE.read_text()
    match = re.search(r'pip install\s+"/app\[([^\]]+)\]"', text)
    assert match, 'Dockerfile.backend no longer installs "/app[<extras>]" — update this test'
    return [e.strip() for e in match.group(1).split(",")]


def _backend_image_requirements() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text())
    project = data["project"]
    reqs = [Requirement(r) for r in project["dependencies"]]
    for extra in _backend_image_extras():
        reqs += [Requirement(r) for r in project["optional-dependencies"][extra]]
    return reqs


def test_backend_image_installs_sqlalchemy() -> None:
    names = {r.name.lower() for r in _backend_image_requirements()}
    assert "sqlalchemy" in names, "backend image no longer installs sqlalchemy"


def test_backend_image_declares_greenlet_for_sqlalchemy_asyncio() -> None:
    reqs = _backend_image_requirements()
    declares_greenlet = any(r.name.lower() == "greenlet" for r in reqs) or any(
        r.name.lower() == "sqlalchemy" and "asyncio" in r.extras for r in reqs
    )
    assert declares_greenlet, (
        "Backend image must declare greenlet (via sqlalchemy[asyncio] or greenlet "
        "itself): sqlalchemy.ext.asyncio raises ImportError without it, and "
        "SQLAlchemy >=2.1 no longer installs it by default. Extras installed: "
        f"{_backend_image_extras()}"
    )
