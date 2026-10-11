"""Selfhosted Jev routes: operator-run endpoints, keyless calls, model passthrough.

These tests stay offline. Transport assertions use in-memory connection fakes
and never reach the network; route assertions use the same duck-typed plugin
context the other registration tests use.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

import hermes_switchyard
from hermes_switchyard import client as client_module
from hermes_switchyard.client import DecisionClient, _selfhosted_endpoint, _valid_model_slug

SELFHOSTED_ENDPOINT = "http://localhost:8895/v1/systemone"
SELFHOSTED_MODEL = "decider-4b-v2.1"
_QUESTIONS = {"answer": {"type": "noul", "instructions": "Is it true?"}}


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.reason = "fixture"
        self.headers = {}
        self.will_close = False

    def read(self, size: int | None = None) -> bytes:
        return self.body if size is None else self.body[:size]

    def close(self) -> None:
        pass


class _FakeConnection:
    """In-memory connection recording what the client sent on the wire."""

    bodies: list[bytes] = []
    headers: list[dict] = []
    response_body: bytes = b"{}"

    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.sock = None

    def request(self, _method, _path, body=None, headers=None):
        type(self).bodies.append(body or b"")
        type(self).headers.append(dict(headers or {}))

    def getresponse(self):
        return _FakeResponse(type(self).response_body)

    def close(self):
        self.closed = True


def _install_fake_connection(test_case, connection_cls):
    https_factory = mock.Mock(side_effect=AssertionError("TLS class used on an http endpoint"))
    patcher = mock.patch.object(client_module.http.client, "HTTPConnection", connection_cls), \
        mock.patch.object(client_module.http.client, "HTTPSConnection", https_factory)
    for entry in patcher:
        entry.start()
    test_case.addCleanup(patcher[0].stop)
    test_case.addCleanup(patcher[1].stop)


class SelfhostedEndpointPredicateTests(unittest.TestCase):
    def test_absolute_http_and_https_urls_qualify(self):
        self.assertTrue(_selfhosted_endpoint("http://localhost:8895/v1/systemone"))
        self.assertTrue(_selfhosted_endpoint("https://gateway.internal.example/v1/systemone"))

    def test_hosted_fixed_endpoints_are_never_selfhosted(self):
        from hermes_switchyard.client import DEFAULT_ENDPOINT, TYPESAFE_ENDPOINT

        self.assertFalse(_selfhosted_endpoint(DEFAULT_ENDPOINT))
        self.assertFalse(_selfhosted_endpoint(TYPESAFE_ENDPOINT))

    def test_malformed_values_do_not_qualify(self):
        from hermes_switchyard.client import DEFAULT_ENDPOINT

        for value in (None, 7, "", "not a url", "ftp://localhost/v1/systemone", "localhost:8895", DEFAULT_ENDPOINT):
            with self.subTest(value=value):
                self.assertFalse(_selfhosted_endpoint(value))


class SelfhostedClientEndpointTests(unittest.TestCase):
    def test_selfhosted_endpoint_and_operator_model_are_accepted(self):
        client = DecisionClient(
            api_key="test-key",
            endpoint=SELFHOSTED_ENDPOINT,
            model=SELFHOSTED_MODEL,
        )
        self.assertEqual(client.endpoint, SELFHOSTED_ENDPOINT)
        self.assertEqual(client.model, SELFHOSTED_MODEL)
        client.close()

    def test_unroutable_scheme_and_hostless_values_are_refused(self):
        for endpoint in ("ftp://localhost/v1/systemone", "localhost:8895"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    DecisionClient(
                        api_key="test-key",
                        endpoint=endpoint,
                        model=SELFHOSTED_MODEL,
                    )

    def test_selfhosted_model_slug_must_be_bounded_and_printable(self):
        self.assertTrue(_valid_model_slug(SELFHOSTED_MODEL, SELFHOSTED_ENDPOINT))
        self.assertTrue(_valid_model_slug("m" * 128, SELFHOSTED_ENDPOINT))
        self.assertFalse(_valid_model_slug("m" * 129, SELFHOSTED_ENDPOINT))
        self.assertFalse(_valid_model_slug(" padded", SELFHOSTED_ENDPOINT))
        self.assertFalse(_valid_model_slug("bad\x00slug", SELFHOSTED_ENDPOINT))

    def test_plaintext_selfhosted_endpoint_uses_the_http_connection_class(self):
        class _RecordingConnection(_FakeConnection):
            bodies: list = []
            headers: list = []

        _RecordingConnection.response_body = json.dumps(
            {"model": SELFHOSTED_MODEL, "answers": {"answer": {"noul": 0.9}}, "usage": {}}
        ).encode()
        _install_fake_connection(self, _RecordingConnection)
        client = DecisionClient(
            api_key="test-key",
            endpoint=SELFHOSTED_ENDPOINT,
            model=SELFHOSTED_MODEL,
        )
        result = client.decide("public", _QUESTIONS, public_or_sanitized_data_ack=True)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertEqual(len(_RecordingConnection.headers), 1)
        client.close()

    def test_hosted_endpoints_still_use_the_tls_connection_class(self):
        from hermes_switchyard.client import DEFAULT_ENDPOINT

        https_factory = mock.Mock()
        with mock.patch.object(client_module.http.client, "HTTPSConnection", https_factory), \
                mock.patch.object(
                    client_module.http.client,
                    "HTTPConnection",
                    side_effect=AssertionError("plaintext class used on a hosted endpoint"),
                ):
            client = DecisionClient(
                api_key="fixture",
                endpoint=DEFAULT_ENDPOINT,
                model="typesafe/jev-1.13",
            )
            connection = https_factory.return_value
            connection.request = mock.Mock()
            connection.getresponse = mock.Mock(
                return_value=_FakeResponse(
                    b'{"model":"typesafe/jev-1.13","answers":{"answer":{"noul":0.9}},"usage":{}}'
                )
            )
            result = client.decide("public", _QUESTIONS, public_or_sanitized_data_ack=True)
        self.assertEqual(result["model"], "typesafe/jev-1.13")
        self.assertEqual(https_factory.call_count, 1)
        client.close()


class _CaptureClient:
    """DecisionClient stand-in that records construction kwargs per call."""

    instances: list["_CaptureClient"] = []

    def __init__(self, *, api_key, endpoint, model, **_kwargs):
        self.kwargs = {"api_key": api_key, "endpoint": endpoint, "model": model}
        self.closed = False
        _CaptureClient.instances.append(self)

    def decide(self, _state, questions, *, public_or_sanitized_data_ack=False):
        return {
            "model": self.kwargs["model"],
            "answers": {name: {"noul": 0.9} for name in questions},
            "usage": {},
        }

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_auxiliary_task(self, *_args, **_kwargs):
        pass

    def register_tool(self, *, name, handler, **_kwargs):
        self.tools[name] = handler

    def register_skill(self, *_args, **_kwargs):
        pass

    def register_hook(self, *_args, **_kwargs):
        pass


def _register_and_assess(settings):
    """Register the plugin with captured clients and run one jev_assess call.

    Patches stay active for the tool call, as the route is resolved per call.
    """
    _CaptureClient.instances = []
    context = _Context(settings)
    with mock.patch.object(hermes_switchyard, "DecisionClient", _CaptureClient), \
            mock.patch.object(hermes_switchyard, "_secret", side_effect=lambda _provider: ""):
        hermes_switchyard.register(context)
        result = _assess(context)
    return result, context


def _register_with_capture(settings):
    _CaptureClient.instances = []
    context = _Context(settings)
    with mock.patch.object(hermes_switchyard, "DecisionClient", _CaptureClient), \
            mock.patch.object(hermes_switchyard, "_secret", side_effect=lambda _provider: ""):
        hermes_switchyard.register(context)
    return context


def _assess(context):
    return json.loads(context.tools["jev_assess"]({
        "state": "public",
        "questions": _QUESTIONS,
        "public_or_sanitized_data_ack": True,
    }))


class SelfhostedRouteTests(unittest.TestCase):
    def test_api_endpoint_alone_selects_the_selfhosted_route(self):
        result, _context = _register_and_assess({
            "api_endpoint": SELFHOSTED_ENDPOINT,
            "jev_model": SELFHOSTED_MODEL,
        })
        self.assertNotIn("error", result)
        self.assertEqual(_CaptureClient.instances[-1].kwargs, {
            "api_key": "",
            "endpoint": SELFHOSTED_ENDPOINT,
            "model": SELFHOSTED_MODEL,
        })

    def test_explicit_selfhosted_provider_requires_an_explicit_endpoint(self):
        result, _context = _register_and_assess({"jev_provider": "selfhosted"})
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(_CaptureClient.instances, [])

    def test_selfhosted_route_requires_jev_model(self):
        result, _context = _register_and_assess({"api_endpoint": SELFHOSTED_ENDPOINT})
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(_CaptureClient.instances, [])

    def test_selfhosted_provider_and_hosted_endpoint_conflict(self):
        from hermes_switchyard.client import DEFAULT_ENDPOINT

        result, _context = _register_and_assess({
            "jev_provider": "selfhosted",
            "api_endpoint": DEFAULT_ENDPOINT,
            "jev_model": SELFHOSTED_MODEL,
        })
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(_CaptureClient.instances, [])

    def test_hosted_providers_conflict_with_a_selfhosted_endpoint(self):
        for provider in ("typesafe", "openrouter"):
            with self.subTest(provider=provider):
                result, _context = _register_and_assess({
                    "jev_provider": provider,
                    "api_endpoint": SELFHOSTED_ENDPOINT,
                    "jev_model": SELFHOSTED_MODEL,
                })
                self.assertEqual(result["error"]["code"], "invalid_request")
                self.assertEqual(_CaptureClient.instances, [])

    def test_arbitrary_non_http_endpoint_stays_refused(self):
        result, _context = _register_and_assess({
            "api_endpoint": "ftp://localhost/v1/systemone",
            "jev_model": SELFHOSTED_MODEL,
        })
        self.assertEqual(result["error"]["code"], "invalid_request")
        self.assertEqual(_CaptureClient.instances, [])

    def test_hosted_route_is_unchanged(self):
        result, _context = _register_and_assess({"jev_provider": "openrouter"})
        self.assertNotIn("error", result)
        self.assertEqual(_CaptureClient.instances[-1].kwargs, {
            "api_key": "",
            "endpoint": "https://openrouter.ai/api/alpha/decisions",
            "model": "typesafe/jev-1.13",
        })


class KeylessSelfhostedTests(unittest.TestCase):
    def test_keyless_selfhosted_sends_no_authorization_header(self):
        class _RecordingConnection(_FakeConnection):
            bodies: list = []
            headers: list = []

        _RecordingConnection.response_body = json.dumps(
            {"model": SELFHOSTED_MODEL, "answers": {"answer": {"noul": 0.9}}, "usage": {}}
        ).encode()
        _install_fake_connection(self, _RecordingConnection)
        client = DecisionClient(api_key="", endpoint=SELFHOSTED_ENDPOINT, model=SELFHOSTED_MODEL)
        result = client.decide("public", _QUESTIONS, public_or_sanitized_data_ack=True)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertNotIn("Authorization", _RecordingConnection.headers[0])
        self.assertEqual(_RecordingConnection.headers[0]["Content-Type"], "application/json")
        client.close()

    def test_operator_credential_is_still_forwarded_when_provided(self):
        class _RecordingConnection(_FakeConnection):
            bodies: list = []
            headers: list = []

        _RecordingConnection.response_body = json.dumps(
            {"model": SELFHOSTED_MODEL, "answers": {"answer": {"noul": 0.9}}, "usage": {}}
        ).encode()
        _install_fake_connection(self, _RecordingConnection)
        client = DecisionClient(
            api_key="test-key", endpoint=SELFHOSTED_ENDPOINT, model=SELFHOSTED_MODEL
        )
        client.decide("public", _QUESTIONS, public_or_sanitized_data_ack=True)
        self.assertEqual(_RecordingConnection.headers[0].get("Authorization"), "Bearer test-key")
        client.close()

    def test_empty_credential_stays_required_on_hosted_routes(self):
        from hermes_switchyard.client import DEFAULT_ENDPOINT

        with self.assertRaises(ValueError):
            DecisionClient(api_key="", endpoint=DEFAULT_ENDPOINT, model="typesafe/jev-1.13")

    def test_selfhosted_route_is_ready_without_stored_credentials(self):
        exposure = {"tools": {"jev_assess": {"registered": True, "callable": True}}}
        self.assertEqual(
            hermes_switchyard._overall_status(
                {"typesafe": False, "openrouter": False}, exposure, "selfhosted"
            ),
            "ready",
        )
        self.assertTrue(
            hermes_switchyard._credential_ready({"typesafe": False, "openrouter": False}, "selfhosted")
        )
        self.assertEqual(
            hermes_switchyard._overall_status(
                {"typesafe": False, "openrouter": False}, exposure, "openrouter"
            ),
            "credential_required",
        )


class SelfhostedResponseContractTests(unittest.TestCase):
    """Operator-run servers answer with their own model id and extra typed fields."""

    @staticmethod
    def _operator_transport(payload):
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "choice":
                answers[name] = {
                    "type": "choice",
                    "choice": "safe",
                    "confidence": 0.55,
                    "x_p_max": 0.77,
                    "certainty": 0.23,
                    "probabilities": {"safe": 0.77, "unsafe": 0.23},
                }
            else:
                answers[name] = {"type": question["type"], "noul": 0.9, "x_p_max": 0.77}
        return {"model": payload["model"], "answers": answers, "usage": {"input_tokens": 36, "output_tokens": 0}}

    _CHOICE_QUESTIONS = {
        "answer": {
            "type": "choice",
            "instructions": "Pick one.",
            "criteria": {"safe": "the safe choice", "unsafe": "the unsafe choice"},
        }
    }

    def test_selfhosted_consumes_closed_fields_and_drops_operator_extras(self):
        client = DecisionClient(
            api_key="",
            endpoint=SELFHOSTED_ENDPOINT,
            model=SELFHOSTED_MODEL,
            transport=self._operator_transport,
        )
        result = client.decide("public", self._CHOICE_QUESTIONS, public_or_sanitized_data_ack=True)
        self.assertEqual(result["model"], SELFHOSTED_MODEL)
        self.assertEqual(
            result["answers"]["answer"],
            {"choice": "safe", "probabilities": {"safe": 0.77, "unsafe": 0.23}, "confidence": 0.55},
        )
        client.close()

    def test_selfhosted_requires_the_server_to_echo_the_configured_model(self):
        def wrong_model(payload):
            return {**self._operator_transport(payload), "model": "not-the-configured-model"}

        client = DecisionClient(
            api_key="",
            endpoint=SELFHOSTED_ENDPOINT,
            model=SELFHOSTED_MODEL,
            transport=wrong_model,
        )
        with self.assertRaises(ValueError):
            client.decide("public", self._CHOICE_QUESTIONS, public_or_sanitized_data_ack=True)
        client.close()

    def test_hosted_routes_still_reject_unexpected_answer_fields(self):
        from hermes_switchyard.client import DEFAULT_ENDPOINT

        client = DecisionClient(
            api_key="fixture",
            endpoint=DEFAULT_ENDPOINT,
            model="typesafe/jev-1.13",
            transport=self._operator_transport,
        )
        with self.assertRaises(ValueError):
            client.decide("public", self._CHOICE_QUESTIONS, public_or_sanitized_data_ack=True)
        client.close()


class SelfhostedApprovalTests(unittest.TestCase):
    """switchyard-approvals follows the configured selfhosted route."""

    @staticmethod
    def _messages():
        return [
            {"role": "system", "content": "You are a security reviewer for an AI coding agent."},
            {"role": "user", "content": "review\n<command>\necho hi\n</command>"},
        ]

    @staticmethod
    def _review_row():
        return {
            "verdict": "allow",
            "review_attempted": True,
            "request_count": 1,
            "reason": "fixture",
            "usage": {},
        }

    def test_selfhosted_route_replaces_the_hardcoded_hosted_endpoint(self):
        from hermes_switchyard import approval_review

        captured = {}

        class _FakeDecision:
            def __init__(self, *, api_key, endpoint, model, **_kwargs):
                captured.update(api_key=api_key, endpoint=endpoint, model=model)

            def close(self):
                pass

        def _fake_review(_command, **_kwargs):
            return self._review_row()

        with mock.patch.object(approval_review, "DecisionClient", _FakeDecision), \
                mock.patch.object(approval_review, "review_command", _fake_review):
            approval_client = approval_review.ApprovalClient(
                enabled=lambda: True,
                route=lambda: (SELFHOSTED_ENDPOINT, SELFHOSTED_MODEL, "selfhosted"),
            )
            approval_client.create(
                messages=self._messages(), model="typesafe/jev-1.13", timeout=2.0
            )
        self.assertEqual(captured["endpoint"], SELFHOSTED_ENDPOINT)
        self.assertEqual(captured["model"], SELFHOSTED_MODEL)
        self.assertEqual(captured["api_key"], "")

    def test_hosted_approval_route_is_unchanged(self):
        from hermes_switchyard import approval_review
        from hermes_switchyard.client import DEFAULT_ENDPOINT

        captured = {}

        class _FakeDecision:
            def __init__(self, *, api_key, endpoint, model, **_kwargs):
                captured.update(api_key=api_key, endpoint=endpoint, model=model)

            def close(self):
                pass

        def _fake_review(_command, **_kwargs):
            return self._review_row()

        with mock.patch.object(approval_review, "DecisionClient", _FakeDecision), \
                mock.patch.object(approval_review, "review_command", _fake_review):
            approval_client = approval_review.ApprovalClient(
                api_key="fixture-key", enabled=lambda: True
            )
            approval_client.create(messages=self._messages(), model="typesafe/jev-1.13", timeout=2.0)
        self.assertEqual(captured["endpoint"], DEFAULT_ENDPOINT)
        self.assertEqual(captured["model"], "typesafe/jev-1.13")
        self.assertEqual(captured["api_key"], "fixture-key")

    def test_selfhosted_registration_keeps_the_upstream_env_var_declaration(self):
        """The env var stays declared even on a selfhosted route.

        Hermes' registry mirror silently drops api_key profiles that declare no
        env vars, and its auxiliary gate refuses an empty key before the plugin
        client is ever constructed. The stored value is never sent on a
        selfhosted route — keylessness is enforced on the wire.
        """
        from hermes_switchyard import approval_review

        try:
            import providers  # noqa: F401
        except ImportError:
            self.skipTest("Hermes provider registry is not importable")

        captured = {}

        def _capture(profile):
            captured["profile"] = profile

        with mock.patch("providers.register_provider", _capture):
            approval_review.register_approval_provider(
                lambda: True,
                route=lambda: (SELFHOSTED_ENDPOINT, SELFHOSTED_MODEL, "selfhosted"),
            )
        self.assertEqual(captured["profile"].env_vars, ("OPENROUTER_API_KEY",))

        with mock.patch("providers.register_provider", _capture):
            approval_review.register_approval_provider(
                lambda: True,
                route=lambda: (
                    "https://openrouter.ai/api/alpha/decisions",
                    "typesafe/jev-1.13",
                    "openrouter",
                ),
            )
        self.assertEqual(captured["profile"].env_vars, ("OPENROUTER_API_KEY",))

    def test_plugin_registration_passes_the_selfhosted_route_to_the_registry(self):
        from hermes_switchyard import approval_review

        try:
            import providers  # noqa: F401
        except ImportError:
            self.skipTest("Hermes provider registry is not importable")

        captured = {}

        def _capture(profile):
            captured["profile"] = profile

        context = _Context({
            "api_endpoint": SELFHOSTED_ENDPOINT,
            "jev_model": SELFHOSTED_MODEL,
            "smart_approval_provider": True,
            "public_or_sanitized_data_ack": True,
        })
        with mock.patch("providers.register_provider", _capture), \
                mock.patch.object(hermes_switchyard, "DecisionClient", _CaptureClient), \
                mock.patch.object(hermes_switchyard, "_secret", side_effect=lambda _provider: ""):
            hermes_switchyard.register(context)
        self.assertEqual(captured["profile"].env_vars, ("OPENROUTER_API_KEY",))
        self.assertIsInstance(captured["profile"].create_client(), approval_review.ApprovalClient)

    def test_unresolvable_route_fails_the_review_without_a_hosted_fallback(self):
        """A route that cannot resolve escalates instead of egressing externally."""
        from hermes_switchyard import approval_review

        with mock.patch.object(approval_review, "DecisionClient") as decision, \
                mock.patch.object(approval_review, "review_command"):
            approval_client = approval_review.ApprovalClient(
                enabled=lambda: True,
                route=mock.Mock(side_effect=ValueError("no operator-run api_endpoint")),
            )
            with self.assertRaises(ValueError):
                approval_client.create(
                    messages=self._messages(), model="typesafe/jev-1.13", timeout=2.0
                )
        decision.assert_not_called()

    def test_selfhosted_route_ignores_the_hosted_approval_aliases(self):
        """The host's hosted approval alias does not gate a selfhosted review."""
        from hermes_switchyard import approval_review

        captured = {}

        class _FakeDecision:
            def __init__(self, *, api_key, endpoint, model, **_kwargs):
                captured.update(api_key=api_key, endpoint=endpoint, model=model)

            def close(self):
                pass

        with mock.patch.object(approval_review, "DecisionClient", _FakeDecision), \
                mock.patch.object(
                    approval_review, "review_command", lambda _c, **_kw: self._review_row()
                ):
            approval_client = approval_review.ApprovalClient(
                enabled=lambda: True,
                route=lambda: (SELFHOSTED_ENDPOINT, SELFHOSTED_MODEL, "selfhosted"),
            )
            approval_client.create(
                messages=self._messages(), model="guard/local-alias", timeout=2.0
            )
        self.assertEqual(captured["model"], SELFHOSTED_MODEL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
