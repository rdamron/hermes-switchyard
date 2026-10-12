# Unified test runner for the hermes-switchyard selfhosted battery.
#
# Layers (markers, assigned in tests/conftest.py per TEST-PLAN.md §3):
#   unit         tests/test_selfhosted_jev.py (+ the selfhosted case in test_switchyard.py)
#   integration  tests/test_selfhosted_agent_lifecycle.py, tests/test_selfhosted_gate_construction.py
#   e2e          tests/test_selfhosted_e2e.py
#
#   make test            one command: the whole battery (unit + integration + e2e
#                        + the host-seam scenarios under the Hermes runtime python
#                        + portability). Writes .pytest-battery/results/.
#   make test-unit       fast unit-only mode.
#   make test-integration / test-e2e   single layers (python3.11).
#   make test-host-seams the six host-seam scenarios that need Hermes imports
#                        (they skip on python3.11; this runs them for real).
#   make portability     scripts/check_portability.py hygiene gate.
#   make test-clean      remove the battery venvs and results.
#
# Everything is offline and deterministic. The live operator-server contract
# (scripts/ci/live_jev_contract.py) stays manual/CI-gated and is NOT part of
# the battery.

HERMES_ROOT ?= $(HOME)/.hermes/hermes-agent
HERMES_PY := $(shell ls $(HOME)/.hermes/tools/python-3.14*/bin/python3 2>/dev/null | head -n 1)
PY311 ?= $(shell command -v $(HOME)/.local/bin/python3.11 2>/dev/null || command -v python3.11 2>/dev/null || echo python3)

BATTERY_DIR := $(CURDIR)/.pytest-battery
VENV311 := $(BATTERY_DIR)/venv-py311
VENV314 := $(BATTERY_DIR)/venv-py314
RESULTS := $(BATTERY_DIR)/results

# rootdir is tests/ (which has no __init__.py): the repo root is itself a
# package (the Hermes plugin entrypoint __init__.py), and pytest would import
# it as a Package node and fail. Config lives in tests/pytest.ini.
PYTESTINI := $(CURDIR)/tests/pytest.ini

PYTEST311 := $(VENV311)/bin/pytest -c $(PYTESTINI)
PYTEST314 := $(HERMES_PY) $(CURDIR)/scripts/battery_pytest.py

COV311 := --cov=hermes_switchyard --cov-report=term-missing
# Host-seam run: coverage of the plugin only (Hermes core is not ours to measure).
COV314 := --cov=hermes_switchyard --cov-report=term-missing

.PHONY: test test-unit test-integration test-e2e test-host-seams portability test-clean venvs

venvs: $(VENV311)/.ready $(VENV314)/.ready

test: test-unit test-integration test-e2e test-host-seams portability
	@echo "=== FULL BATTERY COMPLETE — results in $(RESULTS) ==="

# --- fast unit-only mode -------------------------------------------------
# Explicit file lists: collection must not walk the legacy suites (they have
# pre-existing collection/import quirks and are outside battery scope).
test-unit: venvs
	@mkdir -p $(RESULTS)
	$(PYTEST311) tests/test_selfhosted_jev.py $(COV311) --junitxml=$(RESULTS)/unit-py311.xml -v 2>&1 | tee $(RESULTS)/unit-py311.log
	$(PYTEST311) tests/test_switchyard.py -k selfhosted -v 2>&1 | tee $(RESULTS)/unit-switchyard-selfhosted-py311.log

test-integration: venvs
	@mkdir -p $(RESULTS)
	$(PYTEST311) tests/test_selfhosted_agent_lifecycle.py tests/test_selfhosted_gate_construction.py $(COV311) --junitxml=$(RESULTS)/integration-py311.xml -v 2>&1 | tee $(RESULTS)/integration-py311.log

test-e2e: venvs
	@mkdir -p $(RESULTS)
	$(PYTEST311) tests/test_selfhosted_e2e.py $(COV311) --junitxml=$(RESULTS)/e2e-py311.xml -v 2>&1 | tee $(RESULTS)/e2e-py311.log

# --- host-seam scenarios (Hermes runtime python + dependency env) ---------
test-host-seams:
	@if [ -z "$(HERMES_PY)" ]; then \
		echo "SKIP test-host-seams: Hermes runtime python (tools/python-3.14*) not found"; \
	else $(MAKE) venvs; \
		mkdir -p $(RESULTS); \
		BATTERY_PYTEST_SITE="$(VENV314)/lib/python3.14/site-packages" HERMES_ROOT="$(HERMES_ROOT)" \
		$(PYTEST314) -c $(PYTESTINI) tests/test_selfhosted_jev.py tests/test_selfhosted_agent_lifecycle.py \
			tests/test_selfhosted_gate_construction.py tests/test_selfhosted_e2e.py \
			$(COV314) --junitxml=$(RESULTS)/host-seams-py314.xml -v 2>&1 | tee $(RESULTS)/host-seams-py314.log; \
	fi

portability: venvs
	$(VENV311)/bin/python scripts/check_portability.py

test-clean:
	rm -rf $(BATTERY_DIR)

# --- battery venvs (gitignored, rebuilt by `make venvs`) -------------------
$(VENV311)/.ready:
	$(PY311) -m venv $(VENV311)
	$(VENV311)/bin/pip install -q pytest pytest-cov
	@touch $@

$(VENV314)/.ready:
	@test -n "$(HERMES_PY)" || { echo "No Hermes runtime python found"; exit 1; }
	$(HERMES_PY) -m venv $(VENV314)
	$(VENV314)/bin/pip install -q pytest pytest-cov
	@touch $@
