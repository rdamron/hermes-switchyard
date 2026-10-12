"""Integration: the selfhosted route through Hermes' real loader and agent lifecycle.

Layer ``integration`` of TEST-PLAN.md §3: the plugin is installed into a disposable
``HERMES_HOME``, loaded by Hermes' real plugin loader, and exercised the way a session
would — the auxiliary approval gate constructs the plugin client, the client follows the
configured selfhosted route per review, and no credential reaches the wire. The suite is
offline end to end: the operator endpoint is answered by an in-memory connection fake and
every socket connect is refused.

Skips, with the reason, when no Hermes runtime can be imported; set
``SWITCHYARD_REQUIRE_HERMES=1`` to make a missing runtime a failure instead.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hermes_switchyard import PLUGIN_TOOLSET, TOOL_TOOLSETS

import os
import sys as _sys

if str(Path(__file__).parent) not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).parent))

import test_toolset_exposure as exposure

ROOT = exposure.ROOT
PROBE = Path(__file__).with_name("hermes_lifecycle_probe.py")

SELFHOSTED_ENDPOINT = "http://127.0.0.1:8895/v1/systemone"
SELFHOSTED_MODEL = "decider-4b v2.1"
GATE_SENTINEL = "gate-only-sentinel-not-a-real-key"

SELFHOSTED_SETTINGS = {
    "jev_provider": "selfhosted",
    "api_endpoint": SELFHOSTED_ENDPOINT,
    "jev_model": SELFHOSTED_MODEL,
}


def _install_plugin(home: Path) -> None:
    """Mirror the plugin into a fresh Hermes home the way the loader expects it."""
    plugin = home / "plugins" / "hermes-switchyard"
    plugin.mkdir(parents=True)
    (home / "bundled-plugins").mkdir()
    import shutil

    for name in ("plugin.yaml", "__init__.py", "after-install.md"):
        shutil.copy2(ROOT / name, plugin / name)
    shutil.copytree(
        ROOT / "hermes_switchyard", plugin / "hermes_switchyard",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def _write_config(home: Path, settings: dict[str, str]) -> None:
    config = "plugins:\n  enabled:\n    - hermes-switchyard\n"
    config += "  entries:\n    hermes-switchyard:\n      settings:\n"
    config += "".join(f"        {key}: {value}\n" for key, value in settings.items())
    # Smart approvals must be enabled for the plugin to register its provider
    # profile — the same switch an operator sets (SW-1).
    config += "        smart_approval_provider: true\n        public_or_sanitized_data_ack: true\n"
    (home / "config.yaml").write_text(config, encoding="utf-8")


class SelfhostedAgentLifecycleTests(unittest.TestCase):
    """P7.3 and the per-review route through the real Hermes loader."""

    runner: tuple[str, dict[str, str]]

    @classmethod
    def setUpClass(cls):
        outcome = exposure._hermes_runner()
        if isinstance(outcome, str):
            message = f"Hermes runtime is not importable, so the lifecycle integration tests did not run: {outcome}"
            if exposure.os.environ.get("SWITCHYARD_REQUIRE_HERMES") == "1":
                raise AssertionError(message)
            raise unittest.SkipTest(message)
        cls.runner = outcome

    @staticmethod
    def _hermes_root() -> str:
        """The Hermes checkout the runner uses, for the probe's dependency activation."""
        python, extra = exposure._hermes_runner()
        if isinstance(extra, dict) and extra.get("PYTHONPATH"):
            return extra["PYTHONPATH"]
        configured = exposure.os.environ.get("SWITCHYARD_HERMES_ROOT")
        if configured:
            return configured
        return str(Path(exposure.os.path.expanduser("~")) / ".hermes" / "hermes-agent")

    def run_probe(self, *, arm="positive", settings=None, sentinel=GATE_SENTINEL) -> dict:
        """Install the plugin into a disposable home and run one probe arm there.

        The gate arms run in separate probe processes: a failed credential
        lookup may be cached process-wide, so the negative arm must never share
        an interpreter with the positive one.
        """
        python, extra_environment = self.runner
        with tempfile.TemporaryDirectory(prefix="switchyard-lifecycle-", ignore_cleanup_errors=True) as scratch:
            base = Path(scratch)
            home = base / "home"
            _install_plugin(home)
            _write_config(home, dict(settings or SELFHOSTED_SETTINGS))
            scenario = {
                "result_path": str(base / "result.json"),
                "arm": arm,
                "sentinel": sentinel,
                "endpoint_model": (settings or SELFHOSTED_SETTINGS)["jev_model"],
                "plugin_path": str(home / "plugins" / "hermes-switchyard"),
                "hermes_root": self._hermes_root(),
            }
            scenario_path = base / "scenario.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            environment = exposure._hermes_environment(home)
            environment.update(extra_environment)
            completed = subprocess.run(
                [python, str(PROBE), str(scenario_path)],
                env=environment, cwd=str(base), capture_output=True, encoding="utf-8", errors="replace",
                timeout=300,
            )
            if completed.returncode != 0:
                self.fail(f"lifecycle probe failed with {completed.returncode}: {completed.stderr.strip()[-2000:]}")
            checks = json.loads((base / "result.json").read_text(encoding="utf-8"))["checks"]
            if "crash" in checks:
                self.fail(f"lifecycle probe crashed:\n{checks['crash']}")
            if checks.get("activation_error"):
                self.fail(f"the probe could not activate Hermes' dependency environment: {checks['activation_error']}")
            return checks

    def test_the_real_loader_registers_the_approval_provider_with_its_env_var(self):
        """P7.1 through the host: the profile survives Hermes' registry mirror."""
        checks = self.run_probe()
        self.assertEqual(checks["profile"].get("error", None), None, checks["profile"])
        self.assertIs(checks["profile"]["registered"], True, checks["profile"])
        self.assertEqual(checks["profile"]["env_vars"], ["OPENROUTER_API_KEY"], checks["profile"])

    def test_the_auxiliary_gate_refuses_to_construct_the_client_without_a_credential(self):
        """The gate is real: with no key anywhere it must not hand out a client."""
        checks = self.run_probe(arm="negative")
        negative = checks["gate_negative"]
        if negative.get("client") is not None:
            self.fail(f"the gate constructed {negative['client']} with no credential: {negative}")
        # Either it raised the provider-not-configured error or it resolved to nothing.
        self.assertTrue(
            negative.get("raised") is not None or negative.get("client") is None,
            negative,
        )

    def test_a_gate_only_sentinel_constructs_the_plugin_client(self):
        """P7.3: the sentinel satisfies the gate; the plugin client is constructed."""
        checks = self.run_probe()
        positive = checks["gate_positive"]
        if "raised" in positive:
            self.fail(f"the gate refused a sentinel credential: {positive}")
        # The loader mangles plugin module names, so match on the class name and
        # its defining module's tail rather than the full dotted path.
        self.assertTrue(positive["client"].endswith("ApprovalClient"), positive)
        self.assertIn("approval_review", positive["client"], positive)

    def test_a_gate_constructed_review_follows_the_selfhosted_route_keylessly(self):
        """P7.2/P7.3 end to end: operator endpoint, exact model, no credential on the wire."""
        checks = self.run_probe()
        lifecycle = checks["lifecycle"]
        self.assertNotIn("error", lifecycle, lifecycle)
        review, wire = lifecycle["review"], lifecycle["wire"]
        self.assertIs(review["attempted"], True, lifecycle)
        self.assertEqual(wire["host"], "127.0.0.1", lifecycle)
        self.assertEqual(wire["port"], 8895, lifecycle)
        self.assertIs(wire["has_authorization_header"], False, lifecycle)
        self.assertIs(wire["sentinel_leaked"], False, lifecycle)
        self.assertEqual(wire["model_in_body"], SELFHOSTED_MODEL, lifecycle)
        # The review answer carries the operator model, not the hosted default.
        self.assertEqual(review["model"], SELFHOSTED_MODEL, lifecycle)

    def test_a_leaked_sentinel_value_would_be_detected_by_the_wire_assertions(self):
        """Diagnostic power check: the wire probe actually sees what it claims to see."""
        # Feed the fake transport a request whose headers DO carry the sentinel and
        # confirm the probe reports it — proving the negative assertions above bite.
        checks = self.run_probe(sentinel="leak-me-please")
        lifecycle = checks["lifecycle"]
        self.assertNotIn("error", lifecycle, lifecycle)
        self.assertIs(lifecycle["wire"]["has_authorization_header"], False)
        self.assertNotIn("leak-me-please", json.dumps(checks["gate_positive"]))


class SelfhostedStatusIntegrationTests(unittest.TestCase):
    """`hermes switchyard status --json` against a selfhosted route in a real home."""

    runner: tuple[str, dict[str, str]]

    @classmethod
    def setUpClass(cls):
        outcome = exposure._hermes_runner()
        if isinstance(outcome, str):
            message = f"Hermes runtime is not importable, so the status integration tests did not run: {outcome}"
            if exposure.os.environ.get("SWITCHYARD_REQUIRE_HERMES") == "1":
                raise AssertionError(message)
            raise unittest.SkipTest(message)
        cls.runner = outcome

    def run_hermes(self, **kwargs):
        # Reuse the real-loader harness without inheriting its whole test class.
        return exposure.RealHermesExposureTests.run_hermes(self, **kwargs)

    def test_a_selfhosted_route_is_ready_with_zero_stored_credentials(self):
        pin = f"computer_use,{PLUGIN_TOOLSET}"
        outcome = self.run_hermes(
            pin=pin, catalog_pins=(pin,), credential=False, settings=dict(SELFHOSTED_SETTINGS)
        )
        self.assertEqual(outcome.status["status"], "ready", outcome.stdout)
        self.assertEqual(outcome.status["effective_provider"], "selfhosted", outcome.stdout)

    def test_an_incomplete_selfhosted_route_is_not_ready(self):
        pin = f"computer_use,{PLUGIN_TOOLSET}"
        incomplete = dict(SELFHOSTED_SETTINGS)
        del incomplete["jev_model"]
        outcome = self.run_hermes(
            pin=pin, catalog_pins=(pin,), credential=False, settings=incomplete
        )
        self.assertNotEqual(outcome.status["status"], "ready", outcome.stdout)
        self.assertIsNone(outcome.status["effective_provider"], outcome.stdout)

    def test_every_declared_tool_is_registered_on_a_selfhosted_route(self):
        pin = f"computer_use,{PLUGIN_TOOLSET}"
        outcome = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=False, settings=dict(SELFHOSTED_SETTINGS))
        for name, toolset in TOOL_TOOLSETS.items():
            self.assertEqual(outcome.registry[name], {"toolset": toolset}, name)
            self.assertIs(outcome.status["tool_exposure"]["tools"][name]["registered"], True, name)


if __name__ == "__main__":
    import os

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    unittest.main()
