"""Assign TEST-PLAN.md §3 layer markers to the selfhosted battery files.

Markers are assigned here — one mapping table instead of edits scattered
across test modules — so pytest selection (``-m unit``, ``-m integration``,
``-m e2e``) works without touching files other workstreams may be editing.

Out of battery scope: the legacy suites (e.g. tests/test_switchyard.py,
tests/test_toolset_exposure.py) get no marker; the Makefile selects the
single selfhosted case from test_switchyard.py explicitly.
"""

import sys
from pathlib import Path

import pytest

# Plugin repo root on sys.path so `import hermes_switchyard` works regardless
# of which interpreter/directory pytest was launched from.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# test file (basename) -> layer marker
LAYER_BY_FILE = {
    "test_selfhosted_jev.py": "unit",
    "test_selfhosted_agent_lifecycle.py": "integration",
    "test_selfhosted_gate_construction.py": "integration",
    "test_selfhosted_e2e.py": "e2e",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        filename = item.nodeid.replace("\\", "/").rsplit("/", 1)[-1].split("::")[0]
        marker = LAYER_BY_FILE.get(filename)
        if marker:
            item.add_marker(getattr(pytest.mark, marker))
