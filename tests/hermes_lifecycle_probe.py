"""Probe run inside a Hermes-capable interpreter by tests/test_selfhosted_agent_lifecycle.py.

The parent test starts this script with a disposable Hermes home configured for a
selfhosted Jev route. It loads the plugin through Hermes' real loader, then exercises
the auxiliary approval gate (``agent.auxiliary_client.resolve_provider_client``) and
the full review round-trip of the client the gate constructs — all offline, with the
network refused and the operator endpoint answered by an in-memory connection fake.
Every outcome is written to the result file; the probe never asserts, so the parent
test owns the diagnostics.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path


def _deny_network(*_args, **_kwargs):
    raise OSError("network access is disabled in this probe")


def _type_name(obj):
    return None if obj is None else f"{type(obj).__module__}.{type(obj).__name__}"


def _activate_hermes_dependencies(hermes_root: str) -> str | None:
    """Activate Hermes' PM dependency environment the way hermes_bootstrap does.

    The managed interpreter the checkout relaunches into carries no third-party
    dependencies of its own; without this activation, ``import model_tools``
    dies on the first dependency import (e.g. ruamel) no matter how correct
    the plugin is.
    """
    try:
        sys.path.insert(0, hermes_root)
        from pm.environments import activate_dependencies

        activate_dependencies(Path(hermes_root))
        return None
    except Exception as exc:  # noqa: BLE001 - record, never crash the probe
        return f"{type(exc).__name__}: {exc}"


class _FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200
        self.reason = "fixture"
        self.headers = {}
        self.will_close = False

    def read(self, size=None):
        return self.body if size is None else self.body[:size]

    def close(self):
        pass


_MESSAGES = [
    {
        "role": "system",
        "content": "You are a security reviewer for an AI coding agent. Assess the command below.",
    },
    {"role": "user", "content": "<command>\nls -la\n</command>"},
]


def main() -> int:
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = {"checks": {}, "arm": scenario.get("arm")}
    result_path = Path(scenario["result_path"])
    try:
        _run(scenario, result)
    except Exception:
        import traceback

        result["crash"] = traceback.format_exc()[-3000:]
    finally:
        result_path.write_text(json.dumps(result), encoding="utf-8")
    return 0


def _run(scenario, result):
    socket.create_connection = _deny_network
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network

    result["activation_error"] = _activate_hermes_dependencies(scenario["hermes_root"])

    # The real loader discovers and registers the plugin, including the
    # switchyard-approvals provider profile, during these imports.
    import model_tools  # noqa: F401
    from hermes_cli.plugins import discover_plugins

    discover_plugins()

    from providers import get_provider_profile

    try:
        profile = get_provider_profile("switchyard-approvals")
        result["checks"]["profile"] = {
            "registered": profile is not None,
            "env_vars": list(profile.env_vars) if profile is not None else None,
        }
    except Exception as exc:  # noqa: BLE001 - record, never crash the probe
        result["checks"]["profile"] = {"error": f"{type(exc).__name__}: {exc}"}

    import agent.secret_scope as secret_scope

    from agent.auxiliary_client import resolve_provider_client

    arm = scenario.get("arm", "positive")

    if arm == "negative":
        # Gate, negative arm: no credential anywhere -> the gate must refuse to
        # construct the plugin client rather than silently handing out a client.
        secret_scope.get_secret = lambda name, *args, **kwargs: ""
        try:
            negative_client, negative_model = resolve_provider_client("switchyard-approvals")
            result["checks"]["gate_negative"] = {
                "client": _type_name(negative_client),
                "model": negative_model,
            }
        except Exception as exc:  # noqa: BLE001
            result["checks"]["gate_negative"] = {"raised": type(exc).__name__, "message": str(exc)[:300]}
        return

    # Gate, positive arm: a gate-only sentinel credential is present (the SW-1
    # contract). Its VALUE must never reach the wire — the wire assertions below
    # prove that independently of what the gate was handed.
    sentinel = scenario["sentinel"]

    def _sentinel_secret(name, *args, **kwargs):
        return sentinel if name == "OPENROUTER_API_KEY" else ""

    secret_scope.get_secret = _sentinel_secret
    try:
        client, resolved_model = resolve_provider_client("switchyard-approvals")
        result["checks"]["gate_positive"] = {
            "client": _type_name(client),
            "resolved_model": resolved_model,
        }
    except Exception as exc:  # noqa: BLE001
        result["checks"]["gate_positive"] = {"raised": type(exc).__name__, "message": str(exc)[:300]}
        return
    if client is None:
        return

    # Agent lifecycle: a full review round-trip through the client the gate
    # constructed, against the operator endpoint answered by the fake transport.
    record = {}
    # Reach the transport through the constructed client's own code globals:
    # the loader mangles plugin module names, so a import-by-name could patch
    # the wrong copy.
    create_fn = getattr(type(client).create, "__func__", type(client).create)
    decision_client = create_fn.__globals__["DecisionClient"]
    post_fn = getattr(decision_client._post_attempt, "__func__", decision_client._post_attempt)
    http_module = post_fn.__globals__["http"]

    class _FakeConnection:
        def __init__(self, host, port=None, timeout=None, **_kwargs):
            record["host"] = host
            record["port"] = port

        def request(self, method, path, body=None, headers=None):
            record.setdefault("requests", []).append(
                {"method": method, "path": path, "headers": dict(headers or {}), "body": body}
            )
            try:
                payload = json.loads(body or b"{}")
            except (ValueError, TypeError):
                payload = {}
            questions = payload.get("questions") or {}
            answers = {}
            for name, question in questions.items():
                if question.get("type") == "choice":
                    answers[name] = {
                        "choice": "APPROVE",
                        "probabilities": {"APPROVE": 0.9, "DENY": 0.05, "ESCALATE": 0.05},
                        "confidence": 0.9,
                    }
                else:
                    # "safe" is a positive noul (true = safe); the risk
                    # questions are negative nouls (true = risky).
                    answers[name] = {"noul": 0.9 if name == "safe" else 0.05}
            self._response = _FakeResponse(
                json.dumps({"model": scenario["endpoint_model"], "answers": answers, "usage": {}}).encode()
            )

        def getresponse(self):
            return self._response

        def close(self):
            pass

    http_module.client.HTTPConnection = _FakeConnection
    http_module.client.HTTPSConnection = _FakeConnection
    try:
        response = client.create(messages=_MESSAGES, model="typesafe/jev-1.13")
        review = getattr(response, "switchyard_review", None)
        requests = record.get("requests", [])
        headers = requests[0]["headers"] if requests else {}
        body = json.loads(requests[0]["body"]) if requests and requests[0]["body"] else {}
        result["checks"]["lifecycle"] = {
            "review": {
                "attempted": review.get("review_attempted") if review else None,
                "reason": review.get("reason") if review else None,
                "verdict": getattr(response.choices[0].message, "content", None),
                "model": getattr(response, "model", None),
            },
            "wire": {
                "host": record.get("host"),
                "port": record.get("port"),
                "has_authorization_header": any(k.lower() == "authorization" for k in headers),
                "sentinel_leaked": sentinel in json.dumps(headers) or sentinel in json.dumps(body),
                "model_in_body": body.get("model"),
            },
        }
    except Exception as exc:  # noqa: BLE001
        import traceback

        result["checks"]["lifecycle"] = {
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        }


if __name__ == "__main__":
    raise SystemExit(main())
