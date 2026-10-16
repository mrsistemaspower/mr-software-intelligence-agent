"""Ponte roteamento <-> AI_RECHECK: a decisão vira pedido do executor da Extração 3, e o resultado volta como nova decisão.

Usa o executor e o estágio de verdade, com adaptadores sintéticos. Cobre o handoff, o retorno de cada estado, a falha técnica, o laço
epistêmico limitado e a coerência com a escada da Fundação.
"""
from __future__ import annotations

import unittest

from adapters.models.scripted import ScriptedModelAdapter
from core.ai_recheck import AIRecheckRequest, AIRecheckResult, PriorInterpretation, RecheckPolicy
from core.analysis_signal import EvidenceBinding
from core.escalation import decide
from core.model_task import EvidenceRef, ModelContractError
from core.recheck_engine import run_recheck
from core.review_policy import default_policy
from core.review_recheck import (
    FOLLOW_UP_REEVALUATED,
    FOLLOW_UP_TECHNICAL_FAILURE,
    ReviewFollowUp,
    build_recheck_request,
    executor_policy,
    follow_up,
    plan_problems,
    records_from_result,
)
from core.review_routing import RecheckRecord, ReviewRouter
from core.schema_lite import Registry, Validator
from tests.helpers import TempDirCase
from tests.recheck_helpers import (
    DDL_REF, DOC, DOC_REF, database_request, evidence, finding, human_decision, inconclusive, request as plain_request, resolved, source_gap,
)
from tests.review_helpers import (
    SUBJECT_ONE,
    SUBJECT_TWO,
    catalog_for,
    doc_binding,
    doc_evidence,
    reader_for,
    recheck_signal,
    route,
    run_cycle,
    signal,
    variant,
)
from tests.test_recheck_stage import ALL_DONE, Flow

VALIDATOR = Validator(Registry())
ROUTER = ReviewRouter()
HASH = "a" * 64


def code_of(call) -> str:  # noqa: ANN001
    try:
        call()
    except ModelContractError as exc:
        return exc.code
    raise AssertionError("esperava ModelContractError")


def settle(decision, script, **kwargs):  # noqa: ANN001, ANN201
    """Executa o recheck da decisão e aplica o resultado. Devolve (pedido, resultado, adaptador, follow-up)."""
    router = kwargs.pop("router", ROUTER)
    request, result, adapter = run_cycle(decision, script, policy=router.policy, **kwargs)
    return request, result, adapter, follow_up(router, decision, request, result)


class TheDecisionBecomesTheExecutorsRequest(unittest.TestCase):
    def test_the_plan_is_enough_to_build_a_valid_request_for_the_extraction_3_executor(self) -> None:
        decision = route([recheck_signal("critical"), recheck_signal("high", target="c2")])
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        self.assertIsInstance(request, AIRecheckRequest)
        self.assertEqual((request.subject_ref, request.subject_kind, request.subject_title), (SUBJECT_ONE.ref, SUBJECT_ONE.kind, SUBJECT_ONE.title))
        self.assertEqual([item.claim_ref for item in request.claims], ["c1", "c2"])
        self.assertEqual([item.ref for item in request.evidence], [DOC_REF])
        self.assertEqual((request.max_boundary, request.risk_level, request.round, request.software_id), ("LOCAL", "HIGH", 1, "synthetic-one"))
        self.assertEqual(plan_problems(decision, request), [])
        self.assertEqual(AIRecheckRequest.from_dict(request.to_dict()), request)
        self.assertEqual(VALIDATOR.validate(request.to_dict(), "ai-recheck-request"), [])

    def test_the_request_is_deterministic(self) -> None:
        decision = route([recheck_signal("critical")])
        one = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        two = build_recheck_request(route([recheck_signal("critical")]), [doc_evidence()])
        self.assertEqual(one.request_id, two.request_id)

    def test_building_the_request_runs_nothing(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        build_recheck_request(route([recheck_signal("critical")]), catalog_for(SUBJECT_ONE))
        self.assertEqual(adapter.calls, 0)

    def test_the_evidence_must_be_authorized_and_unchanged(self) -> None:
        decision = route([recheck_signal("critical")])
        self.assertEqual(code_of(lambda: build_recheck_request(decision, {})), "EVIDENCE_NOT_AUTHORIZED")
        edited = evidence(DOC_REF, DOC + b"linha nova\n")
        self.assertEqual(code_of(lambda: build_recheck_request(decision, {DOC_REF: edited})), "EVIDENCE_CHANGED")
        self.assertEqual(code_of(lambda: build_recheck_request(decision, ["not evidence"])), "INVALID_CATALOG")  # type: ignore[list-item]

    def test_only_an_ai_recheck_decision_makes_a_request(self) -> None:
        for signals in ([], [signal("LOW_CONFIDENCE", "low")], [signal("MISSING_EVIDENCE", "high")], [signal("POLICY_CONFLICT")]):
            decision = route(signals)
            with self.subTest(decision.decision_class):
                self.assertEqual(code_of(lambda: build_recheck_request(decision, catalog_for(SUBJECT_ONE))), "NOT_A_RECHECK")
        with self.assertRaises(TypeError):
            build_recheck_request("nope", {})  # type: ignore[arg-type]

    def test_the_data_boundary_only_ever_tightens(self) -> None:
        relaxed = variant(lambda d: d["recheck"].update(default_max_boundary="PRIVATE"))
        decision = route([recheck_signal("critical")], policy=relaxed)
        catalog = catalog_for(SUBJECT_ONE)
        self.assertEqual(build_recheck_request(decision, catalog).max_boundary, "PRIVATE")
        self.assertEqual(build_recheck_request(decision, catalog, boundary_ceiling="LOCAL").max_boundary, "LOCAL")
        self.assertEqual(build_recheck_request(decision, catalog, boundary_ceiling="EXTERNAL").max_boundary, "PRIVATE", "o ambiente não afrouxa")
        with self.assertRaises(ModelContractError):
            build_recheck_request(decision, catalog, boundary_ceiling="OFFSITE")

    def test_a_privacy_restriction_on_the_signals_reaches_the_request(self) -> None:
        relaxed = variant(lambda d: d["recheck"].update(default_max_boundary="EXTERNAL"))
        restricted = signal("PRIVACY_OR_SECURITY_CONCERN", "high", qualifier="systemic_handled", boundary_limit="LOCAL", target_ref="p1")
        decision = route([recheck_signal("critical"), restricted], policy=relaxed)
        self.assertEqual(build_recheck_request(decision, catalog_for(SUBJECT_ONE)).max_boundary, "LOCAL")

    def test_sensitive_evidence_keeps_its_flag_so_the_executors_gate_can_refuse_an_external_provider(self) -> None:
        relaxed = variant(lambda d: d["recheck"].update(default_max_boundary="EXTERNAL"))
        decision = route([recheck_signal("critical")], policy=relaxed)
        sensitive = evidence(DOC_REF, DOC, sensitive=True)
        request = build_recheck_request(decision, {DOC_REF: sensitive})
        adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        result = run_recheck(request, adapter, reader_for(SUBJECT_ONE))
        self.assertEqual((result.status, result.failure["kind"], result.failure["retryable"]), ("FAILED", "ADAPTER_INELIGIBLE", False))
        self.assertEqual(adapter.calls, 0)

    def test_optional_context_passes_through(self) -> None:
        decision = route([recheck_signal("critical")])
        prior = PriorInterpretation("Provavelmente 30 dias.", "INFERRED", 0.3)
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE), prior_interpretation=prior, required_capabilities=("long-context",))
        self.assertEqual(request.prior_interpretation, prior)
        self.assertIn("long-context", request.required_capabilities)

    def test_the_executor_policy_is_aligned_with_the_routing_policy(self) -> None:
        wide = variant(lambda d: d["recheck"].update(max_rounds=3))
        aligned = executor_policy(wide, min_resolved_confidence=0.7)
        self.assertEqual((aligned.max_rounds, aligned.boundary_ceiling, aligned.min_resolved_confidence), (3, "LOCAL", 0.7))
        self.assertEqual(executor_policy(default_policy()).max_rounds, 1)

    def test_a_plan_mismatch_is_reported(self) -> None:
        decision = route([recheck_signal("critical")])
        other = route([recheck_signal("critical", target="c9")])
        wrong = build_recheck_request(other, catalog_for(SUBJECT_ONE))
        self.assertIn("afirmações diferentes", plan_problems(decision, wrong))
        self.assertIn("assunto diferente", plan_problems(decision, database_request()))
        self.assertEqual(plan_problems(decision, plain_request()), [], "o pedido de outro fluxo que descreve o mesmo plano coincide")
        self.assertIn("não AI_RECHECK", plan_problems(route([]), wrong)[0])


class EachVerdictRoutesTheDecisionAgain(unittest.TestCase):
    def test_resolved_clears_the_signal_and_the_decision_is_reevaluated(self) -> None:
        decision = route([recheck_signal("critical")])
        request, result, adapter, follow = settle(decision, [resolved()])
        self.assertEqual((follow.kind, follow.decision.decision_class), (FOLLOW_UP_REEVALUATED, "AUTO_PASS"))
        self.assertEqual(adapter.calls, 1)
        (record,) = follow.records
        self.assertEqual((record.status, record.round, record.request_id, record.result_id), ("RESOLVED", 1, request.request_id, result.result_id))
        (assessment,) = follow.decision.assessments
        self.assertEqual((assessment["role"], assessment["why"], assessment["action"]), ("addressed", "recheck-resolved", "OBSERVE"))
        self.assertEqual(follow.decision.secondary_signals, ())
        self.assertEqual(follow.decision.primary_reasons, ())
        self.assertTrue(follow.epistemic_budget_consumed)
        self.assertTrue(ROUTER.reproduces(follow.decision))
        self.assertEqual(VALIDATOR.validate(follow.decision.to_dict(), "review-decision"), [])

    def test_resolved_leaves_the_rest_of_the_signals_to_route_normally(self) -> None:
        decision = route([recheck_signal("critical"), signal("LOW_CONFIDENCE", "low", target_ref="other")])
        self.assertEqual(decision.decision_class, "AI_RECHECK")
        follow = settle(decision, [resolved()])[3]
        self.assertEqual(follow.decision.decision_class, "SAMPLE_QA")
        self.assertEqual({item["role"] for item in follow.decision.assessments}, {"addressed", "primary"})

    def test_resolved_never_creates_human_validation_or_promotes_anything(self) -> None:
        request, result, adapter, follow = settle(route([recheck_signal("critical")]), [resolved()])
        self.assertEqual({item["status"] for item in result.reusable_findings}, {"INFERRED"})
        self.assertEqual(follow.decision.data["validation_effect"], "NONE")
        self.assertFalse(follow.decision.requires_named_human)

    def test_a_resolved_answer_below_the_executors_confidence_floor_is_not_resolved_here_either(self) -> None:
        decision = route([recheck_signal("critical")])
        request, result, adapter, follow = settle(decision, [resolved(confidence=0.3)])
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(follow.decision.decision_class, "HUMAN_DECISION")
        self.assertEqual(follow.records[0].status, "INCONCLUSIVE")

    def test_source_gap_feedback_becomes_a_source_gap_with_what_the_model_says_is_missing(self) -> None:
        decision = route([recheck_signal("critical")])
        request, result, adapter, follow = settle(decision, [source_gap(recommended="runtime")])
        new = follow.decision
        self.assertEqual((new.decision_class, new.requires_named_human, new.human_decision_candidates), ("SOURCE_GAP", False, ()))
        (requirement,) = new.source_gap_requirements
        self.assertEqual(requirement.origin, "ai-recheck")
        self.assertEqual(requirement.missing_information, "O valor configurado em produção.")
        self.assertEqual(requirement.why_needed, "O documento só descreve o padrão.")
        self.assertEqual((requirement.recommended_source_types, requirement.blocking), (("runtime",), True))
        self.assertEqual(new.assessments[0]["why"], "recheck-source-gap")
        self.assertEqual(VALIDATOR.validate(requirement.to_dict(), "source-gap-requirement"), [])

    def test_source_gap_feedback_maps_the_registered_kind_to_the_generic_type(self) -> None:
        follow = settle(route([recheck_signal("critical")]), [source_gap(recommended="database-schema")])[3]
        self.assertEqual(follow.decision.source_gap_requirements[0].recommended_source_types, ("database",))
        none = settle(route([recheck_signal("critical")]), [source_gap(recommended=None)])[3]
        self.assertEqual(none.decision.source_gap_requirements[0].recommended_source_types, ("human-expert",), "sem indicação, a política escolhe")

    def test_source_gap_feedback_stays_a_source_gap_even_when_the_point_is_not_blocking(self) -> None:
        eager = variant(lambda d: d["rules"]["AMBIGUOUS_INTERPRETATION"]["actions"].update(medium="AI_RECHECK"))
        router = ReviewRouter(eager)
        decision = router.route(SUBJECT_ONE, [recheck_signal("medium")])
        self.assertEqual(decision.decision_class, "AI_RECHECK")
        follow = settle(decision, [source_gap()], router=router)[3]
        self.assertEqual(follow.decision.decision_class, "SOURCE_GAP")
        self.assertFalse(follow.decision.source_gap_requirements[0].blocking)

    def test_human_decision_feedback_becomes_a_candidate_with_the_models_question_and_options(self) -> None:
        decision = route([recheck_signal("critical")])
        follow = settle(decision, [human_decision()])[3]
        new = follow.decision
        self.assertEqual((new.decision_class, new.requires_named_human), ("HUMAN_DECISION", True))
        (candidate,) = new.human_decision_candidates
        self.assertEqual(candidate.question, "Qual prazo vale: 30 ou 90 dias?")
        self.assertEqual(candidate.options, ("30 dias", "90 dias"))
        self.assertEqual(candidate.decision_kind, "resolve_conflict")
        self.assertEqual(candidate.attempted_before, (("ai-recheck", "HUMAN_DECISION na passada 1"),))
        self.assertIn("alternativas", candidate.why_ai_cannot_resolve)
        self.assertEqual(VALIDATOR.validate(candidate.to_dict(), "human-decision-candidate"), [])

    def test_inconclusive_escalates_by_the_policy_not_back_into_a_loop(self) -> None:
        decision = route([recheck_signal("critical")])
        follow = settle(decision, [inconclusive()])[3]
        self.assertEqual(follow.decision.decision_class, "HUMAN_DECISION")
        (candidate,) = follow.decision.human_decision_candidates
        self.assertEqual(candidate.decision_kind, "resolve_residue")
        self.assertEqual(candidate.attempted_before, (("ai-recheck", "INCONCLUSIVE na passada 1"),))
        self.assertEqual(follow.decision.assessments[0]["why"], "recheck-inconclusive")

    def test_the_escalation_after_inconclusive_is_configurable_but_never_a_recheck(self) -> None:
        as_gap = ReviewRouter(variant(lambda d: d["recheck"]["on_inconclusive"].update(high="SOURCE_GAP", critical="SOURCE_GAP")))
        decision = as_gap.route(SUBJECT_ONE, [recheck_signal("critical")])
        follow = settle(decision, [inconclusive()], router=as_gap)[3]
        self.assertEqual(follow.decision.decision_class, "SOURCE_GAP")
        self.assertFalse(follow.decision.requires_named_human)
        sampled = ReviewRouter(variant(lambda d: d["recheck"]["on_inconclusive"].update(high="SAMPLE_QA", critical="SAMPLE_QA")))
        decision = sampled.route(SUBJECT_ONE, [recheck_signal("critical")])
        self.assertEqual(settle(decision, [inconclusive()], router=sampled)[3].decision.decision_class, "SAMPLE_QA")

    def test_a_partly_resolved_inconclusive_clears_what_was_resolved_and_escalates_the_rest(self) -> None:
        decision = route([recheck_signal("critical", target="c1"), recheck_signal("critical", target="c2")])
        script = [inconclusive(unresolved=("c1",), findings=[finding("c2")])]
        request, result, adapter, follow = settle(decision, script)
        self.assertEqual(sorted(record.status for record in follow.records), ["INCONCLUSIVE", "RESOLVED"])
        roles = {item["role"] for item in follow.decision.assessments}
        self.assertEqual(roles, {"addressed", "primary"})
        (candidate,) = follow.decision.human_decision_candidates
        self.assertEqual(candidate.target_ref, "c1")

    def test_the_second_software_goes_through_the_same_loop(self) -> None:
        decision = route([recheck_signal("critical", target="n1", subject=SUBJECT_TWO)], subject=SUBJECT_TWO)
        request, result, adapter, follow = settle(decision, [resolved(("n1",), (DDL_REF,))], subject=SUBJECT_TWO)
        self.assertEqual((request.software_id, follow.decision.decision_class), ("synthetic-two", "AUTO_PASS"))


class ATechnicalFailureIsNotAVerdict(unittest.TestCase):
    def failed(self, script, **adapter):  # noqa: ANN001, ANN201
        decision = route([recheck_signal("critical")])
        request, result, adapter_, follow = settle(decision, script, **adapter)
        return decision, result, adapter_, follow

    def test_a_retryable_failure_creates_no_decision_and_spends_no_round(self) -> None:
        decision, result, adapter, follow = self.failed([TimeoutError("lento")])
        self.assertEqual(result.status, "FAILED")
        self.assertEqual((follow.kind, follow.decision, follow.records), (FOLLOW_UP_TECHNICAL_FAILURE, None, ()))
        self.assertIs(follow.retryable, True)
        self.assertFalse(follow.epistemic_budget_consumed)
        self.assertEqual(follow.failure["kind"], "ADAPTER_FAILURE")

    def test_a_non_retryable_failure_is_reported_as_such(self) -> None:
        decision, result, adapter, follow = self.failed([resolved()], data_boundary="EXTERNAL")
        self.assertEqual((follow.failure["kind"], follow.retryable), ("ADAPTER_INELIGIBLE", False))
        self.assertEqual(adapter.calls, 0)

    def test_the_retry_flag_is_exactly_the_executors(self) -> None:
        for script, adapter_fields in (([TimeoutError("x")], {}), ([resolved()], {"data_boundary": "EXTERNAL"}), ([{"status": "RESOLVED"}], {})):
            decision, result, adapter, follow = self.failed(script, **adapter_fields)
            with self.subTest(kind=follow.failure["kind"]):
                self.assertEqual(follow.retryable, result.failure["retryable"])

    def test_after_a_failure_the_original_decision_still_stands_and_asks_for_the_same_recheck(self) -> None:
        decision, result, adapter, follow = self.failed([TimeoutError("lento")])
        again = ROUTER.route(decision.subject, decision.signals, history=decision.history)
        self.assertEqual(again.decision_id, decision.decision_id)
        self.assertEqual(again.decision_class, "AI_RECHECK")
        self.assertEqual(again.history, ())

    def test_a_failure_then_a_success_uses_the_single_epistemic_round(self) -> None:
        decision = route([recheck_signal("critical")])
        adapter = ScriptedModelAdapter([TimeoutError("lento"), resolved()])
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        policy = executor_policy(default_policy())
        first = follow_up(ROUTER, decision, request, run_recheck(request, adapter, reader_for(SUBJECT_ONE), policy=policy, attempt=1))
        second = follow_up(ROUTER, decision, request, run_recheck(request, adapter, reader_for(SUBJECT_ONE), policy=policy, attempt=2))
        self.assertEqual((first.kind, second.kind), (FOLLOW_UP_TECHNICAL_FAILURE, FOLLOW_UP_REEVALUATED))
        self.assertEqual(second.decision.decision_class, "AUTO_PASS")
        self.assertEqual(len(second.decision.history), 1)

    def test_the_follow_up_shape_is_validated(self) -> None:
        with self.assertRaises(ModelContractError):
            ReviewFollowUp(FOLLOW_UP_REEVALUATED)
        with self.assertRaises(ModelContractError):
            ReviewFollowUp(FOLLOW_UP_TECHNICAL_FAILURE, failure=None)
        with self.assertRaises(ModelContractError):
            ReviewFollowUp("SOMETHING")


class AResultIsAppliedOnlyToTheRequestThatAskedForIt(unittest.TestCase):
    def test_a_request_that_is_not_the_plans_is_refused(self) -> None:
        decision = route([recheck_signal("critical")])
        other = route([recheck_signal("critical", target="c9")])
        wrong = build_recheck_request(other, catalog_for(SUBJECT_ONE))
        result = run_recheck(wrong, ScriptedModelAdapter([resolved(("c9",))]), reader_for(SUBJECT_ONE))
        self.assertEqual(code_of(lambda: follow_up(ROUTER, decision, wrong, result)), "REQUEST_MISMATCH")

    def test_a_result_of_another_request_is_refused(self) -> None:
        decision = route([recheck_signal("critical")])
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        foreign = run_recheck(plain_request(), ScriptedModelAdapter([resolved()]), reader_for(SUBJECT_ONE))
        self.assertEqual(code_of(lambda: follow_up(ROUTER, decision, request, foreign)), "RESULT_MISMATCH")
        with self.assertRaises(TypeError):
            records_from_result("nope", foreign)  # type: ignore[arg-type]

    def test_only_an_ai_recheck_decision_accepts_a_result(self) -> None:
        decision = route([recheck_signal("critical")])
        request, result, adapter = run_cycle(decision, [resolved()])
        self.assertEqual(code_of(lambda: follow_up(ROUTER, route([]), request, result)), "REQUEST_MISMATCH")
        with self.assertRaises(TypeError):
            follow_up("router", decision, request, result)  # type: ignore[arg-type]


class TheEpistemicLoopIsBounded(unittest.TestCase):
    @staticmethod
    def drive(rounds: int, script_item) -> "tuple[object, ScriptedModelAdapter, list]":  # noqa: ANN001
        policy = variant(lambda d: d["recheck"].update(max_rounds=rounds))
        router = ReviewRouter(policy)
        adapter = ScriptedModelAdapter([script_item] * 20)
        decision = router.route(SUBJECT_ONE, [recheck_signal("critical")])
        plan_rounds = []
        for _ in range(12):
            if decision.decision_class != "AI_RECHECK":
                break
            plan_rounds.append(decision.recheck_plan["round"])
            request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
            result = run_recheck(request, adapter, reader_for(SUBJECT_ONE), policy=executor_policy(policy))
            follow = follow_up(router, decision, request, result)
            assert follow.decision is not None, follow.failure
            decision = follow.decision
        return decision, adapter, plan_rounds

    def test_an_always_inconclusive_model_is_asked_exactly_as_many_times_as_the_policy_allows(self) -> None:
        for rounds in (1, 2, 3):
            decision, adapter, plan_rounds = self.drive(rounds, inconclusive())
            with self.subTest(rounds=rounds):
                self.assertEqual(adapter.calls, rounds)
                self.assertEqual(plan_rounds, list(range(1, rounds + 1)))
                self.assertEqual(decision.decision_class, "HUMAN_DECISION")
                self.assertEqual(len(decision.history), rounds)
                self.assertEqual([record.round for record in decision.history], list(range(1, rounds + 1)))

    def test_a_definitive_verdict_ends_the_loop_at_once_whatever_the_budget(self) -> None:
        for item, expected in ((resolved(), "AUTO_PASS"), (source_gap(), "SOURCE_GAP"), (human_decision(), "HUMAN_DECISION")):
            decision, adapter, plan_rounds = self.drive(3, item)
            with self.subTest(expected):
                self.assertEqual((adapter.calls, decision.decision_class), (1, expected))

    def test_a_routing_budget_larger_than_the_executors_fails_deterministically_at_the_limit(self) -> None:
        wide = ReviewRouter(variant(lambda d: d["recheck"].update(max_rounds=2)))
        decision = wide.route(SUBJECT_ONE, [recheck_signal("critical")])
        adapter = ScriptedModelAdapter([inconclusive(), inconclusive()])
        narrow = RecheckPolicy(max_rounds=1, boundary_ceiling="LOCAL")
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        first = follow_up(wide, decision, request, run_recheck(request, adapter, reader_for(SUBJECT_ONE), policy=narrow))
        self.assertEqual(first.decision.decision_class, "AI_RECHECK")
        second_request = build_recheck_request(first.decision, catalog_for(SUBJECT_ONE))
        self.assertEqual(second_request.round, 2)
        failed = run_recheck(second_request, adapter, reader_for(SUBJECT_ONE), policy=narrow)
        self.assertEqual((failed.status, failed.failure["kind"], failed.failure["retryable"]), ("FAILED", "ROUND_LIMIT", False))
        self.assertEqual(adapter.calls, 1, "o executor nem chamou o modelo")
        stuck = follow_up(wide, first.decision, second_request, failed)
        self.assertEqual((stuck.kind, stuck.epistemic_budget_consumed), (FOLLOW_UP_TECHNICAL_FAILURE, False))

    def test_new_or_changed_evidence_reopens_the_right_to_a_recheck(self) -> None:
        decision = route([recheck_signal("critical")])
        request, result, adapter, follow = settle(decision, [inconclusive()])
        self.assertEqual(follow.decision.decision_class, "HUMAN_DECISION")
        changed = recheck_signal("critical", evidence=(EvidenceBinding(DOC_REF, "b" * 64),))
        again = ROUTER.route(SUBJECT_ONE, [changed], history=follow.decision.history)
        self.assertEqual((again.decision_class, again.recheck_plan["round"]), ("AI_RECHECK", 1))
        wider = recheck_signal("critical", evidence=(doc_binding(), EvidenceBinding("doc-extra", "c" * 64)))
        self.assertEqual(ROUTER.route(SUBJECT_ONE, [wider], history=follow.decision.history).decision_class, "AI_RECHECK")

    def test_a_record_for_another_target_does_not_spend_this_targets_budget(self) -> None:
        record = RecheckRecord(("c9",), (doc_binding(),), 1, "INCONCLUSIVE", "art:ai-recheck.request:sha256:" + HASH, "art:ai-recheck.result:sha256:" + HASH, {"reason": "MODEL_REPORTED", "unresolved": ["c9"]})
        self.assertEqual(route([recheck_signal("critical")], history=[record]).decision_class, "AI_RECHECK")

    def test_a_signal_that_comes_back_after_a_resolved_recheck_is_not_rechecked_again(self) -> None:
        decision = route([recheck_signal("critical")])
        follow = settle(decision, [resolved()])[3]
        again = ROUTER.route(SUBJECT_ONE, [recheck_signal("critical")], history=follow.decision.history)
        self.assertEqual(again.decision_class, "AUTO_PASS")


class TheStageOfExtraction3ConsumesTheHandoffUnchanged(TempDirCase):
    def test_a_routed_request_runs_as_an_ordinary_stage_and_the_result_routes_back(self) -> None:
        decision = route([recheck_signal("critical")])
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]), make_request=lambda content: request)
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        stored = AIRecheckResult.from_dict(flow.output("recheck", "result"))
        self.assertEqual(stored.request_id, request.request_id)
        follow = follow_up(ROUTER, decision, request, stored)
        self.assertEqual(follow.decision.decision_class, "AUTO_PASS")

    def test_a_stage_failure_routes_back_as_a_technical_failure_without_spending_the_round(self) -> None:
        decision = route([recheck_signal("critical")])
        request = build_recheck_request(decision, catalog_for(SUBJECT_ONE))
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL"), make_request=lambda content: request, attempts=5)
        first = flow.run()
        self.assertEqual(flow.states(first)["recheck"], "FAILED")
        second = flow.run()
        self.assertEqual(second.plan.of("recheck").reason, "NON_RETRYABLE_FAILURE")
        self.assertEqual(flow.stage_record(second, "recheck").attempts, 1)


class ConsistentWithTheFoundationLadder(unittest.TestCase):
    """O roteamento por sinais coincide com a escada de `core.escalation` nas situações que as duas conhecem."""

    def test_the_same_situations_reach_the_same_destination(self) -> None:
        self.assertEqual(decide({"evidence_present": False}).route, "SOURCE_GAP")
        self.assertEqual(route([signal("MISSING_EVIDENCE", "high")]).decision_class, "SOURCE_GAP")

        self.assertEqual(decide({"confidence": 0.2}).route, "AI_RECHECK")
        self.assertEqual(route([signal("LOW_CONFIDENCE", "critical", confidence="low", evidence=(doc_binding(),))]).decision_class, "AI_RECHECK")

        self.assertEqual(decide({"choice_options": 2}).route, "HUMAN_DECISION")
        self.assertEqual(route([signal("CONFLICTING_EVIDENCE", "high", qualifier="direct_conflict")]).decision_class, "HUMAN_DECISION")

    def test_the_foundations_human_validation_is_a_human_decision_of_kind_validate_claim(self) -> None:
        from core.analysis_signal import ClaimAssessment

        self.assertEqual(decide({"impact": "high", "inference_to_confirm": True}).route, "HUMAN_VALIDATION")
        decision = route([], claims=[ClaimAssessment("claim-1", "afirmação", "INFERRED", effects=("deletes-data",))])
        self.assertEqual((decision.decision_class, decision.human_decision_candidates[0].decision_kind), ("HUMAN_DECISION", "validate_claim"))

    def test_an_unresolved_recheck_escalates_to_a_person_in_both(self) -> None:
        self.assertEqual(decide({"confidence": 0.2, "recheck_done": True, "inference_to_confirm": True}).route, "HUMAN_VALIDATION")
        record = RecheckRecord(("c1",), (doc_binding(),), 1, "INCONCLUSIVE", "art:ai-recheck.request:sha256:" + HASH, "art:ai-recheck.result:sha256:" + HASH, {"reason": "MODEL_REPORTED", "unresolved": ["c1"]})
        self.assertEqual(route([recheck_signal("critical")], history=[record]).decision_class, "HUMAN_DECISION")

    def test_validated_knowledge_is_not_escalated_by_the_foundation_and_not_derived_here(self) -> None:
        from core.analysis_signal import ClaimAssessment

        self.assertEqual(decide({"status": "VALIDATED", "confidence": 0.1}).route, "NO_ESCALATION")
        self.assertEqual(route([], claims=[ClaimAssessment("claim-1", "afirmação", "VALIDATED", effects=("deletes-data",))]).decision_class, "AUTO_PASS")


class RecordsAreCheckedLikeTheirWireForm(unittest.TestCase):
    def test_records_round_trip_and_reject_malformed_details(self) -> None:
        request, result, adapter = run_cycle(route([recheck_signal("critical")]), [inconclusive()])
        (record,) = records_from_result(request, result)
        self.assertEqual(RecheckRecord.from_dict(record.to_dict()), record)
        bad = dict(record.to_dict(), detail={"reason": "not a code", "unresolved": ["c1"]})
        self.assertEqual(code_of(lambda: RecheckRecord.from_dict(bad)), "INVALID_DETAIL")
        missing = dict(record.to_dict(), detail={"reason": "MODEL_REPORTED"})
        self.assertEqual(code_of(lambda: RecheckRecord.from_dict(missing)), "INVALID_DETAIL")
        with self.assertRaises(ModelContractError):
            RecheckRecord.from_dict({"target_refs": ["c1"]})

    def test_covers_requires_the_same_target_and_contained_evidence(self) -> None:
        record = RecheckRecord(("c1",), (doc_binding(),), 1, "RESOLVED", "art:ai-recheck.request:sha256:" + HASH, "art:ai-recheck.result:sha256:" + HASH, {})
        self.assertTrue(record.covers("c1", [doc_binding()]))
        self.assertTrue(record.covers("c1", []))
        self.assertFalse(record.covers("c2", [doc_binding()]))
        self.assertFalse(record.covers("c1", [EvidenceBinding(DOC_REF, "d" * 64)]))


if __name__ == "__main__":
    unittest.main()
