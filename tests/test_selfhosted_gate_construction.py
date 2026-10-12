"""P7.3 integration: the sentinel-credential headless auxiliary gate constructs
the keyless plugin client through Hermes' real loader.

TEST-PLAN.md §3 leaves one integration-layer promise open: in a headless
Hermes session (no shell-exported credentials) the auxiliary approval gate
refuses to construct the switchyard-approvals plugin client unless
``OPENROUTER_API_KEY`` is non-empty — the SW-1 fix (card t_118273a8). The
operator answer is the gate-only sentinel documented on wiki
``entities/jev``: ``OPENROUTER_API_KEY=switchyard-gate-selfhosted-only`` —
set through the unified credential lifecycle, never a real key, and never
sent anywhere on a selfhosted route (the plugin enforces ``credential=""``
on the wire).

This module drives the REAL Hermes loader in a disposable ``HERMES_HOME``:
``hermes_cli.plugins.discover_plugins()`` registers the plugin,
``auxiliary.approval`` routes at ``switchyard-approvals``, and the real
``agent.auxiliary_client`` resolver plus the ``call_llm`` ladder run against
a synthetic operator Jev server that records every request server-side. Two
scenarios:

- with the sentinel in the environment, the gate constructs the plugin's
  ``ApprovalClient`` and a full smart-approval round trip lands on the
  operator server with no ``Authorization`` header and no sentinel anywhere
  on the wire (P7.3);
- with no sentinel, the same gate refuses with ``AuxiliaryClientUnavailable``
  — the control proving the happy-path scenario exercises the real gate, not
  a stub.

Like the other host-seam tests (tests/test_toolset_exposure.py real-loader
cases), this module skips cleanly when Hermes is unimportable (set
``SWITCHYARD_REQUIRE_HERMES=1`` to turn a missing runtime into a failure) and
honors ``SWITCHYARD_HERMES_ROOT`` for a source checkout. The disposable home
is torn down with the test; the operator's real profile store is never
touched because the child environment strips every ``HERMES_*`` variable and
re-points ``HERMES_HOME`` at the scratch home.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

SELFHOSTED_MODEL = "decider-4b v2.1"  # free-form operator slug with a space
APPROVAL_MODEL = "typesafe/jev-1.13"
# The documented gate-only sentinel (wiki entities/jev, SW-1). Not a real key:
# Hermes' ``sk-or-`` prefix gate rejects it everywhere else, and the selfhosted
# route never transmits it.
SENTINEL_CREDENTIAL = "switchyard-gate-selfhosted-only"


def _hermes_environment(home: Path) -> dict[str, str]:
    """Return an environment that points Hermes at a disposable home and nothing else."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HERMES_")
        and key not in {"OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment.update(
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(home / "bundled-plugins"),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
        SWITCHYARD_GATE_REPO_ROOT=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    return environment


def _can_import_hermes(python: str, extra_environment: dict[str, str]) -> str | None:
    """Return None when the interpreter imports Hermes, otherwise a short reason."""
    with tempfile.TemporaryDirectory(prefix="switchyard-gate-import-", ignore_cleanup_errors=True) as scratch:
        home = Path(scratch) / "home"
        environment = _hermes_environment(home)
        environment.update(extra_environment)
        try:
            completed = subprocess.run(
                [python, "-c",
                 "import hermes_cli.plugins, hermes_cli.config, providers, agent.auxiliary_client"],
                env=environment,
                cwd=scratch,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__} while starting the interpreter"
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        return f"import failed ({lines[-1] if lines else 'no error output'})"
    return None


_IMPORT_PROBE = (
    "import hermes_cli.plugins, hermes_cli.config, providers, agent.auxiliary_client\n"
)


def _can_import_hermes_script_mode(python: str, extra_environment: dict[str, str]) -> str | None:
    """Script-mode variant for managed Hermes launchers.

    The standalone Hermes runtime python only bootstraps its dependency
    environment (and puts the checkout on ``sys.path``) when it executes a
    script file, so probe through a file like the real child runs will.
    """
    with tempfile.TemporaryDirectory(prefix="switchyard-gate-import-", ignore_cleanup_errors=True) as scratch:
        home = Path(scratch) / "home"
        probe = Path(scratch) / "import_probe.py"
        probe.write_text(_IMPORT_PROBE, encoding="utf-8")
        environment = _hermes_environment(home)
        environment.update(extra_environment)
        try:
            completed = subprocess.run(
                [python, str(probe)],
                env=environment,
                cwd=scratch,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__} while starting the interpreter"
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        return f"import failed ({lines[-1] if lines else 'no error output'})"
    return None


def _managed_runtime_pythons() -> list[Path]:
    """Standalone Hermes runtime pythons shipped under the Hermes home's tools dir."""
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return sorted((home / "tools").glob("python-*/bin/python3"))


def _hermes_source_roots() -> list[Path]:
    """Return candidate Hermes source checkouts: an explicit one, then the default install location."""
    roots = []
    configured = os.environ.get("SWITCHYARD_HERMES_ROOT")
    if configured:
        roots.append(Path(configured))
    home = os.environ.get("HERMES_HOME")
    roots.append((Path(home) if home else Path.home() / ".hermes") / "hermes-agent")
    return roots


def _hermes_runner() -> tuple[str, dict[str, str]] | str:
    """Return (python, extra environment) that can import Hermes, or the reason none can."""
    reasons = []
    problem = _can_import_hermes(sys.executable, {})
    if problem is None:
        return sys.executable, {}
    reasons.append(f"the current interpreter cannot import it: {problem}")
    for root in _hermes_source_roots():
        if not root.is_dir():
            reasons.append("a candidate Hermes source checkout does not exist")
            continue
        # The checkout's own virtual environment, if it has one.
        for folder in (".venv", "venv"):
            for relative in ("bin/python", "Scripts/python.exe"):
                candidate = root / folder / relative
                if not candidate.is_file():
                    continue
                problem = _can_import_hermes(str(candidate), {"PYTHONPATH": str(root)})
                if problem is None:
                    return str(candidate), {"PYTHONPATH": str(root)}
                reasons.append("a Hermes checkout interpreter cannot import it: " + problem)
        # A managed runtime whose launcher does not put the checkout on sys.path
        # (the Hermes standalone python) still works with the root injected.
        problem = _can_import_hermes(sys.executable, {"PYTHONPATH": str(root)})
        if problem is None:
            return sys.executable, {"PYTHONPATH": str(root)}
        reasons.append("the current interpreter cannot import the checkout: " + problem)
    # Managed standalone Hermes runtimes (script-mode launchers that bootstrap
    # their own dependency environment; the checkout rides in via PYTHONPATH).
    for root in _hermes_source_roots():
        if not root.is_dir():
            continue
        for python in _managed_runtime_pythons():
            problem = _can_import_hermes_script_mode(str(python), {"PYTHONPATH": str(root)})
            if problem is None:
                return str(python), {"PYTHONPATH": str(root)}
            reasons.append(f"a managed Hermes runtime cannot import it ({python}): {problem}")
    reasons.append("set SWITCHYARD_HERMES_ROOT to a Hermes source checkout with its own virtual environment")
    return "; ".join(reasons)


_GATE_CHILD = r'''
"""Child process: the real loader + real auxiliary gate in a disposable home.

Prints one JSON verdict line on success; any assertion surfaces via a
non-zero exit and stderr.
"""
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EXPECT = sys.argv[1]  # "construct" or "unavailable"
SENTINEL = "switchyard-gate-selfhosted-only"
SELFHOSTED_MODEL = "decider-4b v2.1"
APPROVAL_MODEL = "typesafe/jev-1.13"
ROOT = Path(os.environ["SWITCHYARD_GATE_REPO_ROOT"])
home = Path(os.environ["HERMES_HOME"])


# -- build the disposable home BEFORE importing hermes ----------------------
# Idempotent: the Hermes runtime wrapper may run dependency-prep passes over
# the data root before executing the script.
plugin = home / "plugins" / "hermes-switchyard"
plugin.mkdir(parents=True, exist_ok=True)
(home / "bundled-plugins").mkdir(exist_ok=True)
for name in ("plugin.yaml", "__init__.py", "after-install.md"):
    target = plugin / name
    if not target.exists():
        shutil.copy2(ROOT / name, target)
if not (plugin / "hermes_switchyard").exists():
    shutil.copytree(
        ROOT / "hermes_switchyard", plugin / "hermes_switchyard",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


class OperatorServer:
    """Synthetic operator-run Jev server recording every request (headers + body)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 10

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                payload = json.loads(body)
                answers = {}
                for name, question in payload["questions"].items():
                    if question["type"] == "choice":
                        criteria = question["criteria"]
                        top = "APPROVE" if "APPROVE" in criteria else sorted(criteria)[0]
                        share = (1.0 - 0.97) / max(len(criteria) - 1, 1)
                        answers[name] = {
                            "type": "choice",
                            "choice": top,
                            "probabilities": {c: (0.97 if c == top else share) for c in criteria},
                            "confidence": 0.97,
                        }
                    else:
                        answers[name] = {"type": "noul", "noul": {"safe": 0.97}.get(name, 0.0)}
                data = json.dumps(
                    {"model": payload["model"], "answers": answers,
                     "usage": {"input_tokens": 12, "output_tokens": 4}}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def log_message(self, *_args, **_kwargs) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1/systemone"


server = OperatorServer()
(home / "config.yaml").write_text(
    "plugins:\n"
    "  enabled:\n"
    "    - hermes-switchyard\n"
    "  entries:\n"
    "    hermes-switchyard:\n"
    "      settings:\n"
    "        smart_approval_provider: true\n"
    "        public_or_sanitized_data_ack: true\n"
    "        jev_provider: selfhosted\n"
    f"        api_endpoint: {server.url}\n"
    f"        jev_model: {SELFHOSTED_MODEL}\n"
    "auxiliary:\n"
    "  approval:\n"
    "    provider: switchyard-approvals\n",
    encoding="utf-8",
)


def _hermes_root() -> str:
    from_env = os.environ.get("SWITCHYARD_HERMES_ROOT")
    if from_env:
        return from_env
    pythonpath = os.environ.get("PYTHONPATH", "")
    for entry in pythonpath.split(os.pathsep):
        if (Path(entry) / "model_tools.py").is_file():
            return entry
    return str((home.parent if home.name == "home" else home) / "hermes-agent")


def _activate_hermes_dependencies(hermes_root: str) -> None:
    """Activate Hermes' PM dependency environment the way hermes_bootstrap does.

    A fresh disposable HERMES_HOME borrows the checkout, and the checkout's
    managed interpreter relaunches into a bare store Python that carries no
    third-party dependencies (the same pitfall tests/hermes_exposure_probe.py
    documents). Without this activation the first dependency import dies
    regardless of plugin correctness.
    """
    root = Path(hermes_root)
    sys.path.insert(0, str(root))
    from pm.environments import activate_dependencies

    activate_dependencies(root)


_activate_hermes_dependencies(_hermes_root())

# -- the real loader + the real auxiliary gate ------------------------------
import hermes_cli.plugins, hermes_cli.config  # noqa: F401,E401
from hermes_cli.plugins import discover_plugins

discover_plugins()
from providers import get_provider_profile  # noqa: E402

profile = get_provider_profile("switchyard-approvals")
assert profile is not None, "the real loader did not register the switchyard-approvals provider"

from agent.auxiliary_client import (  # noqa: E402
    _resolve_task_provider_model,
    AuxiliaryClientUnavailable,
    call_llm,
    resolve_provider_client,
)

provider, model, base_url, api_key, api_mode = _resolve_task_provider_model("approval")
assert provider == "switchyard-approvals", (provider, "auxiliary.approval did not route at the plugin")

client, final_model = resolve_provider_client(
    provider, model=model or APPROVAL_MODEL, task="approval",
)

if EXPECT == "unavailable":
    refused = client is None
    gate_error = ""
    try:
        call_llm(task="approval", temperature=0, max_tokens=16,
                 messages=[{"role": "user", "content": "x"}])
        gate_error = "call_llm unexpectedly succeeded without the sentinel"
    except AuxiliaryClientUnavailable as exc:
        gate_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - any other failure is the verdict
        gate_error = f"{type(exc).__name__}: {exc}"
    print(json.dumps({
        "ok": refused and "AuxiliaryClientUnavailable" in gate_error,
        "provider": provider,
        "api_key_present": bool(api_key),
        "gate_error": gate_error,
        "request_count": len(server.requests),
    }))
    sys.exit(0)

assert client is not None, "the gate refused to construct the client despite the sentinel"
client_is_plugin_approval_client = (
    type(client).__name__ == "ApprovalClient"
    and "approval_review" in type(client).__module__
)
assert client_is_plugin_approval_client, (type(client), type(client).__module__)

messages = [
    {"role": "system", "content": "You are a security reviewer for an AI coding agent."},
    {"role": "user", "content": "review\n<command>\necho hi\n</command>"},
]

# The registered profile's own client, exactly as Hermes would call it.
response = client.chat.completions.create(messages=messages, model=APPROVAL_MODEL)
direct_verdict = response.choices[0].message.content
assert direct_verdict == "APPROVE", direct_verdict

# The exact call shape tools.approval_smart._smart_approve makes in a headless
# session: through the full auxiliary ladder, not just the raw resolver.
smart_response = call_llm(
    task="approval", temperature=0, max_tokens=16, timeout=8, messages=messages,
)
smart_verdict = (smart_response.choices[0].message.content or "").strip().upper()
assert smart_verdict == "APPROVE", smart_verdict

assert server.requests, "the operator server saw no request"
wire = []
for request in server.requests:
    body_text = request["body"].decode("utf-8", "replace")
    wire.append({
        "has_authorization": "authorization" in {k.lower() for k in request["headers"]},
        "sentinel_in_body": SENTINEL in body_text,
        "model": json.loads(body_text).get("model"),
    })
print(json.dumps({
    "ok": True,
    "provider": provider,
    "final_model": final_model,
    "client_is_plugin_approval_client": client_is_plugin_approval_client,
    "direct_verdict": direct_verdict,
    "smart_verdict": smart_verdict,
    "wire": wire,
    "request_count": len(server.requests),
}))
'''


class SentinelGateConstructionTests(unittest.TestCase):
    """P7.3: real loader, disposable HERMES_HOME, sentinel-only credentials."""

    runner: tuple[str, dict[str, str]]

    @classmethod
    def setUpClass(cls):
        outcome = _hermes_runner()
        if isinstance(outcome, str):
            message = f"Hermes runtime is not importable, so the real-loader gate checks did not run: {outcome}"
            if os.environ.get("SWITCHYARD_REQUIRE_HERMES") == "1":
                raise AssertionError(message)
            raise unittest.SkipTest(message)
        cls.runner = outcome

    def _run_gate_child(self, expect: str, *, with_sentinel: bool) -> dict:
        """Build a disposable home and run the real-loader gate child in it."""
        python, extra_environment = self.runner
        with tempfile.TemporaryDirectory(prefix="switchyard-gate-", ignore_cleanup_errors=True) as scratch:
            base = Path(scratch)
            home = base / "home"
            child = base / "gate_child.py"
            child.write_text(_GATE_CHILD, encoding="utf-8")
            environment = _hermes_environment(home)
            environment.update(extra_environment)
            if with_sentinel:
                environment["OPENROUTER_API_KEY"] = SENTINEL_CREDENTIAL
            try:
                completed = subprocess.run(
                    [python, str(child), expect],
                    env=environment,
                    cwd=base,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=420,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f"gate child ({expect}) timed out: {exc}")
            self.assertEqual(
                completed.returncode, 0,
                f"gate child ({expect}) failed:\n{completed.stdout[-3000:]}\n{completed.stderr[-3000:]}",
            )
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            self.assertTrue(lines, "gate child produced no verdict")
            payload = json.loads(lines[-1])
            self.assertTrue(payload.get("ok"), f"gate child verdict not ok: {payload}")
            # The sentinel lives only in the child's environment: it must never
            # land in the disposable home's own configuration either.
            for name in ("config.yaml", ".env"):
                path = home / name
                if path.is_file():
                    self.assertNotIn(
                        SENTINEL_CREDENTIAL, path.read_text(encoding="utf-8", errors="replace"),
                        f"the sentinel was written to {path}",
                    )
            return payload

    def test_p7_3_the_sentinel_lets_the_real_gate_construct_the_keyless_client(self):
        """Verifies P7.3 (SW-1): with only the gate-only sentinel set, the real
        Hermes loader registers the plugin, the auxiliary approval gate
        constructs its ApprovalClient, and a full smart-approval round trip
        lands on the operator server with no credential of any kind on the
        wire."""
        payload = self._run_gate_child("construct", with_sentinel=True)
        self.assertEqual(payload["provider"], "switchyard-approvals")
        self.assertTrue(payload["client_is_plugin_approval_client"], payload)
        self.assertEqual(payload["direct_verdict"], "APPROVE")
        self.assertEqual(payload["smart_verdict"], "APPROVE")
        self.assertGreaterEqual(payload["request_count"], 1)
        for request in payload["wire"]:
            self.assertFalse(request["has_authorization"], request)
            self.assertFalse(request["sentinel_in_body"], request)
            self.assertEqual(request["model"], SELFHOSTED_MODEL, request)

    def test_p7_3_without_the_sentinel_the_gate_refuses_and_names_the_missing_credential(self):
        """The control: with no credential at all, the same real gate refuses
        with ``AuxiliaryClientUnavailable`` naming ``OPENROUTER_API_KEY`` —
        the exact headless failure SW-1 fixed — and the operator server is
        never contacted."""
        payload = self._run_gate_child("unavailable", with_sentinel=False)
        self.assertFalse(payload["api_key_present"], payload)
        self.assertIn("AuxiliaryClientUnavailable", payload["gate_error"], payload)
        self.assertIn("OPENROUTER_API_KEY", payload["gate_error"], payload)
        self.assertEqual(payload["request_count"], 0, payload)


class RunnerDiscoveryTests(unittest.TestCase):
    def test_a_missing_runtime_yields_an_explicit_skip_not_a_silent_pass(self):
        """No importable Hermes: setUpClass skips with the remediation hint —
        or fails when SWITCHYARD_REQUIRE_HERMES=1, like the exposure tests."""

        class _Probe(SentinelGateConstructionTests):
            runner: tuple[str, dict[str, str]] | None = None  # type: ignore[assignment]

        with mock.patch(f"{__name__}._can_import_hermes", return_value="import failed (nothing installed)"), \
             mock.patch(f"{__name__}._hermes_source_roots", return_value=[]), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SWITCHYARD_REQUIRE_HERMES", None)
            with self.assertRaises(unittest.SkipTest) as skipped:
                _Probe.setUpClass()
            self.assertIn("Hermes runtime is not importable", str(skipped.exception))
            self.assertIn("SWITCHYARD_HERMES_ROOT", str(skipped.exception))
            os.environ["SWITCHYARD_REQUIRE_HERMES"] = "1"
            try:
                with self.assertRaises(AssertionError) as required:
                    _Probe.setUpClass()
                self.assertIn("Hermes runtime is not importable", str(required.exception))
            finally:
                os.environ.pop("SWITCHYARD_REQUIRE_HERMES", None)


if __name__ == "__main__":
    unittest.main()
