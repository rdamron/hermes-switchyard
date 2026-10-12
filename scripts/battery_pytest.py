#!/usr/bin/env python3
"""Launch pytest with the Hermes runtime environment active (host seams importable).

Used by the Makefile's ``test-host-seams`` target. The Hermes store python
(``~/.hermes/tools/python-3.14*/bin/python3``) carries no third-party packages
of its own; this activates the committed Hermes dependency environment the same
way hermes entry points do (``hermes_bootstrap`` + ``pm.environments``), then
loads pytest from the battery venv built for that interpreter. The six
host-seam e2e tests (Hermes secret scope, provider registry, approval floor)
only run under this interpreter; everywhere else they skip cleanly.

Environment:
    HERMES_ROOT          Hermes checkout to activate (default ~/.hermes/hermes-agent)
    BATTERY_PYTEST_SITE  absolute path to a site-packages containing pytest,
                         compatible with the store python (required when the
                         store python has no pytest of its own)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERMES_ROOT = Path(
    os.environ.get("HERMES_ROOT", Path.home() / ".hermes" / "hermes-agent")
).resolve()
BATTERY_PYTEST_SITE = os.environ.get("BATTERY_PYTEST_SITE", "")


def main(argv: list[str]) -> int:
    if (HERMES_ROOT / "hermes_bootstrap.py").is_file():
        sys.path.insert(0, str(HERMES_ROOT))
        import hermes_bootstrap  # noqa: F401  (entry-point parity: stdio/import hardening)

        from pm.environments import activate_dependencies

        activate_dependencies(HERMES_ROOT)
        # activate_dependencies() rewrites sys.path (dropping plain site-packages
        # entries), so the battery venv goes in after it, behind the Hermes
        # checkout and the dependency env so nothing there is shadowed.
        if BATTERY_PYTEST_SITE:
            sys.path.insert(2, BATTERY_PYTEST_SITE)
    import pytest

    return pytest.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
