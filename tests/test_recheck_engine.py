"""Executor de AI_RECHECK: resolvido, lacuna de fonte, decisão humana, inconclusivo, falhas técnicas e disciplina de evidência."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from tests.helpers import REPO
from tests.recheck_helpers import (
    DDL, DDL_REF, DOC, DOC_REF, IMAGE, contents, database_request, evidence, finding, human_decision, inconclusive, request, resolved,
    source_gap, two_claim_request,
)
from adapters.models.scripted import RawReturn, RuleBasedModelAdapter, ScriptedModelAdapter
from core import ai_recheck as ar
from core.ai_recheck import AIRecheckResult, RecheckPolicy
from core.epistemics import check_epistemic
from core.model_adapter import AdapterDescriptor, ModelFailure, ModelIdentity, ModelResult
from core.model_task import EVIDENCE_KEY, EvidenceRef
from core.recheck_engine import EvidenceChanged, EvidenceUnavailable, MappingEvidenceReader, run_recheck
from core.schema_lite import Registry, Validator

VALIDATOR = Validator(Registry())


class SpyReader(MappingEvidenceReader):
    def __init__(self, data: dict) -> None:
        super().__init__(data)
        self.reads: list = []

    def read(self, evidence: EvidenceRef) -> bytes:
        self.reads.append(evidence.ref)
        return super().read(evidence)


def run(script: list, req: "ar.AIRecheckRequest | None" = None, data: "dict | None" = None, policy: "RecheckPolicy | None" = None, attempt: int = 1, **adapter: object):
    req = req or request()
    adapter_ = ScriptedModelAdapter(script, **adapter)  # type: ignore[arg-type]
    reader = SpyReader(data if data is not None else contents())
    return run_recheck(req, adapter_, reader, policy=policy, attempt=attempt), adapter_, reader


def failure_kind(result: AIRecheckResult) -> str:
    return result.failure["kind"]  # type: ignore[index]


class ResolvedRecheck(unittest.TestCase):
    def test_a_valid_resolution_is_reusable_and_complete(self) -> None:
        result, adapter, _ = run([resolved()])
        self.assertEqual((result.status, result.route, result.proceeds), ("RESOLVED", "REUSE", True))
        self.assertEqual(VALIDATOR.validate(result.to_dict(), "ai-recheck-result"), [])
        data = result.to_dict()
        self.assertEqual(data["confidence"], 0.9)
        self.assertEqual(data["evidence_used"], [DOC_REF])
        self.assertTrue(data["rationale"])
        self.assertEqual(len(result.reusable_findings), 1)
        self.assertEqual(adapter.calls, 1)

    def test_provenance_records_request_adapter_model_response_and_evidence_snapshot(self) -> None:
        req = request()
        result, adapter, _ = run([resolved()], req, model_id="model-7", adapter_id="synthetic-alpha", adapter_version="3")
        provenance = result.provenance
        self.assertEqual(result.request_id, req.request_id)
        self.assertEqual(provenance["adapter"]["adapter_id"], "synthetic-alpha")
        self.assertEqual(provenance["adapter"]["adapter_version"], "3")
        self.assertEqual(provenance["adapter"]["model_id"], "model-7")
        self.assertEqual(provenance["model"], {"adapter_id": "synthetic-alpha", "model_id": "model-7"})
        self.assertEqual(provenance["task_id"], adapter.tasks[0].task_id)
        self.assertRegex(provenance["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(provenance["evidence_snapshot"], req.evidence_snapshot())
        self.assertTrue(provenance["evidence_verified"])
        self.assertEqual((provenance["attempt"], provenance["round"]), (1, 1))

    def test_the_response_hash_is_the_hash_of_the_canonical_model_output(self) -> None:
        from core.hashing import hash_canonical_json

        result, _, _ = run([resolved()])
        self.assertEqual(result.provenance["response_sha256"], hash_canonical_json(resolved()).digest)

    def test_the_model_configuration_fingerprint_is_recorded_when_known(self) -> None:
        from core.model_adapter import config_fingerprint

        fingerprint = config_fingerprint({"temperature": 0})
        result, _, _ = run([resolved()], config_fingerprint=fingerprint)
        self.assertEqual(result.provenance["adapter"]["config_fingerprint"], fingerprint)
        self.assertEqual(result.provenance["model"]["config_fingerprint"], fingerprint)
        plain, _, _ = run([resolved()])
        self.assertNotIn("config_fingerprint", plain.provenance["adapter"])

    def test_the_model_that_actually_answered_is_recorded_even_if_it_differs_from_the_default(self) -> None:
        step = lambda task: ModelResult.success(task.task_id, ModelIdentity("synthetic-alpha", "actual-model"), resolved())  # noqa: E731
        result, _, _ = run([step], model_id="default-model", adapter_id="synthetic-alpha")
        self.assertEqual(result.provenance["adapter"]["model_id"], "default-model")
        self.assertEqual(result.provenance["model"]["model_id"], "actual-model")

    def test_token_usage_is_recorded_only_when_the_adapter_knows_it(self) -> None:
        with_usage, _, _ = run([resolved()], usage={"input_tokens": 120, "output_tokens": 30})
        self.assertEqual(with_usage.provenance["usage"], {"input_tokens": 120, "output_tokens": 30})
        without, _, _ = run([resolved()])
        self.assertNotIn("usage", without.provenance)
        self.assertEqual(without.status, "RESOLVED")

    def test_reproducibility_is_only_claimed_for_deterministic_adapters(self) -> None:
        deterministic, _, _ = run([resolved()], deterministic=True)
        real_like, _, _ = run([resolved()], deterministic=False)
        self.assertEqual(deterministic.provenance["reproducibility"], "DETERMINISTIC_ADAPTER")
        self.assertEqual(real_like.provenance["reproducibility"], "NOT_GUARANTEED")

    def test_two_claims_need_two_findings(self) -> None:
        result, _, _ = run([resolved(("c1", "c2"), confidence=0.8)], two_claim_request())
        self.assertEqual([item["claim_ref"] for item in result.to_dict()["findings"]], ["c1", "c2"])

    def test_the_overall_confidence_is_the_weakest_finding(self) -> None:
        output = resolved(("c1", "c2"))
        output["findings"][0]["confidence"], output["findings"][1]["confidence"] = 0.95, 0.7
        result, _, _ = run([output], two_claim_request())
        self.assertEqual(result.to_dict()["confidence"], 0.7)

    def test_the_stored_result_does_not_contain_raw_evidence_or_hidden_reasoning(self) -> None:
        result, _, _ = run([resolved()])
        text = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn("keeps records", text)
        self.assertNotIn("Retention service manual", text)
        self.assertNotIn("chain_of_thought", text)
        self.assertLess(len(result.to_dict()["rationale"]), 1001)

    def test_findings_and_lists_are_stably_ordered_whatever_the_model_order(self) -> None:
        shuffled = resolved(("c1", "c2"))
        shuffled["findings"].reverse()
        shuffled["evidence_used"] = [DOC_REF]
        first, _, _ = run([resolved(("c1", "c2"))], two_claim_request())
        second, _, _ = run([shuffled], two_claim_request())
        self.assertEqual(first.to_dict()["findings"], second.to_dict()["findings"])
        self.assertEqual(first.result_id, second.result_id)


class EpistemicCeiling(unittest.TestCase):
    """AI_RECHECK nunca cria validação humana."""

    def test_every_finding_stays_inferred_and_passes_the_foundation_check(self) -> None:
        result, _, _ = run([resolved(("c1", "c2"))], two_claim_request())
        for item in result.to_dict()["findings"]:
            self.assertEqual(item["status"], "INFERRED")
            epistemic = {"status": item["status"], "confidence": item["confidence"], "provenance": [{"ref": ref} for ref in item["evidence_refs"]], "basis": item["basis"]}
            self.assertEqual(check_epistemic(epistemic), [])
        self.assertEqual(result.provenance["epistemic_ceiling"], "INFERRED")

    def test_a_prior_that_was_observed_is_not_promoted_either(self) -> None:
        from core.ai_recheck import PriorInterpretation

        result, _, _ = run([resolved()], request(prior_interpretation=PriorInterpretation("Visto no log.", "OBSERVED", 0.9)))
        self.assertEqual({item["status"] for item in result.to_dict()["findings"]}, {"INFERRED"})

    def test_a_second_pass_that_agrees_with_the_first_does_not_validate(self) -> None:
        first, _, _ = run([resolved()])
        second, _, _ = run([resolved()], request(round=2), policy=RecheckPolicy(max_rounds=2))
        for result in (first, second):
            data = result.to_dict()
            self.assertEqual({item["status"] for item in data["findings"]}, {"INFERRED"})
            self.assertNotIn("validation", json.dumps(data))
            self.assertNotIn("validated_by", json.dumps(data))
            self.assertNotIn("VALIDATED", json.dumps(data))

    def test_a_model_that_claims_validation_is_malformed_not_obeyed(self) -> None:
        cases = {
            "finding status": lambda o: o["findings"][0].__setitem__("status", "VALIDATED"),
            "top-level validated_by": lambda o: o.__setitem__("validated_by", "Fulano de Tal"),
            "validation block": lambda o: o.__setitem__("validation", {"method": "human_review"}),
            "status value": lambda o: o.__setitem__("status", "VALIDATED"),
        }
        for label, mutate in cases.items():
            output = resolved()
            mutate(output)
            result, _, _ = run([output])
            with self.subTest(label):
                self.assertEqual((result.status, failure_kind(result)), ("FAILED", "MALFORMED_RESPONSE"))
                self.assertFalse(result.proceeds)
                self.assertEqual(result.reusable_findings, ())

    def test_the_result_schema_has_no_way_to_carry_a_validation(self) -> None:
        self.assertEqual(ar.EPISTEMIC_CEILING, "INFERRED")
        text = (REPO / "schemas" / "ai-recheck-result.schema.json").read_text(encoding="utf-8")
        self.assertNotIn("validated_by", text)
        self.assertIn('"status": { "const": "INFERRED" }', text)


class OtherVerdicts(unittest.TestCase):
    def test_source_gap_records_what_is_missing_why_and_which_source(self) -> None:
        result, _, _ = run([source_gap(recommended="runtime")])
        data = result.to_dict()
        self.assertEqual((result.status, result.route, result.proceeds), ("SOURCE_GAP", "SOURCE_GAP", False))
        self.assertEqual(data["source_gap"]["missing"], ["O valor configurado em produção."])
        self.assertEqual(data["source_gap"]["insufficient_because"], "O documento só descreve o padrão.")
        self.assertEqual(data["source_gap"]["recommended_source"], "runtime")
        self.assertEqual(data["routing"]["needed_source"], "runtime")
        self.assertEqual(data["routing"]["destination"], "source_backlog")
        self.assertNotIn("findings", data)

    def test_source_gap_without_a_known_source_type_is_valid(self) -> None:
        result, _, _ = run([source_gap(recommended=None)])
        self.assertEqual(result.status, "SOURCE_GAP")
        self.assertNotIn("needed_source", result.routing)

    def test_every_source_kind_is_a_valid_recommendation_and_none_is_hardcoded(self) -> None:
        from core.model_task import SOURCE_KINDS

        for kind in SOURCE_KINDS:
            result, _, _ = run([source_gap(recommended=kind)])
            with self.subTest(kind):
                self.assertEqual(result.routing["needed_source"], kind)

    def test_human_decision_is_a_candidate_not_a_question_to_a_person(self) -> None:
        result, adapter, _ = run([human_decision()])
        data = result.to_dict()
        self.assertEqual((result.status, result.route, result.proceeds), ("HUMAN_DECISION", "HUMAN_DECISION", False))
        self.assertEqual(data["decision"]["options"], ["30 dias", "90 dias"])
        self.assertEqual(data["routing"]["destination"], "human_review_candidates")
        self.assertEqual(adapter.calls, 1)

    def test_inconclusive_is_a_valid_outcome_with_a_policy_defined_fallback(self) -> None:
        result, _, _ = run([inconclusive()])
        data = result.to_dict()
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(data["inconclusive"], {"unresolved": ["c1"], "reason": "MODEL_REPORTED"})
        self.assertEqual(result.route, "HUMAN_VALIDATION", "há uma interpretação a confirmar")
        no_prior, _, _ = run([inconclusive()], request(prior_interpretation=None))
        self.assertEqual((no_prior.route, no_prior.routing["needed_source"]), ("SOURCE_GAP", "human-expert"))
        forced, _, _ = run([inconclusive()], policy=RecheckPolicy(inconclusive_route="HUMAN_DECISION"))
        self.assertEqual(forced.route, "HUMAN_DECISION")
        self.assertFalse(result.proceeds or no_prior.proceeds or forced.proceeds)

    def test_inconclusive_may_keep_partial_findings_without_reusing_them(self) -> None:
        output = inconclusive(("c2",), findings=[finding("c1")])
        result, _, _ = run([output], two_claim_request())
        self.assertEqual([item["claim_ref"] for item in result.to_dict()["findings"]], ["c1"])
        self.assertEqual(result.reusable_findings, ())

    def test_a_resolved_answer_below_the_confidence_floor_becomes_inconclusive(self) -> None:
        result, adapter, _ = run([resolved(confidence=0.3)])
        data = result.to_dict()
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(data["inconclusive"], {"unresolved": ["c1"], "reason": "LOW_CONFIDENCE"})
        self.assertNotIn("findings", data)
        self.assertFalse(result.proceeds)
        self.assertEqual(adapter.calls, 1, "o rebaixamento não pede outra passada")

    def test_the_weakest_finding_decides_the_downgrade_and_the_floor_is_policy(self) -> None:
        mixed = resolved(("c1", "c2"))
        mixed["findings"][1]["confidence"] = 0.4
        self.assertEqual(run([mixed], two_claim_request())[0].status, "INCONCLUSIVE")
        self.assertEqual(run([mixed], two_claim_request(), policy=RecheckPolicy(min_resolved_confidence=0.4))[0].status, "RESOLVED")
        self.assertEqual(run([resolved(confidence=0.5)])[0].status, "RESOLVED", "exatamente no piso é aceito")
        self.assertEqual(run([resolved(confidence=0.0)], policy=RecheckPolicy(min_resolved_confidence=0.0))[0].status, "RESOLVED")


class UnauthorizedEvidence(unittest.TestCase):
    def test_a_reference_outside_the_authorization_rejects_the_whole_response(self) -> None:
        output = resolved(("c1", "c2"))
        output["findings"][1]["evidence_refs"] = ["web-page-42"]
        result, _, _ = run([output], two_claim_request())
        self.assertEqual((result.status, failure_kind(result)), ("FAILED", "UNAUTHORIZED_EVIDENCE"))
        self.assertIn("web-page-42", result.failure["message"])  # type: ignore[index]
        self.assertEqual(result.reusable_findings, ())
        self.assertNotIn("findings", result.to_dict(), "nada é aceito pela metade")

    def test_the_unknown_reference_is_caught_in_evidence_used_too(self) -> None:
        output = resolved()
        output["evidence_used"] = [DOC_REF, "invented"]
        self.assertEqual(failure_kind(run([output])[0]), "UNAUTHORIZED_EVIDENCE")

    def test_the_model_only_ever_sees_authorized_content(self) -> None:
        other = evidence("not-authorized", b"top secret", locator="input:other")
        _, adapter, _ = run([resolved()], request(), contents((DOC_REF, DOC), ("not-authorized", other and b"top secret")))
        task = adapter.tasks[0]
        self.assertEqual(sorted(task.allowed_context[EVIDENCE_KEY]), [DOC_REF])
        self.assertEqual([item.ref for item in task.evidence_refs], [DOC_REF])
        self.assertNotIn("top secret", json.dumps(task.to_dict()))

    def test_the_task_carries_no_locator_and_no_local_path(self) -> None:
        _, adapter, _ = run([resolved()])
        task = adapter.tasks[0].to_dict()
        self.assertTrue(all("locator" not in item for item in task["evidence_refs"]))
        from core.paths import find_local_paths

        self.assertEqual(find_local_paths({key: value for key, value in task.items() if key != "allowed_context"}), [])


class EvidenceIntegrity(unittest.TestCase):
    def test_evidence_modified_after_the_request_fails_before_the_model_is_called(self) -> None:
        result, adapter, reader = run([resolved()], data=contents((DOC_REF, DOC + b"edited after the request\n")))
        self.assertEqual((result.status, failure_kind(result)), ("FAILED", "EVIDENCE_CHANGED"))
        self.assertEqual(adapter.calls, 0)
        self.assertFalse(result.provenance["evidence_verified"])
        self.assertIn(DOC_REF, result.failure["message"])  # type: ignore[index]
        self.assertNotIn("edited", result.failure["message"])  # type: ignore[index]
        self.assertFalse(result.failure["retryable"])  # type: ignore[index]

    def test_a_change_of_size_only_is_also_a_change(self) -> None:
        self.assertEqual(failure_kind(run([resolved()], data=contents((DOC_REF, b"")))[0]), "EVIDENCE_CHANGED")

    def test_evidence_that_cannot_be_read_is_unavailable_and_retryable(self) -> None:
        result, adapter, _ = run([resolved()], data={})
        self.assertEqual((failure_kind(result), result.failure["retryable"], adapter.calls), ("EVIDENCE_UNAVAILABLE", True, 0))  # type: ignore[index]

    def test_a_reader_that_reports_a_change_or_misbehaves_is_handled(self) -> None:
        class Changed:
            def read(self, evidence: EvidenceRef) -> bytes:
                raise EvidenceChanged("mudou")

        class Boom:
            def read(self, evidence: EvidenceRef) -> bytes:
                raise OSError("disco")

        class NotBytes:
            def read(self, evidence: EvidenceRef) -> bytes:
                return "texto"  # type: ignore[return-value]

        for reader, kind in ((Changed(), "EVIDENCE_CHANGED"), (Boom(), "EVIDENCE_UNAVAILABLE"), (NotBytes(), "EVIDENCE_UNAVAILABLE")):
            adapter = ScriptedModelAdapter([resolved()])
            with self.subTest(kind):
                self.assertEqual(failure_kind(run_recheck(request(), adapter, reader)), kind)  # type: ignore[arg-type]
                self.assertEqual(adapter.calls, 0)

    def test_every_changed_reference_is_reported_not_just_the_first(self) -> None:
        a, b = evidence("a-1", b"one", locator="input:a"), evidence("b-1", b"two", locator="input:b")
        req = request(evidence=(a, b))
        result = run_recheck(req, ScriptedModelAdapter([]), MappingEvidenceReader({"a-1": b"X", "b-1": b"Y"}))
        self.assertIn("a-1", result.failure["message"])  # type: ignore[index]
        self.assertIn("b-1", result.failure["message"])  # type: ignore[index]

    def test_text_evidence_must_be_utf8(self) -> None:
        raw = b"\xff\xfe not utf-8 \x80"
        item = evidence("bin-1", raw, "documentation", "text", locator="input:bin")
        result = run_recheck(request(evidence=(item,)), ScriptedModelAdapter([resolved(refs=("bin-1",))]), MappingEvidenceReader({"bin-1": raw}))
        self.assertEqual(failure_kind(result), "EVIDENCE_UNDECODABLE")

    def test_the_evidence_is_read_once_per_reference(self) -> None:
        _, _, reader = run([resolved()])
        self.assertEqual(reader.reads, [DOC_REF])


class CapabilityAndPolicyGates(unittest.TestCase):
    def image_request(self, **overrides: object):
        shot = evidence("shot-1", IMAGE, "screenshot", "image", locator="input:shot", media_type="image/png")
        return request(evidence=(shot,), **overrides), {"shot-1": IMAGE}

    def test_an_adapter_without_vision_is_not_eligible_for_visual_evidence_and_is_not_called(self) -> None:
        req, data = self.image_request()
        result, adapter, reader = run([resolved(refs=("shot-1",))], req, data)
        self.assertEqual((result.status, failure_kind(result)), ("FAILED", "ADAPTER_INELIGIBLE"))
        self.assertEqual((adapter.calls, reader.reads), (0, []), "inelegível: nem o adaptador nem a evidência são tocados")
        self.assertIn("MISSING_CAPABILITY: vision", result.failure["reasons"])  # type: ignore[index]
        self.assertFalse(result.failure["retryable"])  # type: ignore[index]

    def test_an_adapter_with_vision_receives_the_image_as_base64(self) -> None:
        req, data = self.image_request()
        result, adapter, _ = run([resolved(refs=("shot-1",))], req, data, capabilities=("structured-output", "vision"))
        self.assertEqual(result.status, "RESOLVED")
        task = adapter.tasks[0]
        self.assertEqual(task.allowed_context[EVIDENCE_KEY]["shot-1"]["encoding"], "base64")
        self.assertIn("vision", task.required_capabilities)
        self.assertEqual(task.evidence_refs[0].media_type, "image/png")

    def test_a_request_can_require_any_capability_generically(self) -> None:
        req = request(required_capabilities=("long-context",))
        refused, adapter, _ = run([resolved()], req)
        self.assertEqual(failure_kind(refused), "ADAPTER_INELIGIBLE")
        self.assertEqual(adapter.calls, 0)
        accepted, _, _ = run([resolved()], req, capabilities=("structured-output", "long-context"))
        self.assertEqual(accepted.status, "RESOLVED")

    def test_structured_output_is_always_required(self) -> None:
        result, adapter, _ = run([resolved()], capabilities=())
        self.assertEqual((failure_kind(result), adapter.calls), ("ADAPTER_INELIGIBLE", 0))
        self.assertIn("MISSING_CAPABILITY: structured-output", result.failure["reasons"])  # type: ignore[index]

    def test_an_external_adapter_is_refused_when_the_request_keeps_data_local(self) -> None:
        result, adapter, reader = run([resolved()], data_boundary="EXTERNAL")
        self.assertEqual(failure_kind(result), "ADAPTER_INELIGIBLE")
        self.assertIn("BOUNDARY_EXCEEDED: EXTERNAL > LOCAL", result.failure["reasons"])  # type: ignore[index]
        self.assertEqual((adapter.calls, reader.reads), (0, []))

    def test_sensitive_or_unchecked_evidence_never_goes_to_an_external_provider(self) -> None:
        for label, sensitive in (("sensitive", True), ("unchecked", None)):
            req = request(max_boundary="EXTERNAL", evidence=(evidence(sensitive=sensitive),))
            result, adapter, reader = run([resolved()], req, data_boundary="EXTERNAL")
            with self.subTest(label):
                self.assertEqual(failure_kind(result), "ADAPTER_INELIGIBLE")
                self.assertTrue(any(reason.startswith("SENSITIVE_EVIDENCE_EXTERNAL") for reason in result.failure["reasons"]))  # type: ignore[index]
                self.assertEqual((adapter.calls, reader.reads), (0, []), "o dado não é nem lido")

    def test_verified_clear_evidence_may_go_external_when_the_request_allows_it(self) -> None:
        req = request(max_boundary="EXTERNAL", evidence=(evidence(sensitive=False),))
        result, adapter, _ = run([resolved()], req, data_boundary="EXTERNAL")
        self.assertEqual((result.status, adapter.calls), ("RESOLVED", 1))

    def test_sensitive_evidence_may_use_a_private_adapter(self) -> None:
        req = request(max_boundary="PRIVATE", evidence=(evidence(sensitive=True),))
        self.assertEqual(run([resolved()], req, data_boundary="PRIVATE")[0].status, "RESOLVED")

    def test_the_environment_ceiling_overrides_what_the_request_allows(self) -> None:
        req = request(max_boundary="EXTERNAL")
        result, adapter, _ = run([resolved()], req, policy=RecheckPolicy(boundary_ceiling="PRIVATE"), data_boundary="EXTERNAL")
        self.assertEqual((failure_kind(result), adapter.calls), ("ADAPTER_INELIGIBLE", 0))
        local_only, adapter_, _ = run([resolved()], req, policy=RecheckPolicy(boundary_ceiling="LOCAL"), data_boundary="PRIVATE")
        self.assertEqual((failure_kind(local_only), adapter_.calls), ("ADAPTER_INELIGIBLE", 0))
        self.assertEqual(run([resolved()], req, policy=RecheckPolicy(boundary_ceiling="LOCAL"), data_boundary="LOCAL")[0].status, "RESOLVED")

    def test_the_model_task_carries_the_effective_boundary(self) -> None:
        _, adapter, _ = run([resolved()], request(max_boundary="EXTERNAL"), policy=RecheckPolicy(boundary_ceiling="PRIVATE"), data_boundary="PRIVATE")
        self.assertEqual(adapter.tasks[0].max_boundary, "PRIVATE")

    def test_the_round_limit_stops_a_repeated_recheck(self) -> None:
        result, adapter, reader = run([resolved()], request(round=2))
        self.assertEqual((failure_kind(result), adapter.calls, reader.reads), ("ROUND_LIMIT", 0, []))
        allowed, _, _ = run([resolved()], request(round=2), policy=RecheckPolicy(max_rounds=2))
        self.assertEqual((allowed.status, allowed.provenance["round"]), ("RESOLVED", 2))


class TechnicalFailures(unittest.TestCase):
    """Falha técnica não é conhecimento nem é INCONCLUSIVE: não gera rota epistêmica e não pede outro recheck."""

    def assert_failed(self, result: AIRecheckResult, kind: str, retryable: bool) -> None:
        self.assertEqual((result.status, failure_kind(result), result.failure["retryable"]), ("FAILED", kind, retryable))  # type: ignore[index]
        self.assertEqual((result.route, result.proceeds), ("FAILURE", False))
        self.assertEqual(result.routing["destination"], "failure_handling")
        self.assertEqual(result.reusable_findings, ())
        self.assertEqual(VALIDATOR.validate(result.to_dict(), "ai-recheck-result"), [])
        for key in ("findings", "rationale", "confidence", "source_gap", "decision", "inconclusive"):
            self.assertNotIn(key, result.to_dict())

    def test_a_timeout_returned_by_the_adapter_is_retryable(self) -> None:
        self.assert_failed(run([ModelFailure("TIMEOUT", "demorou", True)])[0], "ADAPTER_FAILURE", True)
        self.assert_failed(run([TimeoutError("lento")])[0], "ADAPTER_FAILURE", True)
        self.assert_failed(run([ConnectionError("sem rede")])[0], "ADAPTER_FAILURE", True)

    def test_a_refusal_is_not_retryable(self) -> None:
        self.assert_failed(run([ModelFailure("REFUSED", "recusou", False)])[0], "ADAPTER_FAILURE", False)

    def test_an_exception_inside_a_non_conforming_adapter_is_caught(self) -> None:
        class Plain:
            descriptor = AdapterDescriptor("synthetic-plain", "1", ("structured-output",), "LOCAL")

            def __init__(self, error: Exception) -> None:
                self.error = error

            def execute(self, task):  # noqa: ANN001, ANN201
                raise self.error

        self.assert_failed(run_recheck(request(), Plain(RuntimeError("bug")), MappingEvidenceReader(contents())), "ADAPTER_EXCEPTION", False)
        self.assert_failed(run_recheck(request(), Plain(TimeoutError("lento")), MappingEvidenceReader(contents())), "ADAPTER_EXCEPTION", True)

    def test_a_script_exception_that_the_base_does_not_translate_is_recorded_not_raised(self) -> None:
        self.assert_failed(run([RuntimeError("bug interno")])[0], "ADAPTER_EXCEPTION", False)

    def test_exception_text_with_a_local_path_is_not_stored(self) -> None:
        result, _, _ = run([RuntimeError("falhou lendo C:\\Users\\alguem\\.credenciais")])
        self.assertNotIn("credenciais", json.dumps(result.to_dict()))
        self.assertEqual(failure_kind(result), "ADAPTER_EXCEPTION")

    def test_interrupting_the_process_is_never_swallowed(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            run([KeyboardInterrupt()])

    def test_a_response_that_is_not_a_model_result_is_malformed(self) -> None:
        for garbage in ("lixo", 42, None, {"status": "RESOLVED"}):
            with self.subTest(repr(garbage)):
                self.assert_failed(run([RawReturn(garbage)])[0], "MALFORMED_RESPONSE", False)

    def test_a_response_for_another_task_is_malformed(self) -> None:
        other = ModelResult.success("art:model.task:sha256:" + "b" * 64, ModelIdentity("synthetic-alpha"), resolved())
        self.assert_failed(run([RawReturn(other)])[0], "MALFORMED_RESPONSE", False)

    def test_malformed_output_is_retryable_only_for_a_non_deterministic_adapter(self) -> None:
        bad = {"status": "RESOLVED"}
        self.assert_failed(run([bad], deterministic=True)[0], "MALFORMED_RESPONSE", False)
        self.assert_failed(run([bad], deterministic=False)[0], "MALFORMED_RESPONSE", True)
        self.assert_failed(run([{"status": "RESOLVED", "rationale": "r", "evidence_used": ["zz"]}], deterministic=False)[0], "MALFORMED_RESPONSE", True)

    def test_a_malformed_response_still_records_who_answered_and_the_response_hash(self) -> None:
        result, adapter, _ = run([{"status": "RESOLVED"}], model_id="model-9")
        provenance = result.provenance
        self.assertEqual(provenance["model"]["model_id"], "model-9")
        self.assertRegex(provenance["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(provenance["task_id"], adapter.tasks[0].task_id)
        self.assertTrue(provenance["evidence_verified"])

    def test_a_failure_before_the_model_has_no_response_hash_or_task(self) -> None:
        result, _, _ = run([resolved()], request(round=2))
        for key in ("task_id", "response_sha256", "model"):
            self.assertNotIn(key, result.provenance)

    def test_adapter_failures_keep_the_usage_the_adapter_reported(self) -> None:
        failed = ModelResult.failed("x", ModelIdentity("synthetic-alpha"), ModelFailure("ERROR", "x"), {"input_tokens": 3})
        step = lambda task: ModelResult.failed(task.task_id, ModelIdentity("synthetic-alpha"), ModelFailure("ERROR", "falhou"), {"input_tokens": 3})  # noqa: E731
        self.assertEqual(failed.usage, {"input_tokens": 3})
        self.assertEqual(run([step])[0].provenance["usage"], {"input_tokens": 3})

    def test_technical_failure_is_distinct_from_a_valid_inconclusive(self) -> None:
        technical, _, _ = run([ModelFailure("ERROR", "x")])
        epistemic, _, _ = run([inconclusive()])
        self.assertEqual((technical.status, epistemic.status), ("FAILED", "INCONCLUSIVE"))
        self.assertNotEqual(technical.route, epistemic.route)
        self.assertNotIn(epistemic.route, ("FAILURE", "REUSE"))
        self.assertIsNone(epistemic.failure)

    def test_no_failure_asks_for_a_second_recheck(self) -> None:
        for script in ([ModelFailure("ERROR", "x")], [TimeoutError()], [{"status": "RESOLVED"}], [inconclusive()], [resolved(confidence=0.1)]):
            result, adapter, _ = run(script)
            self.assertNotEqual(result.route, "AI_RECHECK")
            self.assertEqual(adapter.calls, 1, "o executor nunca repete a chamada por conta própria")


class UsageAndDeterminism(unittest.TestCase):
    def test_the_same_input_through_a_deterministic_adapter_gives_identical_bytes(self) -> None:
        from core.serialization import canonical_pretty_bytes

        first = canonical_pretty_bytes(run([resolved()])[0].to_dict())
        second = canonical_pretty_bytes(run([resolved()])[0].to_dict())
        self.assertEqual(first, second)

    def test_the_inputs_are_not_mutated(self) -> None:
        req = request()
        before = req.to_dict()
        data = contents()
        script = [resolved()]
        run(script, req, data)
        self.assertEqual(req.to_dict(), before)
        self.assertEqual(data, contents())
        self.assertEqual(script, [resolved()])

    def test_the_attempt_is_metadata_and_never_changes_the_result_id(self) -> None:
        one, _, _ = run([resolved()], attempt=1)
        three, adapter, _ = run([resolved()], attempt=3)
        self.assertEqual(one.result_id, three.result_id)
        self.assertEqual((one.provenance["attempt"], three.provenance["attempt"]), (1, 3))
        self.assertEqual(adapter.tasks[0].attempt, 3)

    def test_usage_errors_raise_but_runtime_problems_never_do(self) -> None:
        adapter = ScriptedModelAdapter([])
        reader = MappingEvidenceReader(contents())
        with self.assertRaises(TypeError):
            run_recheck({"not": "a request"}, adapter, reader)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            run_recheck(request(), adapter, reader, policy="loose")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_recheck(request(), adapter, reader, attempt=0)
        with self.assertRaises(TypeError):
            run_recheck(request(), mock.Mock(descriptor="x"), reader)

    def test_the_tasks_instructions_are_short_generic_data(self) -> None:
        _, adapter, _ = run([resolved()])
        task = adapter.tasks[0]
        self.assertEqual(task.task_type, "ai-recheck")
        self.assertLessEqual(len(task.instructions), 8)
        self.assertTrue(all(len(item) < 300 for item in task.instructions))
        self.assertEqual(task.output_contract, ar.OUTPUT_CONTRACT)
        self.assertEqual(task.risk_level, "NORMAL")
        self.assertEqual(sorted(task.allowed_context), ["ambiguity", "claims", "evidence", "prior_interpretation", "reason", "subject"])

    def test_no_network_is_touched(self) -> None:
        with mock.patch("socket.socket", side_effect=AssertionError("rede!")), mock.patch("socket.create_connection", side_effect=AssertionError("rede!")):
            self.assertEqual(run([resolved()])[0].status, "RESOLVED")
            self.assertEqual(run([source_gap()])[0].status, "SOURCE_GAP")


class ProviderIndependence(unittest.TestCase):
    """O mesmo pedido, por dois adaptadores sintéticos de identidades diferentes, dá resultados válidos pelo mesmo motor."""

    @staticmethod
    def rule(task) -> dict:  # noqa: ANN001
        text = task.allowed_context[EVIDENCE_KEY][DOC_REF]["content"]
        return resolved() if "90 days" in text else source_gap()

    def adapters(self):  # noqa: ANN201
        alpha = ScriptedModelAdapter([resolved()], adapter_id="synthetic-alpha", model_id="alpha-1", capabilities=("structured-output",), data_boundary="LOCAL")
        beta = RuleBasedModelAdapter(
            self.rule, adapter_id="synthetic-beta", adapter_version="9", model_id="beta-2", capabilities=("structured-output", "long-context"),
            data_boundary="PRIVATE", deterministic=False, profile_id="local", usage={"input_tokens": 10, "output_tokens": 5},
        )
        return alpha, beta

    def test_same_request_two_adapters_both_contract_valid(self) -> None:
        req = request(max_boundary="PRIVATE")
        results = [run_recheck(req, adapter, MappingEvidenceReader(contents())) for adapter in self.adapters()]
        for result in results:
            self.assertEqual(VALIDATOR.validate(result.to_dict(), "ai-recheck-result"), [])
            self.assertEqual((result.status, result.route, result.request_id), ("RESOLVED", "REUSE", req.request_id))

    def test_the_content_is_identical_and_only_the_provenance_differs(self) -> None:
        req = request(max_boundary="PRIVATE")
        alpha, beta = (run_recheck(req, adapter, MappingEvidenceReader(contents())) for adapter in self.adapters())
        self.assertEqual(alpha.to_dict()["findings"], beta.to_dict()["findings"])
        self.assertEqual(alpha.result_id, beta.result_id)
        self.assertNotEqual(alpha.provenance["adapter"], beta.provenance["adapter"])
        self.assertEqual((alpha.provenance["adapter"]["adapter_id"], beta.provenance["adapter"]["adapter_id"]), ("synthetic-alpha", "synthetic-beta"))
        self.assertEqual(beta.provenance["adapter"]["profile_id"], "local")
        self.assertEqual((alpha.provenance["reproducibility"], beta.provenance["reproducibility"]), ("DETERMINISTIC_ADAPTER", "NOT_GUARANTEED"))
        self.assertEqual(alpha.provenance["evidence_snapshot"], beta.provenance["evidence_snapshot"])
        self.assertNotEqual(alpha.provenance["response_sha256"], "", "cada resposta tem o seu hash")

    def test_the_engine_is_literally_the_same_code_for_both(self) -> None:
        import inspect

        before = inspect.getsource(run_recheck)
        req = request(max_boundary="PRIVATE")
        for adapter in self.adapters():
            run_recheck(req, adapter, MappingEvidenceReader(contents()))
        self.assertEqual(before, inspect.getsource(run_recheck))

    def test_the_engine_never_branches_on_an_adapter_identity(self) -> None:
        text = (REPO / "core" / "recheck_engine.py").read_text(encoding="utf-8")
        for name in ("scripted", "rule-based", "synthetic", "adapter_id ==", "model_id =="):
            self.assertNotIn(name, text)

    def test_a_different_verdict_from_the_second_adapter_is_equally_accepted(self) -> None:
        req = request(max_boundary="PRIVATE")
        weak = RuleBasedModelAdapter(lambda task: source_gap(), adapter_id="synthetic-gamma", data_boundary="PRIVATE")
        result = run_recheck(req, weak, MappingEvidenceReader(contents()))
        self.assertEqual((result.status, result.route), ("SOURCE_GAP", "SOURCE_GAP"))


class ASecondSyntheticSoftware(unittest.TestCase):
    """Nenhum nome, tipo de fonte ou estrutura do primeiro software está no executor: um segundo, sem relação, roda igual."""

    def test_a_database_schema_question_runs_through_the_same_engine(self) -> None:
        req = database_request()

        def rule(task) -> dict:  # noqa: ANN001
            ddl = task.allowed_context[EVIDENCE_KEY][DDL_REF]["content"]
            nullable = "reorder_level INTEGER\n" in ddl and "reorder_level INTEGER NOT NULL" not in ddl
            return {
                "status": "RESOLVED" if nullable else "INCONCLUSIVE",
                "rationale": "A coluna não tem NOT NULL na definição.",
                "evidence_used": [DDL_REF],
                **({"findings": [finding("n1", (DDL_REF,), 0.95)]} if nullable else {"unresolved": ["n1"]}),
            }

        result = run_recheck(req, RuleBasedModelAdapter(rule), MappingEvidenceReader({DDL_REF: DDL}))
        self.assertEqual((result.status, result.route), ("RESOLVED", "REUSE"))
        self.assertEqual(result.to_dict()["findings"][0]["claim_ref"], "n1")
        self.assertEqual(VALIDATOR.validate(result.to_dict(), "ai-recheck-result"), [])

    def test_unrelated_softwares_have_unrelated_identities(self) -> None:
        self.assertNotEqual(request().request_id, database_request().request_id)
        self.assertEqual((request().software_id, database_request().software_id), ("synthetic-one", "synthetic-two"))

    def test_the_second_software_gets_all_the_same_guarantees(self) -> None:
        req = database_request()
        data = {DDL_REF: DDL}
        gap = run_recheck(req, ScriptedModelAdapter([source_gap(refs=(DDL_REF,), recommended="runtime")]), MappingEvidenceReader(data))
        self.assertEqual((gap.route, gap.routing["needed_source"]), ("SOURCE_GAP", "runtime"))
        unknown = run_recheck(req, ScriptedModelAdapter([resolved(("n1",), (DOC_REF,))]), MappingEvidenceReader(data))
        self.assertEqual(failure_kind(unknown), "UNAUTHORIZED_EVIDENCE")
        changed = run_recheck(req, ScriptedModelAdapter([]), MappingEvidenceReader({DDL_REF: DDL + b"-- edit\n"}))
        self.assertEqual(failure_kind(changed), "EVIDENCE_CHANGED")

    def test_runtime_traces_and_configuration_are_subjects_too(self) -> None:
        for kind, source in (("runtime-trace", "runtime"), ("configuration", "filesystem"), ("api", "runtime"), ("source-code", "source-code")):
            data = b"line one\nline two\n"
            item = EvidenceRef.of("e-1", data, source, contains_sensitive_data=False, locator="input:e")
            req = request(subject_kind=kind, subject_ref=f"{kind}:x", evidence=(item,), software_id=None)
            result = run_recheck(req, ScriptedModelAdapter([resolved(refs=("e-1",))]), MappingEvidenceReader({"e-1": data}))
            with self.subTest(kind):
                self.assertEqual(result.status, "RESOLVED")


if __name__ == "__main__":
    unittest.main()
