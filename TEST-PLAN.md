# TEST-PLAN.md — hermes-switchyard selfhosted (keyless) modification

**Scope of the modification.** The fork (commit `7c4cc4b` "feat(approvals): follow
the configured selfhosted route keylessly" plus the uncommitted `PROVIDERS`
change in `hermes_switchyard/__init__.py`) adds a third Jev route —
**selfhosted** — alongside the two hosted providers (`typesafe`,
`openrouter`). A selfhosted route is the operator's own keyless Jev server at
any absolute `http(s)` URL. Everything below is the observable behavior that
change introduces or alters.

Note on the search brief: the typo spelling **`swithyard`** does **not** occur
anywhere in this repository (checked `*.py`, `*.md`, `*.yaml`); only the
correct `switchyard` spelling exists. No test is needed for it.

**Upstream-invariant clause:** every feature below also carries a negative
promise that the two hosted routes (`typesafe`, `openrouter` →
`https://openrouter.ai/api/alpha/decisions` and the TypeSafe endpoint) are
byte-for-byte unchanged. Those negative cases are listed per feature and must
never be dropped.

## 1. Feature inventory

| # | Feature | Where it lives |
|---|---------|----------------|
| F1 | `PROVIDERS` public tuple includes `"selfhosted"` | `hermes_switchyard/__init__.py` (`PROVIDERS`) |
| F2 | Route resolution: `jev_provider: selfhosted` and/or a selfhosted `api_endpoint` selects the selfhosted route, with strict conflict/refusal rules | `__init__.py::register()` → local `_route()` |
| F3 | Selfhosted route is keyless in the secret layer | `__init__.py::_secret()`, `_credential_ready()` |
| F4 | `DecisionClient` accepts operator endpoints without a credential, validates a bounded free-form model slug, sends no `Authorization` header, and requires exact model echo | `hermes_switchyard/client.py` (`_selfhosted_endpoint`, `_valid_model_slug`, `DecisionClient.__init__`, `_post_attempt`, `_validate_resolved_model`) |
| F5 | Selfhosted responses tolerate operator extra fields: closed set of consumed answer fields per question type, extras dropped instead of refused | `client.py::_SELFHOSTED_ANSWER_FIELDS` and the answer-validation loop |
| F6 | Smart approvals follow the live route per review: selfhosted reviews on the operator endpoint with empty credential and the operator-configured model; an unresolvable route escalates rather than falling back to the hosted endpoint | `hermes_switchyard/approval_review.py::ApprovalClient.__init__/create`, `register_approval_provider()` |
| F7 | Registry declaration keeps `OPENROUTER_API_KEY` unconditionally (host-auxiliary-gate compatibility), while the value is never sent on a selfhosted route | `approval_review.py::register_approval_provider`; host integration (SW-1) |
| F8 | Manifest/docs surface the new configuration | `plugin.yaml` (`jev_provider`, `api_endpoint`, `jev_model` descriptions), `README.md` |

## 2. Promised behavior — given/when/then

### F1 — PROVIDERS tuple
- **P1.1** Given the module is imported, when `PROVIDERS` is read, then it equals `("typesafe", "openrouter", "selfhosted")` in that order. *(Unit)*
- **P1.2** Given the tuple, when any consumer iterates it (status text, validation), then `selfhosted` is treated as a first-class provider name, not an error. *(Unit)*

### F2 — Route resolution (`_route()` inside `register(ctx)`)
- **P2.1** Given `api_endpoint` set to any absolute http(s) URL with a hostname (and no explicit provider), when the route is resolved, then provider resolves to `selfhosted` with that endpoint. *(Unit)*
- **P2.2** Given `jev_provider: selfhosted` without an explicit `api_endpoint`, when resolved, then `ValueError("selfhosted jev_provider requires an operator-run api_endpoint")`. *(Unit)*
- **P2.3** Given `jev_provider: selfhosted` and a selfhosted endpoint but no `jev_model`, when resolved, then `ValueError("selfhosted jev_provider requires jev_model")`. *(Unit)*
- **P2.4** Given `jev_provider: typesafe|openrouter` with a selfhosted endpoint (or `selfhosted` with a hosted fixed endpoint), when resolved, then `ValueError("jev_provider and api_endpoint select different providers")`. *(Unit)*
- **P2.5** Given an endpoint that is not one of the two hosted constants and not an absolute http(s) URL (e.g. `ftp://x`, `not a url`, `http://` hostless), when resolved, then `ValueError("api_endpoint must be a fixed Jev endpoint or an operator-run http(s) endpoint")`. *(Unit)*
- **P2.6** Given an unknown `jev_provider` string, when resolved, then `ValueError("jev_provider must be auto, typesafe, openrouter, or selfhosted")`. *(Unit)*
- **P2.7** Given a selfhosted endpoint alone, when `_valid_model_slug(model, endpoint)` is consulted, then a non-empty, ≤128-char, stripped, printable model is accepted and membership in `OPENROUTER_MODELS`/`TYPESAFE_MODELS`/`ALLOWED_MODELS` is NOT required. *(Unit)*
- **P2.8** Given hosted endpoints (TypeSafe or OpenRouter), when resolved, then behavior is exactly as upstream: `auto` prefers `typesafe` when `TYPESAFE_API_KEY` exists, default model aliases `jev-latest` / `typesafe/jev-1.13` apply. *(Unit)*
- **P2.9** Given any invalid route configuration, when `decision_tools_available()` runs, then it returns `False` (and `effective_provider()` returns `None`) — availability is hidden, not raised. *(Unit)*

### F3 — Keyless secret layer
- **P3.1** Given `jev_provider: selfhosted`, when `_secret("selfhosted")` runs, then it returns `None`/falsy without consulting any secret-store entry (no `TYPESAFE_API_KEY`, no `OPENROUTER_API_KEY` lookup) and never raises. *(Unit)*
- **P3.2** Given `_credential_ready(presence, "selfhosted")` with an empty presence dict, when called, then it returns `True` — a selfhosted route is credential-ready with zero stored credentials. *(Unit)*
- **P3.3** Given a hosted provider, when `_credential_ready` runs, then the behavior is unchanged: the route's own key must be present; the other provider's key does not count. *(Unit)*

### F4 — DecisionClient over a selfhosted endpoint
- **P4.1** Given `DecisionClient(api_key="", endpoint="http://decider.home.lan:8895/…", model="decider-4b v2.1")`, when constructed, then it succeeds (no "Jev API key is required" error). *(Unit)*
- **P4.2** Given a hosted endpoint and an empty/whitespace `api_key`, when constructed, then `ValueError("Jev API key is required")` (unchanged). *(Unit)*
- **P4.3** Given a selfhosted client, when a request is serialized, then the headers contain **no** `Authorization` header at all — not `Bearer None`, not `Bearer ""`. *(Unit — transport stub captures headers.)*
- **P4.4** Given a selfhosted client on a plaintext `http://` URL, when connecting, then the plain `http.client.HTTPConnection` class is used (not TLS); on hosted endpoints the TLS connection class is used (unchanged). *(Unit)*
- **P4.5** Given a selfhosted response whose top-level `model` differs from the configured model by even one character, when validated, then a `JevRequestError`/validation error is raised — the server must echo the configured model **exactly** on every response. *(Unit)*
- **P4.6** Given a selfhosted client and a redirect response, when the server replies 3xx, then the redirect is rejected (fail closed) — the `_NoRedirectHandler` semantics apply to operator endpoints too. *(Unit)*
- **P4.7** Given a selfhosted client, when the endpoint value is an absolute http(s) URL that is NOT one of the two hosted constants, then construction succeeds; the two hosted constants are never classified selfhosted. *(Unit — `_selfhosted_endpoint` truth table: https/http qualify; hosted pair, non-strings, no-host values, other schemes do not.)*

### F5 — Tolerant answer fields on selfhosted routes
- **P5.1** Given a selfhosted response whose `choice` answer carries extra operator fields (e.g. calibration metadata), when validated, then the extras are dropped and the answer is accepted with exactly `choice`, `probabilities`, `confidence`. *(Unit)*
- **P5.2** Same for `score` answers: extras dropped, `score`, `legend`, `probabilities`, `confidence` retained. *(Unit)*
- **P5.3** Given a hosted route with the same extra fields, when validated, then the request is **refused** ("unexpected response fields") — tolerance is selfhosted-only. *(Unit)*
- **P5.4** Given a selfhosted response missing a required consumed field (e.g. `choice` answer without `probabilities`), when validated, then it is refused — tolerance adds fields, it never removes required ones. *(Unit)*

### F6 — Per-review approval routing (`ApprovalClient`)
- **P6.1** Given the plugin route resolves to selfhosted, when `ApprovalClient.create()` is called, then the review request goes to the operator's endpoint with credential `""` and model = the route's `jev_model` — not the constructor-supplied hosted default `typesafe/jev-1.13`. *(Unit — route stub + transport capture.)*
- **P6.2** Given a selfhosted route, when `create()` runs, then the resulting `DecisionClient` receives `api_key=""` even if the host supplied a non-empty mirror `api_key` to the constructor — the hosted credential is never forwarded. *(Unit)*
- **P6.3** Given the route resolver raises (unresolvable route), when `create()` runs, then the error propagates out of `create()` (review escalates) and **no** request is made to the hosted endpoint — there is no hosted fallback. *(Unit)*
- **P6.4** Given `ApprovalClient.__init__` with a `base_url` other than the hosted default, when constructed, then `ValueError("unsupported approval endpoint")` — the constructor still refuses foreign base URLs; routing happens only via the resolver. *(Unit)*
- **P6.5** Given no route resolver or a hosted-route resolver, when `create()` runs, then behavior is exactly upstream: hosted endpoint, `OPENROUTER_API_KEY` credential, and only `typesafe/jev-1.13{,-20260917}` models accepted. *(Unit)*
- **P6.6** Given the route resolver, when it is invoked, then it is invoked **per review** (each `create()`), not cached at construction — so a config change between reviews is honored. *(Unit — count resolver calls.)*
- **P6.7** Given `register_approval_provider(enabled, route)`, when Hermes constructs the plugin profile, then `route` is threaded through `Profile.create_client` into `ApprovalClient`. *(Unit — fake registry capture.)*

### F7 — Registry/env-var declaration (SW-1 contract)
- **P7.1** Given the registered profile, when its `env_vars` are read, then `OPENROUTER_API_KEY` is declared unconditionally (host registry drops keyless profiles; the auxiliary gate requires a non-empty key to construct the client). *(Unit)*
- **P7.2** Given a selfhosted route and a non-empty sentinel `OPENROUTER_API_KEY` in the store, when a review runs, then no wire request carries that value (P4.3/P6.2 prove the header absence). *(Unit + e2e.)*
- **P7.3** Given a headless Hermes session with the gate-only sentinel credential set, when the auxiliary approval gate constructs the plugin client, then construction succeeds and switchyard-approvals functions end-to-end (the SW-1 fix). *(Integration — real Hermes loader in a disposable `HERMES_HOME`.)*

### F8 — Manifest and docs
- **P8.1** Given `plugin.yaml`, when parsed, then `jev_provider` documents `selfhosted`, `api_endpoint` documents the arbitrary-absolute-URL contract, and `jev_model` documents "required, no default, must be echoed" for selfhosted. *(Unit — schema/README capability tests.)*
- **P8.2** Given `README.md`, when the capability table is rendered, then the selfhosted row matches actual behavior ("never leaves the operator network"). *(Unit — `test_readme_capabilities.py` pattern.)*

## 3. Test-layer mapping

| Layer | Verifies | Location / new work |
|-------|----------|---------------------|
| **Unit** (offline, synthetic transports) | P1–P6, P8 — every G/W/T above | Mostly **already exists** in `tests/test_selfhosted_jev.py` (endpoint truth table, route resolution, keyless wire, tolerant answer fields, approval routing, no-fallback escalation, registry declaration) and `tests/test_switchyard.py` (`test_arbitrary_http_endpoint_is_the_selfhosted_route`). Remaining gaps: P1.1 (tuple assertion), P6.6 (per-review invocation count), P4.6 (redirect rejection on selfhosted), P5.4 (missing required field on selfhosted). Add these to `tests/test_selfhosted_jev.py`. |
| **Integration** (real Hermes loader, disposable `HERMES_HOME`) | P7.3, toolset exposure/auxiliary gate wiring, `hermes switchyard status --json` | `tests/test_toolset_exposure.py` real-loader cases; `tests/test_native_tool_invocation.py`. Skips when Hermes unimportable unless `SWITCHYARD_REQUIRE_HERMES=1`; use `SWITCHYARD_HERMES_ROOT` for a source checkout. P7.3 covered by `tests/test_selfhosted_gate_construction.py` (sentinel-credential headless auxiliary-gate construction + no-sentinel control, real loader + real `call_llm` ladder against a synthetic operator server). |
| **E2E** (live, manual/CI-gated) | P4.5, P7.2 against the real operator server | `scripts/ci/live_jev_contract.py` + `.github/workflows/live-jev.yml`: point `api_endpoint` at the operator decider, assert exact model echo, zero `Authorization` header on the wire (server-side log or local capture proxy), and a passing review round-trip. |
| **Portability/hygiene** | P8, plus no private hosts/credentials leaked by the new config examples | `scripts/check_portability.py`, `tests/test_portability.py`. |

## 4. Config, fixtures, and external services

**Config used by tests**
- `jev_provider: selfhosted`, `api_endpoint: <operator http(s) URL>`, `jev_model: <operator model>` — route fixtures.
- Hosted baselines: `TYPESAFE_API_KEY` / `OPENROUTER_API_KEY` presence toggles for P2.8, P3.3, P6.5.
- Gate-only sentinel `OPENROUTER_API_KEY` (value is never sent anywhere on a selfhosted route) for P7.2/P7.3 — see wiki `entities/jev` for placement; do not use a real key.

**Fixtures (all synthetic, offline)**
- Transport stubs: callable `transport=` on `DecisionClient` and captured-header HTTP stubs (the existing pattern in `tests/test_selfhosted_jev.py`).
- Route resolver stubs returning `(endpoint, model, provider)` triples, and raising stubs for the escalation case (P6.3).
- Fake provider registry capturing `Profile.create_client` kwargs (P6.7) — pattern in `test_selfhosted_jev.py::test_plugin_registration_passes_the_selfhosted_route_to_the_registry`.
- Sample selfhosted responses: hosted-canonical shape, shape + operator extras (calibration metadata), shape missing a required field, wrong-echo model.

**External services**
- **Live decider** (operator's Jev server, e.g. `decider.home.lan:8895`, model `decider-4b v2.1`): only for the e2e layer and the `live-jev` manual workflow. All unit/integration tests must stay offline and skip/flag anything needing the network.
- **Hermes runtime**: for integration layer only (disposable `HERMES_HOME`); never required for unit tests.

## 5. Coverage-completeness check

Every observable behavior of the modification maps to at least one promise:
F1→P1, F2→P2, F3→P3, F4→P4, F5→P5, F6→P6, F7→P7, F8→P8, and each promise names
its concrete target module/function in §1's table plus the test file to extend
in §3. Downstream test authors need only §2, §3, and §4 — the plugin source
need not be re-read except for the functions named here.
