"""AI_RECHECK: pedido genérico, resultado explícito, validação determinística da resposta e roteamento."""
from __future__ import annotations

import copy
import unittest

from tests.helpers import REPO, load_json
from tests.recheck_helpers import (
    DDL_REF, DOC, DOC_REF, database_request, evidence, finding, human_decision, inconclusive, request, resolved, source_gap, two_claim_request,
)
from core import ai_recheck as ar
from core.ai_recheck import (
    AIRecheckRequest, AIRecheckResult, PriorInterpretation, RecheckClaim, RecheckError, RecheckPolicy,
    assemble_result, human_review_candidate, route_for, source_gap_entry, validate_model_output,
)
from core.model_task import EvidenceRef
from core.schema_lite import Registry, Validator

VALIDATOR = Validator(Registry())
SNAPSHOT = [{"ref": DOC_REF, "sha256": evidence().sha256, "byte_length": evidence().byte_length}]


def provenance(**overrides: object) -> dict:
    from core.hashing import hash_canonical_json

    data: dict = {
        "executor": "ai-recheck/1",
        "adapter": {"adapter_id": "synthetic-a", "adapter_version": "1", "capabilities": ["structured-output"], "data_boundary": "LOCAL", "deterministic": True},
        "evidence_snapshot": SNAPSHOT,
        "evidence_snapshot_digest": str(hash_canonical_json(SNAPSHOT)),
        "evidence_verified": True,
        "attempt": 1,
        "round": 1,
        "reproducibility": "DETERMINISTIC_ADAPTER",
        "epistemic_ceiling": "INFERRED",
    }
    data.update(overrides)
    return data


def accepted(status: str = "RESOLVED", body: "dict | None" = None, req: "AIRecheckRequest | None" = None, policy: "RecheckPolicy | None" = None) -> AIRecheckResult:
    req = req or request()
    bodies = {
        "RESOLVED": {"rationale": "r", "evidence_used": [DOC_REF], "findings": [dict(finding(), status="INFERRED")], "confidence": 0.9},
        "SOURCE_GAP": {"rationale": "r", "evidence_used": [DOC_REF], "source_gap": source_gap()["source_gap"]},
        "HUMAN_DECISION": {"rationale": "r", "evidence_used": [DOC_REF], "decision": human_decision()["decision"]},
        "INCONCLUSIVE": {"rationale": "r", "evidence_used": [DOC_REF], "inconclusive": {"unresolved": ["c1"], "reason": "MODEL_REPORTED"}},
    }
    body = body or bodies[status]
    policy = policy or RecheckPolicy()
    return assemble_result(req.request_id, status, body, route_for(status, req, policy, source_gap=body.get("source_gap")), provenance())


class VocabularyMatchesTheSchemas(unittest.TestCase):
    def test_statuses_routes_and_failure_kinds_are_the_schema_enums(self) -> None:
        schema = load_json(REPO / "schemas" / "ai-recheck-result.schema.json")
        self.assertEqual(list(ar.STATUSES), schema["properties"]["status"]["enum"])
        self.assertEqual(list(ar.MODEL_STATUSES), schema["$defs"]["modelOutput"]["properties"]["status"]["enum"])
        self.assertEqual(list(ar.ROUTES), schema["$defs"]["routing"]["properties"]["route"]["enum"])
        self.assertEqual(list(ar.FAILURE_KINDS), schema["properties"]["failure"]["properties"]["kind"]["enum"])
        self.assertEqual(sorted(set(ar.DESTINATIONS.values())), sorted(schema["$defs"]["routing"]["properties"]["destination"]["enum"]))

    def test_the_minimal_vocabulary(self) -> None:
        self.assertEqual(ar.STATUSES, ("RESOLVED", "SOURCE_GAP", "HUMAN_DECISION", "INCONCLUSIVE", "FAILED"))
        self.assertNotIn("FAILED", ar.MODEL_STATUSES, "o modelo nunca afirma FAILED")
        self.assertNotIn("VALIDATED", ar.STATUSES)

    def test_the_foundation_routes_are_reused_not_reinvented(self) -> None:
        from core import epistemics

        self.assertTrue(set(ar.FALLBACK_ROUTES) <= set(epistemics.ROUTES))
        self.assertNotIn("AI_RECHECK", ar.FALLBACK_ROUTES, "contingência nunca repete a análise")

    def test_source_kinds_for_a_gap_are_the_foundation_ones(self) -> None:
        gap = load_json(REPO / "schemas" / "ai-recheck-result.schema.json")["$defs"]["sourceGap"]["properties"]["recommended_source"]
        self.assertEqual(gap, {"$ref": "urn:mr-software-intelligence:schema:common:1#/$defs/sourceKind"})

    def test_the_expected_output_contract_is_declared_in_one_place(self) -> None:
        self.assertEqual(ar.EXPECTED_OUTPUT, {"contract": "ai-recheck-output", "version": "1"})
        request_schema = load_json(REPO / "schemas" / "ai-recheck-request.schema.json")
        self.assertEqual(request_schema["properties"]["expected_output"]["properties"]["contract"]["const"], ar.OUTPUT_CONTRACT.name)


class TheRequestIsGeneric(unittest.TestCase):
    def test_it_describes_arbitrary_subjects_not_screens(self) -> None:
        for kind in ("source-code", "documentation", "database-schema", "runtime-trace", "api", "configuration", "screenshot", "log"):
            with self.subTest(kind):
                self.assertEqual(request(subject_kind=kind).subject_kind, kind)

    def test_the_contract_never_names_screen_widgets(self) -> None:
        # Só identificadores e chaves de schema contam: o docstring pode (e deve) dizer que nada pressupõe tela, botão, aba ou grid.
        import ast

        names: set = set()
        for module in ("ai_recheck", "recheck_engine", "model_task", "model_adapter"):
            for node in ast.walk(ast.parse((REPO / "core" / f"{module}.py").read_text(encoding="utf-8"))):
                for attribute in ("id", "attr", "name", "arg"):
                    value = getattr(node, attribute, None)
                    if isinstance(value, str):
                        names.add(value.lower())

        def keys(node: object) -> "set[str]":
            found: set = set()
            if isinstance(node, dict):
                for key, value in node.items():
                    found.add(str(key).lower())
                    found |= keys(value)
            elif isinstance(node, list):
                for item in node:
                    found |= keys(item)
            return found

        for schema in ("ai-recheck-request", "ai-recheck-result", "model-task", "model-result"):
            names |= keys(load_json(REPO / "schemas" / f"{schema}.schema.json"))
        widgets = ("screen", "button", "tab", "tabs", "grid", "window", "field", "combobox", "dialog")
        found = sorted(word for word in widgets if any(word == name or name.startswith(word + "_") for name in names))
        self.assertEqual(found, [])

    def test_every_required_concept_is_present(self) -> None:
        data = request().to_dict()
        for key in ("subject", "ambiguity", "claims", "evidence", "prior_interpretation", "reason", "expected_output"):
            self.assertIn(key, data)
        self.assertEqual(VALIDATOR.validate(data, "ai-recheck-request"), [])

    def test_the_identity_is_deterministic_and_order_independent(self) -> None:
        a, b = evidence("a-1", b"one", locator="input:one"), evidence("b-1", b"two", locator="input:two")
        first = request(evidence=(a, b), claims=two_claim_request().claims)
        second = request(evidence=(b, a), claims=tuple(reversed(two_claim_request().claims)))
        self.assertEqual(first.request_id, second.request_id)
        self.assertRegex(first.request_id, r"^art:ai-recheck\.request:sha256:[0-9a-f]{64}$")

    def test_what_changes_the_work_changes_the_identity(self) -> None:
        base = request().request_id
        variants = [
            request(ambiguity="Outra ambiguidade."), request(reason="Outro motivo."), request(max_boundary="PRIVATE"), request(risk_level="HIGH"),
            request(evidence=(evidence(data=DOC + b"!"),)), request(evidence=(evidence(sensitive=None),)), request(evidence=(evidence(locator="input:other"),)),
            request(prior_interpretation=None), request(round=2), request(software_id="synthetic-two"), request(required_capabilities=("long-context",)),
            request(subject_title="Outro título"), request(claims=(RecheckClaim("c1", "Outra afirmação."),)),
        ]
        for index, item in enumerate(variants):
            with self.subTest(index):
                self.assertNotEqual(item.request_id, base)
        self.assertEqual(request().request_id, base)

    def test_a_request_without_evidence_is_not_a_recheck(self) -> None:
        with self.assertRaises(RecheckError) as caught:
            request(evidence=())
        self.assertEqual(caught.exception.code, "NO_EVIDENCE")

    def test_claims_are_required_and_unique(self) -> None:
        with self.assertRaises(RecheckError):
            request(claims=())
        with self.assertRaises(RecheckError) as caught:
            request(claims=(RecheckClaim("c1", "a"), RecheckClaim("c1", "b")))
        self.assertEqual(caught.exception.code, "DUPLICATE_CLAIM")
        with self.assertRaises(RecheckError):
            request(evidence=(evidence(), evidence()))

    def test_validated_knowledge_is_never_rechecked_by_ai(self) -> None:
        with self.assertRaises(RecheckError) as caught:
            PriorInterpretation("Confirmado por Fulano.", "VALIDATED")
        self.assertEqual(caught.exception.code, "VALIDATED_PRIOR")
        self.assertEqual(request(prior_interpretation=PriorInterpretation("x", "OBSERVED")).prior_interpretation.status, "OBSERVED")

    def test_text_fields_are_bounded_and_portable(self) -> None:
        for change in (
            dict(ambiguity=""), dict(reason="x" * 1001), dict(subject_ref="C:\\temp\\x"), dict(subject_kind="Not A Name"),
            dict(ambiguity="ver C:\\Users\\alguem\\arquivo.txt"), dict(software_id="Bad Id"), dict(round=0), dict(risk_level="EXTREME"),
            dict(max_boundary="MOON"), dict(required_capabilities=("telepathy",)),
        ):
            with self.subTest(change), self.assertRaises(Exception):
                request(**change)

    def test_the_data_policy_is_mandatory(self) -> None:
        data = request().to_dict()
        del data["data_policy"]
        with self.assertRaises(RecheckError):
            AIRecheckRequest.from_dict(data)

    def test_round_trip_through_the_schema(self) -> None:
        for item in (request(), database_request(), two_claim_request(prior_interpretation=None, software_id=None)):
            with self.subTest(item.subject_ref):
                self.assertEqual(AIRecheckRequest.from_dict(item.to_dict()), item)

    def test_a_tampered_request_is_refused(self) -> None:
        data = request().to_dict()
        for mutate in (
            lambda d: d.__setitem__("ambiguity", "adulterada"),
            lambda d: d["evidence"][0].__setitem__("sha256", "0" * 64),
            lambda d: d.__setitem__("request_id", "art:ai-recheck.request:sha256:" + "0" * 64),
            lambda d: d.__setitem__("unexpected", 1),
            lambda d: d["prior_interpretation"].__setitem__("status", "VALIDATED"),
            lambda d: d["expected_output"].__setitem__("contract", "other"),
        ):
            broken = copy.deepcopy(data)
            mutate(broken)
            with self.subTest(), self.assertRaises(RecheckError):
                AIRecheckRequest.from_dict(broken)

    def test_effective_capabilities_add_structured_output_and_what_the_modality_imposes(self) -> None:
        self.assertEqual(request().effective_capabilities(), ("structured-output",))
        image = evidence("img", b"\x00\x01", "screenshot", "image", locator="input:shot")
        self.assertEqual(request(evidence=(image,)).effective_capabilities(), ("structured-output", "vision"))
        self.assertEqual(request(required_capabilities=("long-context",)).effective_capabilities(), ("long-context", "structured-output"))

    def test_the_environment_ceiling_can_tighten_but_never_loosen(self) -> None:
        self.assertEqual(request(max_boundary="EXTERNAL").requirements("PRIVATE").max_boundary, "PRIVATE")
        self.assertEqual(request(max_boundary="LOCAL").requirements("EXTERNAL").max_boundary, "LOCAL")
        self.assertEqual(request(max_boundary="PRIVATE").requirements(None).max_boundary, "PRIVATE")

    def test_the_snapshot_pins_hash_and_size_of_what_was_authorized(self) -> None:
        self.assertEqual(request().evidence_snapshot(), SNAPSHOT)


class PolicyRules(unittest.TestCase):
    def test_defaults_recheck_once_and_use_the_foundation_confidence_floor(self) -> None:
        from core.epistemics import DEFAULT_LOW_BELOW

        policy = RecheckPolicy()
        self.assertEqual((policy.max_rounds, policy.min_resolved_confidence, policy.inconclusive_route), (1, DEFAULT_LOW_BELOW, None))

    def test_inconclusive_can_never_route_back_to_ai_recheck(self) -> None:
        with self.assertRaises(RecheckError) as caught:
            RecheckPolicy(inconclusive_route="AI_RECHECK")
        self.assertEqual(caught.exception.code, "RECHECK_LOOP")
        for route in ar.FALLBACK_ROUTES:
            self.assertEqual(RecheckPolicy(inconclusive_route=route).inconclusive_route, route)
        with self.assertRaises(Exception):
            RecheckPolicy(inconclusive_route="REUSE")

    def test_invalid_values_are_refused(self) -> None:
        for change in (dict(max_rounds=0), dict(max_rounds=6), dict(min_resolved_confidence=1.5), dict(boundary_ceiling="MOON"), dict(max_rounds=True)):
            with self.subTest(change), self.assertRaises(Exception):
                RecheckPolicy(**change)

    def test_to_dict_is_portable_and_stable(self) -> None:
        self.assertEqual(RecheckPolicy(inconclusive_route="SOURCE_GAP").to_dict(), RecheckPolicy(inconclusive_route="SOURCE_GAP").to_dict())
        self.assertNotIn("inconclusive_route", RecheckPolicy().to_dict())


class ModelOutputValidation(unittest.TestCase):
    def check(self, output: object, req: "AIRecheckRequest | None" = None) -> "tuple[str | None, list[str]]":
        return validate_model_output(req or request(), output)

    def test_every_valid_shape_is_accepted(self) -> None:
        for output in (resolved(), source_gap(), source_gap(recommended=None), human_decision(), inconclusive(), inconclusive(findings=[]),):
            with self.subTest(output["status"]):
                self.assertEqual(self.check(output), (None, []))
        both = two_claim_request()
        self.assertEqual(self.check(resolved(("c1", "c2")), both), (None, []))
        self.assertEqual(self.check(inconclusive(("c2",), findings=[finding("c1")]), both), (None, []))

    def test_not_an_object_or_missing_fields_is_malformed(self) -> None:
        for output in ("texto", ["lista"], None, 3, {}, {"status": "RESOLVED"}, {"status": "RESOLVED", "rationale": "r", "evidence_used": [DOC_REF]}):
            with self.subTest(repr(output)[:30]):
                kind, issues = self.check(output)
                self.assertEqual(kind, ar.MALFORMED_RESPONSE)
                self.assertTrue(issues)

    def test_unknown_or_forbidden_fields_are_malformed(self) -> None:
        base = resolved()
        for extra in ({"validated_by": "Fulano"}, {"validation": {"method": "human_review"}}, {"epistemic_status": "VALIDATED"}, {"routing": {}}, {"confidence": 0.9}):
            with self.subTest(extra):
                self.assertEqual(self.check({**base, **extra})[0], ar.MALFORMED_RESPONSE)

    def test_the_model_has_no_field_to_pick_an_epistemic_status(self) -> None:
        for status in ("VALIDATED", "OBSERVED", "INFERRED"):
            output = resolved()
            output["findings"][0]["status"] = status
            with self.subTest(status):
                self.assertEqual(self.check(output)[0], ar.MALFORMED_RESPONSE)
        self.assertEqual(self.check({**resolved(), "status": "VALIDATED"})[0], ar.MALFORMED_RESPONSE)
        self.assertEqual(self.check({**resolved(), "status": "FAILED"})[0], ar.MALFORMED_RESPONSE)

    def test_status_specific_fields_are_enforced(self) -> None:
        cases = {
            "resolved without findings": {**resolved(), "findings": []},
            "resolved with a gap": {**resolved(), "source_gap": source_gap()["source_gap"]},
            "resolved with unresolved": {**resolved(), "unresolved": ["c1"]},
            "gap without source_gap": {k: v for k, v in source_gap().items() if k != "source_gap"},
            "gap with findings": {**source_gap(), "findings": [finding()]},
            "gap with unknown source kind": {**source_gap(), "source_gap": {**source_gap()["source_gap"], "recommended_source": "telepathy"}},
            "gap without missing": {**source_gap(), "source_gap": {"missing": [], "insufficient_because": "x"}},
            "decision with one option": {**human_decision(), "decision": {"question": "q", "options": ["única"]}},
            "decision with duplicate options": {**human_decision(), "decision": {"question": "q", "options": ["a", "a"]}},
            "decision with findings": {**human_decision(), "findings": [finding()]},
            "inconclusive without unresolved": {k: v for k, v in inconclusive().items() if k != "unresolved"},
            "inconclusive with a decision": {**inconclusive(), "decision": human_decision()["decision"]},
        }
        for label, output in cases.items():
            with self.subTest(label):
                self.assertEqual(self.check(output)[0], ar.MALFORMED_RESPONSE)

    def test_confidence_must_be_a_number_between_zero_and_one(self) -> None:
        for bad in (-0.1, 1.01, "0.9", None, True, [0.9]):
            output = resolved()
            output["findings"][0]["confidence"] = bad
            with self.subTest(repr(bad)):
                self.assertEqual(self.check(output)[0], ar.MALFORMED_RESPONSE)
        for edge in (0, 1, 0.0, 1.0):
            output = resolved()
            output["findings"][0]["confidence"] = edge
            self.assertEqual(self.check(output), (None, []))

    def test_an_unknown_evidence_reference_invalidates_the_whole_response(self) -> None:
        in_used = {**resolved(), "evidence_used": [DOC_REF, "invented-9"]}
        in_finding = resolved()
        in_finding["findings"][0]["evidence_refs"] = ["invented-9"]
        for label, output in (("evidence_used", in_used), ("finding", in_finding)):
            with self.subTest(label):
                kind, issues = self.check(output)
                self.assertEqual(kind, ar.UNAUTHORIZED_EVIDENCE)
                self.assertTrue(any("invented-9" in issue for issue in issues))

    def test_an_authorized_ref_of_another_request_is_still_unknown_here(self) -> None:
        kind, _ = validate_model_output(request(), resolved(refs=(DDL_REF,)))
        self.assertEqual(kind, ar.UNAUTHORIZED_EVIDENCE)

    def test_claim_coverage(self) -> None:
        both = two_claim_request()
        self.assertEqual(self.check(resolved(("c1",)), both)[0], ar.MALFORMED_RESPONSE, "RESOLVED trata todas as afirmações")
        self.assertEqual(self.check(resolved(("c1", "zz")), request())[0], ar.MALFORMED_RESPONSE, "afirmação desconhecida")
        duplicate = resolved(("c1",))
        duplicate["findings"].append(finding("c1"))
        self.assertEqual(self.check(duplicate)[0], ar.MALFORMED_RESPONSE)
        self.assertEqual(self.check(inconclusive(("zz",)), both)[0], ar.MALFORMED_RESPONSE)
        self.assertEqual(self.check(inconclusive(("c1",), findings=[finding("c1")]), both)[0], ar.MALFORMED_RESPONSE, "resolvida e não resolvida")
        self.assertEqual(self.check(inconclusive(("c1",)), both)[0], ar.MALFORMED_RESPONSE, "c2 sumiu")

    def test_the_response_may_not_leak_a_local_path(self) -> None:
        output = resolved()
        output["findings"][0]["statement"] = "Ver C:\\Users\\alguem\\x.cfg"
        self.assertEqual(self.check(output)[0], ar.MALFORMED_RESPONSE)

    def test_the_rationale_is_short_by_contract(self) -> None:
        output = resolved()
        output["rationale"] = "x" * 1001
        self.assertEqual(self.check(output)[0], ar.MALFORMED_RESPONSE)
        output["rationale"] = ""
        self.assertEqual(self.check(output)[0], ar.MALFORMED_RESPONSE)

    def test_validation_is_pure(self) -> None:
        output = resolved()
        before = copy.deepcopy(output)
        self.check(output)
        self.assertEqual(output, before)


class Routing(unittest.TestCase):
    def test_each_status_has_one_route_and_only_reuse_proceeds(self) -> None:
        req, policy = request(), RecheckPolicy()
        table = {
            "RESOLVED": ("REUSE", "ai_result", True),
            "SOURCE_GAP": ("SOURCE_GAP", "source_backlog", False),
            "HUMAN_DECISION": ("HUMAN_DECISION", "human_review_candidates", False),
            "FAILED": ("FAILURE", "failure_handling", False),
        }
        for status, (route, destination, proceeds) in table.items():
            routing = route_for(status, req, policy, failure_kind="ADAPTER_FAILURE")
            with self.subTest(status):
                self.assertEqual((routing["route"], routing["destination"], routing["proceeds"]), (route, destination, proceeds))

    def test_a_source_gap_carries_the_recommended_source_kind(self) -> None:
        self.assertEqual(route_for("SOURCE_GAP", request(), RecheckPolicy(), source_gap={"recommended_source": "runtime"})["needed_source"], "runtime")
        self.assertNotIn("needed_source", route_for("SOURCE_GAP", request(), RecheckPolicy(), source_gap={}))

    def test_inconclusive_follows_the_foundation_ladder_by_default(self) -> None:
        with_prior = route_for("INCONCLUSIVE", request(), RecheckPolicy())
        self.assertEqual((with_prior["route"], with_prior["reason"]), ("HUMAN_VALIDATION", "inconclusive-fallback-ladder"))
        without_prior = route_for("INCONCLUSIVE", request(prior_interpretation=None), RecheckPolicy())
        self.assertEqual((without_prior["route"], without_prior["needed_source"]), ("SOURCE_GAP", "human-expert"))
        self.assertFalse(with_prior["proceeds"] or without_prior["proceeds"])

    def test_the_ladder_agrees_with_core_escalation(self) -> None:
        from core.escalation import decide

        expected = decide({"confidence": 0.0, "recheck_done": True, "recheck_resolved": False, "inference_to_confirm": True})
        self.assertEqual(route_for("INCONCLUSIVE", request(), RecheckPolicy())["route"], expected.route)

    def test_a_policy_fallback_overrides_the_ladder(self) -> None:
        for route in ar.FALLBACK_ROUTES:
            routing = route_for("INCONCLUSIVE", request(), RecheckPolicy(inconclusive_route=route))
            with self.subTest(route):
                self.assertEqual((routing["route"], routing["reason"]), (route, "inconclusive-fallback-policy"))

    def test_no_route_ever_repeats_the_recheck(self) -> None:
        for status in ar.STATUSES:
            for policy in [RecheckPolicy()] + [RecheckPolicy(inconclusive_route=route) for route in ar.FALLBACK_ROUTES]:
                self.assertNotEqual(route_for(status, request(), policy, failure_kind="X")["route"], "AI_RECHECK")

    def test_an_unknown_status_is_refused(self) -> None:
        with self.assertRaises(Exception):
            route_for("VALIDATED", request(), RecheckPolicy())


class ResultContract(unittest.TestCase):
    def test_every_status_assembles_and_validates_against_the_schema(self) -> None:
        for status in ("RESOLVED", "SOURCE_GAP", "HUMAN_DECISION", "INCONCLUSIVE"):
            result = accepted(status)
            with self.subTest(status):
                self.assertEqual(result.status, status)
                self.assertEqual(VALIDATOR.validate(result.to_dict(), "ai-recheck-result"), [])
                self.assertEqual(AIRecheckResult.from_dict(result.to_dict()), result)

    def test_a_resolved_result_carries_everything_an_audit_needs(self) -> None:
        data = accepted().to_dict()
        for key in ("findings", "evidence_used", "rationale", "confidence", "provenance", "routing", "request_id", "result_id"):
            self.assertIn(key, data)
        self.assertEqual(data["confidence"], 0.9)
        self.assertEqual(data["findings"][0]["status"], "INFERRED")
        self.assertEqual(data["provenance"]["epistemic_ceiling"], "INFERRED")

    def test_the_failed_result_is_not_knowledge(self) -> None:
        req = request()
        failure = {"kind": "ADAPTER_FAILURE", "message": "TIMEOUT: demorou", "retryable": True}
        result = assemble_result(req.request_id, "FAILED", {"failure": failure}, route_for("FAILED", req, RecheckPolicy(), failure_kind="ADAPTER_FAILURE"), provenance(evidence_verified=False))
        self.assertEqual((result.status, result.route, result.proceeds), ("FAILED", "FAILURE", False))
        self.assertEqual(result.reusable_findings, ())
        for key in ("findings", "rationale", "confidence", "evidence_used"):
            self.assertNotIn(key, result.to_dict())

    def test_only_a_reuse_route_exposes_reusable_findings(self) -> None:
        self.assertEqual(len(accepted("RESOLVED").reusable_findings), 1)
        for status in ("SOURCE_GAP", "HUMAN_DECISION", "INCONCLUSIVE"):
            self.assertEqual(accepted(status).reusable_findings, ())

    def test_validated_can_never_appear_in_a_result(self) -> None:
        data = accepted().to_dict()
        for mutate in (
            lambda d: d["findings"][0].__setitem__("status", "VALIDATED"),
            lambda d: d["findings"][0].__setitem__("status", "OBSERVED"),
            lambda d: d["findings"][0].__setitem__("validation", {"validated_by": "Fulano"}),
            lambda d: d.__setitem__("validation", {"validated_by": "Fulano"}),
            lambda d: d["provenance"].__setitem__("epistemic_ceiling", "VALIDATED"),
            lambda d: d.__setitem__("status", "VALIDATED"),
        ):
            broken = copy.deepcopy(data)
            mutate(broken)
            with self.subTest(), self.assertRaises(RecheckError):
                AIRecheckResult.from_dict(broken)

    def test_the_result_id_is_the_identity_of_the_accepted_body_and_ignores_provenance_and_route(self) -> None:
        first = accepted()
        other_provenance = assemble_result(
            first.request_id, "RESOLVED", {k: v for k, v in first.to_dict().items() if k in ("rationale", "evidence_used", "findings", "confidence")},
            first.routing, provenance(attempt=3, adapter={"adapter_id": "synthetic-b", "adapter_version": "9", "capabilities": ["structured-output"], "data_boundary": "LOCAL", "deterministic": False}, reproducibility="NOT_GUARANTEED"),
        )
        self.assertEqual(first.result_id, other_provenance.result_id)
        changed = accepted(body={"rationale": "outra", "evidence_used": [DOC_REF], "findings": [dict(finding(), status="INFERRED")], "confidence": 0.9})
        self.assertNotEqual(first.result_id, changed.result_id)

    def test_tampering_is_detected(self) -> None:
        data = accepted().to_dict()
        for mutate in (
            lambda d: d["findings"][0].__setitem__("statement", "adulterado"),
            lambda d: d.__setitem__("rationale", "adulterada"),
            lambda d: d.__setitem__("request_id", "art:ai-recheck.request:sha256:" + "1" * 64),
            lambda d: d["routing"].__setitem__("route", "SOURCE_GAP"),
            lambda d: d["routing"].__setitem__("proceeds", False),
            lambda d: d["provenance"].__setitem__("evidence_snapshot_digest", "sha256:" + "2" * 64),
            lambda d: d.__setitem__("confidence", 0.1),
            lambda d: d.__setitem__("extra", True),
        ):
            broken = copy.deepcopy(data)
            mutate(broken)
            with self.subTest(), self.assertRaises(RecheckError):
                AIRecheckResult.from_dict(broken)

    def test_the_route_must_fit_the_status(self) -> None:
        req = request()
        body = accepted("SOURCE_GAP").to_dict()
        body = {k: v for k, v in body.items() if k in ("rationale", "evidence_used", "source_gap")}
        with self.assertRaises(RecheckError) as caught:
            assemble_result(req.request_id, "SOURCE_GAP", body, route_for("RESOLVED", req, RecheckPolicy()), provenance())
        self.assertEqual(caught.exception.code, "ROUTE_MISMATCH")

    def test_the_resolved_confidence_is_the_weakest_finding(self) -> None:
        two = [dict(finding("c1", confidence=0.9), status="INFERRED"), dict(finding("c2", confidence=0.6), status="INFERRED")]
        req = two_claim_request()
        body = {"rationale": "r", "evidence_used": [DOC_REF], "findings": two, "confidence": 0.9}
        with self.assertRaises(RecheckError) as caught:
            assemble_result(req.request_id, "RESOLVED", body, route_for("RESOLVED", req, RecheckPolicy()), provenance())
        self.assertEqual(caught.exception.code, "CONFIDENCE")
        body["confidence"] = 0.6
        self.assertEqual(assemble_result(req.request_id, "RESOLVED", body, route_for("RESOLVED", req, RecheckPolicy()), provenance()).data["confidence"], 0.6)

    def test_a_result_with_a_local_path_is_refused(self) -> None:
        body = {"rationale": "ver C:\\Users\\alguem\\x", "evidence_used": [DOC_REF], "source_gap": source_gap()["source_gap"]}
        req = request()
        with self.assertRaises(RecheckError):
            assemble_result(req.request_id, "SOURCE_GAP", body, route_for("SOURCE_GAP", req, RecheckPolicy()), provenance())

    def test_a_result_is_immutable_data(self) -> None:
        result = accepted()
        copy_ = result.to_dict()
        copy_["rationale"] = "mudou"
        self.assertEqual(result.to_dict()["rationale"], "r")
        result.provenance["attempt"] = 99
        self.assertEqual(result.provenance["attempt"], 1)


class ConsumerViews(unittest.TestCase):
    def test_a_source_gap_entry_records_what_is_missing_why_and_which_source(self) -> None:
        req = request()
        entry = source_gap_entry(req, accepted("SOURCE_GAP"))
        self.assertEqual(entry["missing"], ["O valor configurado em produção."])
        self.assertEqual(entry["insufficient_because"], "O documento só descreve o padrão.")
        self.assertEqual(entry["needed_source"], "runtime")
        self.assertEqual(entry["evidence_consulted"], [DOC_REF])
        self.assertEqual(entry["claims"], ["c1"])
        self.assertEqual(entry["subject"], {"ref": "job:nightly-retention", "kind": "documentation", "title": "Nightly retention job"})

    def test_an_inconclusive_result_routed_to_a_gap_still_names_what_is_unresolved(self) -> None:
        req = request(prior_interpretation=None)
        result = accepted("INCONCLUSIVE", req=req)
        entry = source_gap_entry(req, result)
        self.assertIn("c1", entry["missing"][0])
        self.assertEqual(entry["needed_source"], "human-expert")

    def test_the_source_gap_view_refuses_other_routes(self) -> None:
        with self.assertRaises(RecheckError):
            source_gap_entry(request(), accepted("RESOLVED"))
        with self.assertRaises(RecheckError):
            source_gap_entry(request(ambiguity="outra"), accepted("SOURCE_GAP"))

    def test_a_human_candidate_is_open_and_records_what_ai_already_tried(self) -> None:
        req = request()
        candidate = human_review_candidate(req, accepted("HUMAN_DECISION"))
        self.assertEqual((candidate["route"], candidate["state"]), ("HUMAN_DECISION", "OPEN"))
        self.assertEqual(candidate["options"], ["30 dias", "90 dias"])
        self.assertEqual(candidate["question"], "Qual prazo vale: 30 ou 90 dias?")
        self.assertEqual(candidate["attempted_before"][0]["step"], "ai-recheck")
        self.assertNotIn("answer", candidate, "AI_RECHECK não pergunta nem responde pela pessoa")

    def test_an_inconclusive_result_can_become_a_validation_candidate_with_the_hypothesis(self) -> None:
        req = request()
        candidate = human_review_candidate(req, accepted("INCONCLUSIVE", req=req))
        self.assertEqual(candidate["route"], "HUMAN_VALIDATION")
        self.assertEqual(candidate["hypothesis"], "Provavelmente 30 dias.")
        self.assertTrue(candidate["question"].startswith("Confirmar a interpretação"))

    def test_the_candidate_step_is_a_foundation_ladder_step(self) -> None:
        steps = load_json(REPO / "schemas" / "common.schema.json")["$defs"]["ladderStep"]["enum"]
        self.assertIn("ai-recheck", steps)

    def test_the_human_view_refuses_non_human_routes(self) -> None:
        with self.assertRaises(RecheckError):
            human_review_candidate(request(), accepted("SOURCE_GAP"))


if __name__ == "__main__":
    unittest.main()
