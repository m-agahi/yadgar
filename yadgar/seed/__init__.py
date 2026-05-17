"""Project seeding — bootstrap Yadgar memory for existing codebases.

Scans a project directory and creates foundational memories from:
- Project structure and layout
- Config files (package.json, pyproject.toml, Cargo.toml, etc.)
- Documentation (README, ARCHITECTURE, CONTRIBUTING, etc.)
- CI/CD configuration
- Entry points and key source files
"""

from ._generate import seed_project
from ._scan import scan_project

__all__ = ["seed_project", "scan_project"]
