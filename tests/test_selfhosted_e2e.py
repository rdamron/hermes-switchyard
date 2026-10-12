"""End-to-end tests for the selfhosted (keyless) Jev route's user-facing promises.

These tests treat the whole plugin as a black box: the real ``register()`` runs
against the duck-typed host context every registration test uses, the real
``DecisionClient`` performs real HTTP over a loopback socket, and the real
tool handlers and approval review path produce the observable outcomes an
agent sees. The only seams stubbed are host-owned external services — the
secret store (``_secret``) and the provider registry
(``providers.register_provider``) — never plugin internals.

The operator's Jev server is a synthetic in-process HTTP server on
127.0.0.1:0 that records every request it receives (headers + body), so
wire-level promises (no ``Authorization`` header, exact model echo, no
sentinel credential on the wire) are observed server-side, exactly as the
TEST-PLAN.md e2e layer requires — deterministically and offline.

Coverage map (TEST-PLAN.md §2 promises -> scenario):
- P1.1/P1.2  test_p1_selfhosted_is_a_first_class_provider_for_registered_tools
- P2.1       test_p2_1_api_endpoint_alone_routes_the_tool_over_the_operator_server
- P2.2       test_p2_2_selfhosted_without_endpoint_fails_closed_with_no_wire_request
- P2.3       test_p2_3_selfhosted_without_jev_model_fails_closed_with_no_wire_request
- P2.4       test_p2_4_conflicting_provider_and_endpoint_fails_closed_with_no_wire_request
- P2.5       test_p2_5_non_http_endpoint_fails_closed_with_no_wire_request
- P2.6       test_p2_6_unknown_provider_fails_closed_with_no_wire_request
- P2.7       test_p2_7_free_form_operator_model_round_trips_exactly (with P4.5 happy path)
- P2.8       test_p2_8_hosted_route_without_credential_never_touches_the_operator_server
- P2.9       test_p2_9_invalid_route_hides_availability_instead_of_raising
- P3.1/P3.2  test_p3_selfhosted_route_is_ready_with_zero_stored_credentials
- P3.3       test_p2_8_... (hosted route refuses to run keyless; see below)
- P4.1/P4.4/P4.7  test_p4_plain_http_operator_server_serves_the_full_decision_round_trip
- P4.3       test_p4_3_no_authorization_header_reaches_the_operator_server
- P4.5       test_p4_5_wrong_model_echo_refuses_the_decision
- P4.6       test_p4_6_redirect_from_the_operator_server_fails_closed
- P5.1       test_p5_1_choice_extras_are_dropped_end_to_end
- P5.2       test_p5_2_score_extras_are_dropped_end_to_end
- P5.4       test_p5_4_missing_required_field_is_refused_end_to_end
- P6.1/P6.2/P6.7/P7.1  test_p6_1_approval_review_round_trips_on_the_operator_route_keylessly
- P6.3       test_p6_3_unresolvable_route_escalates_without_contacting_any_server
- P6.4       test_p6_4_foreign_approval_base_url_is_refused_at_construction
- P6.5       test_p6_5_hosted_approval_route_still_requires_its_credential_and_alias
- P6.6       test_p6_6_the_route_resolver_runs_per_review_so_config_changes_are_honored
- P7.2       test_p7_2_a_stored_sentinel_credential_never_reaches_the_operator_wire
- P8.1       test_p8_the_documented_selfhosted_config_surface_drives_a_working_route

Hosted-wire negative promises (byte-for-byte hosted behavior: P5.3 strict
hosted answer fields, P6.5 hosted wire content) are enforced at the unit
layer (tests/test_selfhosted_jev.py) because the two hosted endpoints are
fixed https constants and an offline, deterministic e2e run cannot — and
must not — reach them. Their e2e-observable config-level outcomes (no
keyless hosted run, no operator-server contact) are covered by
test_p2_8_... and test_p6_5_....

P7.3 (real Hermes loader + headless auxiliary gate in a disposable
HERMES_HOME) is the integration layer per TEST-PLAN.md §3 and is exercised
there, not here.
"""
from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import hermes_switchyard

REPO_ROOT = Path(__file__).resolve().parents[1]

SELFHOSTED_MODEL = "decider-4b v2.1"  # free-form operator slug with a space
SENTINEL_CREDENTIAL = "sentinel-gate-only-not-a-real-key"

_QUESTIONS = {"answer": {"type": "noul", "instructions": "Is it true?"}}

_RESPONDER = Callable[[str, dict, bytes], "tuple[int, dict]"]


def _approval_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a security reviewer for an AI coding agent."},
        {"role": "user", "content": "review\n<command>\necho hi\n</command>"},
    ]


def _hermes_secret_seam_available() -> bool:
    try:
        import agent.secret_scope  # noqa: F401
    except Exception:
        return False
    return True


def _hermes_provider_registry_available() -> bool:
    try:
        import providers  # noqa: F401
    except Exception:
        return False
    return True


def _hermes_approval_floor_available() -> bool:
    try:
        from tools import approval  # noqa: F401
    except Exception:
        return False
    return True


class _OperatorServer:
    """A synthetic operator-run Jev server that records every request.

    ``responder(path, headers, body) -> (status, payload_dict)`` is swapped per
    scenario. Requests are recorded server-side, which is the observable
    evidence for the wire promises (headers, model echo, credential absence).
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responder: _RESPONDER | None = None
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
                status, payload = outer.responder(self.path, dict(self.headers.items()), body)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def log_message(self, *_args, **_kwargs) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1/systemone"

    def request_count(self) -> int:
        return len(self.requests)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _echo_responder(status: int = 200):
    """Answer every question with its expected type and echo the model exactly."""

    def responder(_path, _headers, body):
        payload = json.loads(body)
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.9}
            else:  # pragma: no cover - specific responders handle choice/score
                raise AssertionError(f"unexpected question type {question['type']}")
        return status, {"model": payload["model"], "answers": answers, "usage": {"input_tokens": 3}}

    return responder


def _approve_review_responder(_path, _headers, body):
    """Answer the six approval questions so review_command returns APPROVE."""
    payload = json.loads(body)
    noul_values = {
        "safe": 0.97,
        "reads_secrets": 0.0,
        "sends_outbound": 0.0,
        "irreversible": 0.0,
        "self_advocating": 0.0,
    }
    answers = {}
    for name, question in payload["questions"].items():
        if question["type"] == "choice":
            criteria = question["criteria"]
            top = "APPROVE" if "APPROVE" in criteria else sorted(criteria)[0]
            share = (1.0 - 0.97) / max(len(criteria) - 1, 1)
            probabilities = {c: (0.97 if c == top else share) for c in criteria}
            answers[name] = {
                "type": "choice",
                "choice": top,
                "probabilities": probabilities,
                "confidence": 0.97,
                "x_calibration": {"operator": "extra"},
            }
        else:
            answers[name] = {"type": "noul", "noul": noul_values.get(name, 0.0), "x_operator_extra": True}
    return 200, {"model": payload["model"], "answers": answers, "usage": {"input_tokens": 12, "output_tokens": 4}}


class _Context:
    """Minimal duck-typed Hermes plugin context (same seam as the unit suite)."""

    def __init__(self, settings):
        self.settings = dict(settings)
        self.tools = {}
        self.checks = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_auxiliary_task(self, *_args, **_kwargs):
        pass

    def register_tool(self, *, name, handler, check_fn=None, **_kwargs):
        self.tools[name] = handler
        self.checks[name] = check_fn

    def register_skill(self, *_args, **_kwargs):
        pass

    def register_hook(self, *_args, **_kwargs):
        pass


def _empty_secret_store(provider: str) -> str:
    """Host secret-store fixture: no credentials stored at all."""
    return ""


class SelfhostedE2ETestCase(unittest.TestCase):
    def setUp(self):
        hermes_switchyard.reset_runtime_status()
        self.servers: list[_OperatorServer] = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        hermes_switchyard.reset_runtime_status()

    def add_server(self, responder: _RESPONDER) -> _OperatorServer:
        server = _OperatorServer()
        server.responder = responder
        self.servers.append(server)
        return server

    def register_plugin(self, settings):
        context = _Context(settings)
        patcher = mock.patch.object(hermes_switchyard, "_secret", side_effect=_empty_secret_store)
        patcher.start()  # host secret-store fixture stays empty for the whole scenario
        self.addCleanup(patcher.stop)
        hermes_switchyard.register(context)
        return context

    def assess(self, context, *, questions=None, state="public"):
        return json.loads(
            context.tools["jev_assess"](
                {
                    "state": state,
                    "questions": _QUESTIONS if questions is None else questions,
                    "public_or_sanitized_data_ack": True,
                }
            )
        )


class SelfhostedRouteE2ETests(SelfhostedE2ETestCase):
    """Agent-visible route behavior: a selfhosted route serves decisions or fails closed."""

    def test_p1_selfhosted_is_a_first_class_provider_for_registered_tools(self):
        """Verifies P1.1/P1.2: selfhosted is a first-class provider, and with it
        configured the plugin's decision tools report available instead of erroring."""
        self.assertEqual(
            hermes_switchyard.PROVIDERS, ("typesafe", "openrouter", "selfhosted")
        )
        server = self.add_server(_echo_responder())
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        self.assertTrue(context.checks["jev_assess"]())
        result = self.assess(context)
        self.assertNotIn("error", result)

    def test_p2_1_api_endpoint_alone_routes_the_tool_over_the_operator_server(self):
        """Verifies P2.1: an absolute http(s) api_endpoint with no explicit
        provider selects the selfhosted route and serves a real decision."""
        server = self.add_server(_echo_responder())
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context)
        self.assertNotIn("error", result)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertEqual(server.request_count(), 1)
        sent = json.loads(server.requests[0]["body"])
        self.assertEqual(sent["model"], SELFHOSTED_MODEL)

    def test_p2_2_selfhosted_without_endpoint_fails_closed_with_no_wire_request(self):
        """Verifies P2.2: jev_provider selfhosted without an api_endpoint is a
        structured tool error, not a crash and not a hosted call."""
        context = self.register_plugin({"jev_provider": "selfhosted"})
        result = self.assess(context)
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_p2_3_selfhosted_without_jev_model_fails_closed_with_no_wire_request(self):
        """Verifies P2.3: a selfhosted endpoint without jev_model fails closed."""
        server = self.add_server(_echo_responder())
        context = self.register_plugin({"api_endpoint": server.url})
        result = self.assess(context)
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.request_count(), 0)

    def test_p2_4_conflicting_provider_and_endpoint_fails_closed_with_no_wire_request(self):
        """Verifies P2.4: a provider/endpoint mismatch is refused before any
        request, in both hosted/selfhosted directions."""
        server = self.add_server(_echo_responder())
        for settings in (
            {"jev_provider": "selfhosted", "api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL},
        ):
            # sanity: the selfhosted pair itself is valid and does wire up
            context = self.register_plugin(settings)
            result = self.assess(context)
            self.assertNotIn("error", result)
        server = self.servers[0]
        baseline = server.request_count()
        for settings in (
            {"jev_provider": "selfhosted", "api_endpoint": "https://openrouter.ai/api/alpha/decisions", "jev_model": SELFHOSTED_MODEL},
            {"jev_provider": "openrouter", "api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL},
            {"jev_provider": "typesafe", "api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL},
        ):
            with self.subTest(settings=settings):
                context = self.register_plugin(settings)
                result = self.assess(context)
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.request_count(), baseline)

    def test_p2_5_non_http_endpoint_fails_closed_with_no_wire_request(self):
        """Verifies P2.5: endpoints that are neither hosted constants nor
        absolute http(s) URLs are refused with no request anywhere."""
        for endpoint in ("ftp://operator.example/v1/systemone", "not a url", "http://"):
            with self.subTest(endpoint=endpoint):
                context = self.register_plugin({"api_endpoint": endpoint, "jev_model": SELFHOSTED_MODEL})
                result = self.assess(context)
                self.assertEqual(result["error"]["code"], "invalid_request")

    def test_p2_6_unknown_provider_fails_closed_with_no_wire_request(self):
        """Verifies P2.6: an unknown jev_provider string is a structured error."""
        context = self.register_plugin({"jev_provider": "self-hosted"})
        result = self.assess(context)
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_p2_7_free_form_operator_model_round_trips_exactly(self):
        """Verifies P2.7 (and P4.5's happy path): the operator's free-form model
        slug is sent as configured and the server's exact echo is required."""
        server = self.add_server(_echo_responder())
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context)
        self.assertNotIn("error", result)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertEqual(json.loads(server.requests[0]["body"])["model"], SELFHOSTED_MODEL)

    def test_p2_8_hosted_route_without_credential_never_touches_the_operator_server(self):
        """Verifies P2.8/P3.3: a hosted route with no stored key fails closed
        (credential required) and never egresses — the operator server sees
        nothing, proving hosted routes are unchanged and not redirected."""
        server = self.add_server(_echo_responder())
        for provider in ("openrouter", "typesafe"):
            with self.subTest(provider=provider):
                context = self.register_plugin({"jev_provider": provider})
                result = self.assess(context)
                self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(server.request_count(), 0)

    def test_p2_9_invalid_route_hides_availability_instead_of_raising(self):
        """Verifies P2.9: with an unresolvable route the tool's availability
        check reports False and the tool answers with a structured error."""
        context = self.register_plugin({"jev_provider": "selfhosted"})
        self.assertFalse(context.checks["jev_assess"]())
        result = self.assess(context)
        self.assertEqual(result["error"]["code"], "invalid_request")


class SelfhostedKeylessWireE2ETests(SelfhostedE2ETestCase):
    """Server-side observation of the keyless wire contract."""

    def test_p3_selfhosted_route_is_ready_with_zero_stored_credentials(self):
        """Verifies P3.1/P3.2: a selfhosted route is credential-ready with an
        empty store — the real seam returns falsy without lookups and a full
        decision round-trip succeeds with no credential anywhere."""
        if not _hermes_secret_seam_available():
            self.skipTest("Hermes secret-scope seam is not importable")
        # The real seam, consulted directly: selfhosted holds no store entry.
        self.assertEqual(hermes_switchyard._secret("selfhosted"), "")
        server = self.add_server(_echo_responder())
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context)
        self.assertNotIn("error", result)
        self.assertEqual(server.request_count(), 1)
        self.assertNotIn("Authorization", server.requests[0]["headers"])

    def test_p4_plain_http_operator_server_serves_the_full_decision_round_trip(self):
        """Verifies P4.1/P4.4/P4.7: a plaintext http operator endpoint is a
        valid selfhosted route and the whole typed round trip completes over
        the plain connection class (a TLS-only client could not talk to it)."""
        server = self.add_server(_echo_responder())
        self.assertTrue(server.url.startswith("http://"))
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context)
        self.assertNotIn("error", result)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertEqual(result["answers"]["answer"], {"noul": 0.9})
        self.assertEqual(server.request_count(), 1)
        sent_headers = {key.lower(): value for key, value in server.requests[0]["headers"].items()}
        self.assertEqual(sent_headers["content-type"], "application/json")

    def test_p4_3_no_authorization_header_reaches_the_operator_server(self):
        """Verifies P4.3: the selfhosted wire carries no Authorization header at
        all — not a bearer token, not an empty one — observed server-side."""
        server = self.add_server(_echo_responder())
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        for _ in range(3):  # repeated real usage, not just the first request
            result = self.assess(context)
            self.assertNotIn("error", result)
        self.assertEqual(server.request_count(), 3)
        for captured in server.requests:
            self.assertNotIn("Authorization", captured["headers"])
            self.assertNotIn("authorization", {key.lower() for key in captured["headers"]})

    def test_p4_5_wrong_model_echo_refuses_the_decision(self):
        """Verifies P4.5: a server that echoes any model other than the
        configured one has its answer refused — the agent sees an error."""
        server = self.add_server(_echo_responder())
        server.responder = lambda _p, _h, body: (
            200,
            {"model": "not-the-configured-model", "answers": {"answer": {"noul": 0.9}}, "usage": {}},
        )
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context)
        self.assertEqual(result["error"]["code"], "invalid_request")

    def test_p4_6_redirect_from_the_operator_server_fails_closed(self):
        """Verifies P4.6: a 3xx from the operator endpoint is rejected (fail
        closed) instead of followed — the agent sees a structured error."""
        server = self.add_server(_echo_responder())
        server.responder = lambda _p, _h, _b: (302, {"no": "redirects"})
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context)
        self.assertEqual(result["status"], "error")
        self.assertEqual(server.request_count(), 1)  # seen, not followed


class SelfhostedTolerantAnswersE2ETests(SelfhostedE2ETestCase):
    """Operator extra fields on answers, observed through the real tool."""

    _CHOICE_QUESTIONS = {
        "answer": {
            "type": "choice",
            "instructions": "Pick one.",
            "criteria": {"safe": "the safe choice", "unsafe": "the unsafe choice"},
        }
    }
    _SCORE_QUESTIONS = {
        "answer": {
            "type": "score",
            "instructions": "Rate it.",
            "criteria": ["poor", "good"],
        }
    }

    def test_p5_1_choice_extras_are_dropped_end_to_end(self):
        """Verifies P5.1: choice answers carrying operator extras are accepted;
        the agent sees exactly choice/probabilities/confidence."""
        server = self.add_server(lambda _p, _h, _b: (200, {}))

        def responder(_path, _headers, body):
            payload = json.loads(body)
            answers = {
                name: {
                    "type": "choice",
                    "choice": "safe",
                    "probabilities": {"safe": 0.77, "unsafe": 0.23},
                    "confidence": 0.55,
                    "x_p_max": 0.99,
                    "certainty": 0.2,
                }
                for name in payload["questions"]
            }
            return 200, {"model": payload["model"], "answers": answers, "usage": {}}

        server.responder = responder
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context, questions=self._CHOICE_QUESTIONS)
        self.assertNotIn("error", result)
        self.assertEqual(
            result["answers"]["answer"],
            {"choice": "safe", "probabilities": {"safe": 0.77, "unsafe": 0.23}, "confidence": 0.55},
        )

    def test_p5_2_score_extras_are_dropped_end_to_end(self):
        """Verifies P5.2: score answers carrying operator extras are accepted;
        the agent sees exactly score/legend/probabilities/confidence."""
        server = self.add_server(lambda _p, _h, _b: (200, {}))

        def responder(_path, _headers, body):
            payload = json.loads(body)
            answers = {
                name: {
                    "type": "score",
                    "score": 0.9,
                    "legend": {"0": "poor", "1": "good"},
                    "probabilities": {"0": 0.1, "1": 0.9},
                    "confidence": 0.9,
                    "x_calibrated": True,
                }
                for name in payload["questions"]
            }
            return 200, {"model": payload["model"], "answers": answers, "usage": {}}

        server.responder = responder
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context, questions=self._SCORE_QUESTIONS)
        self.assertNotIn("error", result)
        self.assertEqual(
            result["answers"]["answer"],
            {"score": 0.9, "legend": {"0": "poor", "1": "good"}, "probabilities": {"0": 0.1, "1": 0.9}, "confidence": 0.9},
        )

    def test_p5_4_missing_required_field_is_refused_end_to_end(self):
        """Verifies P5.4: tolerance adds fields, it never removes required ones
        — a choice answer without probabilities is refused."""
        server = self.add_server(lambda _p, _h, _b: (200, {}))

        def responder(_path, _headers, body):
            payload = json.loads(body)
            answers = {
                name: {"type": "choice", "choice": "safe", "confidence": 0.55}
                for name in payload["questions"]
            }
            return 200, {"model": payload["model"], "answers": answers, "usage": {}}

        server.responder = responder
        context = self.register_plugin({"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL})
        result = self.assess(context, questions=self._CHOICE_QUESTIONS)
        self.assertEqual(result["error"]["code"], "invalid_request")


class SelfhostedApprovalE2ETests(SelfhostedE2ETestCase):
    """switchyard-approvals follows the live selfhosted route, per review."""

    def _register_with_approval_provider(self, settings):
        if not _hermes_provider_registry_available():
            self.skipTest("Hermes provider registry is not importable")
        captured = {}

        def _capture(profile):
            captured["profile"] = profile

        context = _Context({**settings, "smart_approval_provider": True, "public_or_sanitized_data_ack": True})
        with mock.patch("providers.register_provider", _capture), \
                mock.patch.object(hermes_switchyard, "_secret", side_effect=_empty_secret_store):
            hermes_switchyard.register(context)
        return context, captured["profile"]

    def test_p6_1_approval_review_round_trips_on_the_operator_route_keylessly(self):
        """Verifies P6.1/P6.2/P6.7/P7.1: the registry-threaded approval client
        reviews on the operator endpoint with an empty credential even when the
        host mirrors a credential in, the operator model is used, the real
        review path returns its verdict, and OPENROUTER_API_KEY stays declared
        for the host's auxiliary gate."""
        if not _hermes_approval_floor_available():
            self.skipTest("Hermes native approval floor is not importable")
        server = self.add_server(_approve_review_responder)
        _context, profile = self._register_with_approval_provider(
            {"api_endpoint": server.url, "jev_model": SELFHOSTED_MODEL}
        )
        self.assertEqual(profile.env_vars, ("OPENROUTER_API_KEY",))
        client = profile.create_client(api_key="host-mirror-credential", timeout=2.0)
        try:
            response = client.create(
                messages=_approval_messages(), model="typesafe/jev-1.13", timeout=2.0
            )
        finally:
            client.close()
        self.assertEqual(response.choices[0].message.content, "APPROVE")
        self.assertEqual(server.request_count(), 1)
        captured = server.requests[0]
        self.assertNotIn("Authorization", captured["headers"])
        sent = json.loads(captured["body"])
        self.assertEqual(sent["model"], SELFHOSTED_MODEL)

    def test_p6_3_unresolvable_route_escalates_without_contacting_any_server(self):
        """Verifies P6.3: a route that cannot resolve raises out of the review
        (escalates) and no request is sent anywhere — no hosted fallback."""
        if not _hermes_provider_registry_available():
            self.skipTest("Hermes provider registry is not importable")
        server = self.add_server(_approve_review_responder)
        _context, profile = self._register_with_approval_provider(
            {"api_endpoint": server.url}  # missing jev_model: unresolvable
        )
        client = profile.create_client(timeout=2.0)
        with self.assertRaises(ValueError):
            client.create(messages=_approval_messages(), model="typesafe/jev-1.13", timeout=2.0)
        self.assertEqual(server.request_count(), 0)

    def test_p6_4_foreign_approval_base_url_is_refused_at_construction(self):
        """Verifies P6.4: the approval client still refuses foreign base URLs;
        routing happens only through the live route resolver."""
        from hermes_switchyard.approval_review import ApprovalClient

        with self.assertRaises(ValueError):
            ApprovalClient(
                enabled=lambda: True,
                base_url="http://operator.example/v1/systemone",
                route=lambda: ("http://operator.example/v1/systemone", SELFHOSTED_MODEL, "selfhosted"),
            )

    def test_p6_5_hosted_approval_route_still_requires_its_credential_and_alias(self):
        """Verifies P6.5: a hosted-route review without a stored credential, or
        with a non-hosted model alias, fails closed before any request — and
        the operator server sees nothing."""
        if not _hermes_provider_registry_available():
            self.skipTest("Hermes provider registry is not importable")
        server = self.add_server(_approve_review_responder)
        from hermes_switchyard import approval_review

        def hosted_route():
            return ("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "openrouter")

        client = approval_review.ApprovalClient(enabled=lambda: True, route=hosted_route)
        with self.assertRaises(ValueError):
            client.create(messages=_approval_messages(), model="typesafe/jev-1.13", timeout=2.0)
        # A hosted route may only run the hosted aliases; a foreign alias is refused.
        with self.assertRaises(ValueError):
            client.create(messages=_approval_messages(), model="guard/local-alias", timeout=2.0)
        self.assertEqual(server.request_count(), 0)

    def test_p6_6_the_route_resolver_runs_per_review_so_config_changes_are_honored(self):
        """Verifies P6.6: the route is resolved on every review, not cached at
        construction — reconfiguring between reviews redirects the next review
        to the newly configured operator server."""
        if not _hermes_provider_registry_available():
            self.skipTest("Hermes provider registry is not importable")
        if not _hermes_approval_floor_available():
            self.skipTest("Hermes native approval floor is not importable")
        first = self.add_server(_approve_review_responder)
        second = self.add_server(_approve_review_responder)
        context, profile = self._register_with_approval_provider(
            {"api_endpoint": first.url, "jev_model": SELFHOSTED_MODEL}
        )
        client = profile.create_client(timeout=2.0)
        try:
            client.create(messages=_approval_messages(), model="typesafe/jev-1.13", timeout=2.0)
            context.settings["api_endpoint"] = second.url
            client.create(messages=_approval_messages(), model="typesafe/jev-1.13", timeout=2.0)
        finally:
            client.close()
        self.assertEqual(first.request_count(), 1)
        self.assertEqual(second.request_count(), 1)


class SelfhostedCredentialHygieneE2ETests(SelfhostedE2ETestCase):
    """P7.2: the gate-only sentinel credential never reaches the operator wire."""

    def test_p7_2_a_stored_sentinel_credential_never_reaches_the_operator_wire(self):
        """Verifies P7.2: with a sentinel OPENROUTER_API_KEY in the store, no
        wire request on the selfhosted route carries it — header or body — for
        decisions or approval reviews."""
        if not _hermes_provider_registry_available():
            self.skipTest("Hermes provider registry is not importable")
        if not _hermes_approval_floor_available():
            self.skipTest("Hermes native approval floor is not importable")
        # One configured route serves both decisions and approval reviews; the
        # generic responder answers every question type and echoes the model.
        server = self.add_server(_approve_review_responder)

        def _sentinel_store(provider: str) -> str:
            return {"selfhosted": "", "openrouter": SENTINEL_CREDENTIAL, "typesafe": SENTINEL_CREDENTIAL}.get(
                provider, ""
            )

        with mock.patch.object(hermes_switchyard, "_secret", side_effect=_sentinel_store):
            hermes_switchyard.reset_runtime_status()
            context = _Context(
                {
                    "api_endpoint": server.url,
                    "jev_model": SELFHOSTED_MODEL,
                    "smart_approval_provider": True,
                    "public_or_sanitized_data_ack": True,
                }
            )
            captured = {}
            with mock.patch("providers.register_provider", lambda profile: captured.setdefault("profile", profile)):
                hermes_switchyard.register(context)
            result = json.loads(
                context.tools["jev_assess"](
                    {"state": "public", "questions": _QUESTIONS, "public_or_sanitized_data_ack": True}
                )
            )
            self.assertNotIn("error", result)
            approval_client = captured["profile"].create_client(api_key=SENTINEL_CREDENTIAL, timeout=2.0)
            try:
                approval_client.create(
                    messages=_approval_messages(), model="typesafe/jev-1.13", timeout=2.0
                )
            finally:
                approval_client.close()

        self.assertGreaterEqual(server.request_count(), 2)
        for captured_request in server.requests:
            self.assertNotIn("Authorization", captured_request["headers"])
            self.assertNotIn(
                "authorization", {key.lower() for key in captured_request["headers"]}
            )
            self.assertNotIn(
                SENTINEL_CREDENTIAL.encode(), captured_request["body"]
            )


class SelfhostedDocsE2ETests(SelfhostedE2ETestCase):
    """P8: the documented configuration surface actually drives a working route."""

    def test_p8_the_documented_selfhosted_config_surface_drives_a_working_route(self):
        """Verifies P8.1 (and P8.2's operator-network promise in behavior): the
        manifest documents the selfhosted keys, and applying exactly those
        documented keys serves a real decision that never leaves the network
        the operator runs (here: the in-process loopback server)."""
        manifest = (REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("also accepts typesafe, openrouter, or selfhosted", manifest)
        self.assertIn("any absolute http(s) URL for a selfhosted operator-run server", manifest)
        self.assertIn("a selfhosted route has no default and requires the model the operator server echoes", manifest)

        server = self.add_server(_echo_responder())
        # Exactly the documented configuration keys, no more.
        context = self.register_plugin(
            {
                "jev_provider": "selfhosted",
                "api_endpoint": server.url,
                "jev_model": SELFHOSTED_MODEL,
            }
        )
        result = self.assess(context)
        self.assertNotIn("error", result)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertEqual(server.request_count(), 1)
        sent = json.loads(server.requests[0]["body"])
        self.assertEqual(sent["model"], SELFHOSTED_MODEL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
