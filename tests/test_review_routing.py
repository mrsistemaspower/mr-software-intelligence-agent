"""Roteamento de revisão: sinais + política -> decisão. Cada classe, a ordem entre elas, os eixos separados, alto impacto,
privacidade e fronteira de dados, identidade determinística, versionamento da política, amostragem, adulteração e um segundo software.

Os testes de handoff e de retorno do AI_RECHECK estão em `test_review_recheck.py`.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
import unittest

from core import review_routing as rr
from core.analysis_signal import (
    CONFIDENCE_BANDS,
    CRITICALITIES,
    SOURCE_TYPES,
    AnalysisSignal,
    ClaimAssessment,
    Position,
    ReviewContractError,
    SourceNeed,
)
from core.model_task import ModelContractError
from core.orchestrator import StageFailure
from core.resume import Decision
from core.review_policy import default_policy
from core.review_routing import HumanDecisionCandidate, RecheckRecord, ReviewDecision, ReviewRouter, ReviewSubject, SourceGapRequirement
from core.schema_lite import Registry, Validator
from core.serialization import canonical_pretty_bytes
from tests.helpers import REPO, TempDirCase
from tests.recheck_helpers import DOC_REF
from tests.review_helpers import SUBJECT_ONE, SUBJECT_TWO, doc_binding, ddl_binding, recheck_signal, route, signal, variant
from tests.wf_helpers import Scenario, spec

VALIDATOR = Validator(Registry())


def code_of(call) -> str:  # noqa: ANN001
    try:
        call()
    except ModelContractError as exc:
        return exc.code
    raise AssertionError("esperava ModelContractError")


def walk_keys(node: object) -> set:
    found: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= walk_keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= walk_keys(value)
    return found


class EveryClassHasItsOwnTrigger(unittest.TestCase):
    def test_no_signals_is_auto_pass_with_nothing_to_do(self) -> None:
        decision = route([])
        self.assertEqual(decision.decision_class, "AUTO_PASS")
        self.assertEqual((decision.primary_reasons, decision.secondary_signals, decision.assessments), ((), (), ()))
        self.assertFalse(decision.requires_named_human)
        self.assertEqual(decision.next_action, "PROCEED")
        self.assertIsNone(decision.recheck_plan)
        self.assertEqual((decision.source_gap_requirements, decision.human_decision_candidates), ((), ()))
        self.assertEqual(decision.quality_sampling["purpose"], "audit")

    def test_low_noncritical_confidence_goes_to_quality_sampling_by_default(self) -> None:
        for criticality in ("low", "medium"):
            decision = route([signal("LOW_CONFIDENCE", criticality, confidence="low", evidence=(doc_binding(),))])
            with self.subTest(criticality):
                self.assertEqual(decision.decision_class, "SAMPLE_QA")
                self.assertFalse(decision.requires_named_human)
                self.assertEqual(decision.quality_sampling["purpose"], "qa")

    def test_low_noncritical_confidence_follows_a_configured_policy_instead(self) -> None:
        relaxed = variant(lambda d: d["rules"]["LOW_CONFIDENCE"]["actions"].update(low="OBSERVE", medium="OBSERVE"))
        decision = route([signal("LOW_CONFIDENCE", "low", confidence="low")], policy=relaxed)
        self.assertEqual(decision.decision_class, "AUTO_PASS")
        self.assertEqual([item["action"] for item in decision.secondary_signals], ["OBSERVE"], "o sinal continua observável")
        strict = variant(lambda d: d["rules"]["LOW_CONFIDENCE"]["actions"].update(low="AI_RECHECK", medium="AI_RECHECK"))
        again = route([signal("LOW_CONFIDENCE", "low", confidence="low", evidence=(doc_binding(),))], policy=strict)
        self.assertEqual(again.decision_class, "AI_RECHECK")

    def test_critical_ambiguity_with_existing_evidence_goes_to_ai_recheck(self) -> None:
        decision = route([recheck_signal("critical")])
        self.assertEqual(decision.decision_class, "AI_RECHECK")
        self.assertEqual(decision.next_action, "RUN_AI_RECHECK")
        plan = decision.recheck_plan
        self.assertEqual(plan["round"], 1)
        self.assertEqual([item["claim_ref"] for item in plan["claims"]], ["c1"])
        self.assertEqual([item["ref"] for item in plan["evidence"]], [DOC_REF])
        self.assertEqual((plan["max_boundary"], plan["risk_level"]), ("LOCAL", "HIGH"))
        self.assertFalse(decision.requires_named_human)
        self.assertIsNone(decision.quality_sampling)

    def test_missing_critical_evidence_is_a_blocking_source_gap(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "critical")])
        self.assertEqual(decision.decision_class, "SOURCE_GAP")
        self.assertEqual(decision.next_action, "ACQUIRE_SOURCE")
        (requirement,) = decision.source_gap_requirements
        self.assertTrue(requirement.blocking)
        self.assertFalse(decision.requires_named_human)
        self.assertEqual(decision.human_decision_candidates, ())

    def test_a_direct_unresolved_conflict_needs_a_named_person(self) -> None:
        decision = route([signal(
            "CONFLICTING_EVIDENCE", "high", qualifier="direct_conflict", evidence=(doc_binding(),),
            positions=(Position("c1", "retenção de 30 dias"), Position("c2", "retenção de 90 dias")),
        )])
        self.assertEqual(decision.decision_class, "HUMAN_DECISION")
        self.assertTrue(decision.requires_named_human)
        self.assertEqual(decision.next_action, "REQUEST_HUMAN_DECISION")
        (candidate,) = decision.human_decision_candidates
        self.assertEqual(candidate.options, ("retenção de 30 dias", "retenção de 90 dias"))
        self.assertEqual(len(candidate.conflicting_claims), 2)

    def test_hard_policy_conflict_needs_a_person_even_at_low_criticality(self) -> None:
        decision = route([signal("POLICY_CONFLICT", "low")])
        self.assertEqual((decision.decision_class, decision.human_decision_candidates[0].decision_kind), ("HUMAN_DECISION", "apply_policy"))


class CriticalityAndConfidenceAreSeparateAxes(unittest.TestCase):
    def test_confidence_never_changes_the_route(self) -> None:
        for family, criticality, evidence in (
            ("LOW_CONFIDENCE", "low", True), ("LOW_CONFIDENCE", "critical", True), ("MISSING_EVIDENCE", "high", False), ("POLICY_CONFLICT", "low", False),
        ):
            classes = set()
            for confidence in CONFIDENCE_BANDS:
                item = signal(family, criticality, confidence=confidence, evidence=(doc_binding(),) if evidence else ())
                classes.add(route([item]).decision_class)
            with self.subTest(family=family, criticality=criticality):
                self.assertEqual(len(classes), 1, "a confiança mudou a rota")

    def test_low_confidence_alone_is_never_human_review(self) -> None:
        for criticality in CRITICALITIES:
            for evidence in ((doc_binding(),), ()):
                decision = route([signal("LOW_CONFIDENCE", criticality, confidence="low", evidence=evidence)])
                with self.subTest(criticality=criticality, evidence=bool(evidence)):
                    self.assertNotEqual(decision.decision_class, "HUMAN_DECISION")
                    self.assertFalse(decision.requires_named_human)

    def test_critical_low_confidence_with_evidence_goes_to_a_second_reading_not_a_person(self) -> None:
        self.assertEqual(route([signal("LOW_CONFIDENCE", "critical", confidence="low", evidence=(doc_binding(),))]).decision_class, "AI_RECHECK")

    def test_high_confidence_is_not_safety(self) -> None:
        conflict = signal("CONFLICTING_EVIDENCE", "critical", qualifier="direct_conflict", confidence="high", evidence=(doc_binding(),))
        self.assertEqual(route([conflict]).decision_class, "HUMAN_DECISION")
        self.assertEqual(route([signal("MISSING_EVIDENCE", "critical", confidence="high")]).decision_class, "SOURCE_GAP")
        self.assertEqual(route([signal("POLICY_CONFLICT", "low", confidence="high")]).decision_class, "HUMAN_DECISION")

    def test_criticality_alone_moves_the_route_up(self) -> None:
        seen = [route([signal("LOW_CONFIDENCE", criticality, evidence=(doc_binding(),))]).decision_class for criticality in CRITICALITIES]
        self.assertEqual(seen, ["SAMPLE_QA", "SAMPLE_QA", "AI_RECHECK", "AI_RECHECK"])

    def test_the_assessment_records_declared_and_effective_criticality_and_the_confidence_is_kept_on_the_signal(self) -> None:
        decision = route([signal("LOW_CONFIDENCE", "medium", confidence="low", evidence=(doc_binding(),))])
        assessment = decision.assessments[0]
        self.assertEqual((assessment["declared_criticality"], assessment["effective_criticality"]), ("medium", "medium"))
        self.assertEqual(decision.signals[0].confidence, "low")
        self.assertNotIn("confidence", assessment)


class EvidenceAvailabilityIsTheCentralInvariant(unittest.TestCase):
    def test_a_evidence_exists_but_the_reading_is_uncertain_is_a_recheck_candidate(self) -> None:
        for family in ("LOW_CONFIDENCE", "AMBIGUOUS_INTERPRETATION", "SELF_AUDIT_REVIEW"):
            with self.subTest(family):
                self.assertEqual(route([signal(family, "high", evidence=(doc_binding(),))]).decision_class, "AI_RECHECK")

    def test_b_required_evidence_does_not_exist_is_a_source_gap_never_a_recheck(self) -> None:
        for family in ("LOW_CONFIDENCE", "AMBIGUOUS_INTERPRETATION", "SELF_AUDIT_REVIEW"):
            decision = route([signal(family, "high")])
            with self.subTest(family):
                self.assertEqual(decision.decision_class, "SOURCE_GAP")
                self.assertEqual(decision.assessments[0]["why"], "no-evidence-to-reread")
                self.assertEqual(decision.assessments[0]["rule_action"], "AI_RECHECK", "a regra pedia releitura; o roteador explica o ajuste")
                self.assertIsNone(decision.recheck_plan)

    def test_truncated_or_missing_evidence_is_never_confused_with_model_uncertainty(self) -> None:
        for family in ("MISSING_EVIDENCE", "TRUNCATED_EVIDENCE"):
            decision = route([signal(family, "high", evidence=(doc_binding(),))])
            with self.subTest(family):
                self.assertEqual(decision.decision_class, "SOURCE_GAP")
                self.assertIsNone(decision.recheck_plan, "existe evidência parcial, mas o que falta não se relê")

    def test_c_evidence_exists_and_conflicts_in_a_way_the_ai_cannot_decide_needs_a_person(self) -> None:
        decision = route([signal("CONFLICTING_EVIDENCE", "high", qualifier="human_vs_observed", evidence=(doc_binding(),))])
        self.assertEqual(decision.decision_class, "HUMAN_DECISION")
        self.assertEqual(decision.human_decision_candidates[0].evidence_available, (doc_binding(),))

    def test_a_source_gap_never_becomes_a_human_decision_by_itself(self) -> None:
        for family in ("MISSING_EVIDENCE", "TRUNCATED_EVIDENCE", "UNKNOWN_RELATION", "AMBIGUOUS_INTERPRETATION"):
            for criticality in CRITICALITIES:
                for evidence in ((), (doc_binding(),)):
                    decision = route([signal(family, criticality, evidence=evidence, impact_class="fiscal")])
                    with self.subTest(family=family, criticality=criticality, evidence=bool(evidence)):
                        self.assertFalse(decision.requires_named_human)
                        self.assertEqual(decision.human_decision_candidates, ())
                        self.assertNotEqual(decision.decision_class, "HUMAN_DECISION")

    def test_even_for_high_impact_missing_evidence_asks_for_a_source_not_a_person(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "low", impact_class="financial")])
        self.assertEqual(decision.decision_class, "SOURCE_GAP")
        self.assertEqual(decision.assessments[0]["effective_criticality"], "high")
        self.assertTrue(decision.source_gap_requirements[0].blocking)


class HighImpactClaimsInfluenceRoutingByPolicy(unittest.TestCase):
    def test_declared_effects_raise_the_effective_criticality_without_hand_marking(self) -> None:
        plain = route([signal("LOW_CONFIDENCE", "low", evidence=(doc_binding(),))])
        risky = route([signal("LOW_CONFIDENCE", "low", evidence=(doc_binding(),), effects=("deletes-data",))])
        self.assertEqual(plain.decision_class, "SAMPLE_QA")
        self.assertEqual(risky.decision_class, "AI_RECHECK")
        assessment = risky.assessments[0]
        self.assertEqual((assessment["impact_class"], assessment["declared_criticality"], assessment["effective_criticality"]), ("irreversible_action", "low", "critical"))

    def test_an_unknown_effect_is_not_treated_as_harmless(self) -> None:
        plain = route([signal("AMBIGUOUS_INTERPRETATION", "low")])
        unknown = route([signal("AMBIGUOUS_INTERPRETATION", "low", effects=("does-something-new",))])
        self.assertEqual((plain.decision_class, unknown.decision_class), ("AUTO_PASS", "SAMPLE_QA"))
        self.assertEqual(unknown.assessments[0]["effective_criticality"], "medium")

    def test_an_explicit_impact_class_counts_and_an_unknown_one_is_refused(self) -> None:
        self.assertEqual(route([signal("AMBIGUOUS_INTERPRETATION", "low", impact_class="security", evidence=(doc_binding(),))]).decision_class, "AI_RECHECK")
        self.assertEqual(code_of(lambda: route([signal("AMBIGUOUS_INTERPRETATION", "low", impact_class="cosmic")])), "UNKNOWN_IMPACT")

    def test_an_unverified_high_impact_claim_derived_from_effects_needs_a_person(self) -> None:
        claim = ClaimAssessment("claim-wipe", "O job apaga os registros vencidos.", "INFERRED", effects=("deletes-data",), recommended_role="data-owner")
        decision = route([], claims=[claim])
        self.assertEqual(decision.decision_class, "HUMAN_DECISION")
        (candidate,) = decision.human_decision_candidates
        self.assertEqual((candidate.decision_kind, candidate.target_ref, candidate.recommended_role), ("validate_claim", "claim-wipe", "data-owner"))
        self.assertEqual((candidate.impact_class, candidate.criticality), ("irreversible_action", "critical"))
        self.assertEqual([item.family for item in decision.signals], ["UNVERIFIED_HIGH_IMPACT_CLAIM"], "o sinal foi DERIVADO, ninguém o marcou")

    def test_only_unverified_high_impact_claims_derive_a_signal(self) -> None:
        wipe = ("deletes-data",)
        for status, effects, expected in (
            ("INFERRED", wipe, "HUMAN_DECISION"), ("VALIDATED", wipe, "AUTO_PASS"), ("OBSERVED", wipe, "AUTO_PASS"),
            ("INFERRED", ("reads-data",), "AUTO_PASS"), ("INFERRED", (), "AUTO_PASS"),
            ("INFERRED", ("transmits-externally",), "HUMAN_DECISION"), ("INFERRED", ("computes-regulatory-value",), "HUMAN_DECISION"),
            ("INFERRED", ("alters-access-control",), "HUMAN_DECISION"), ("INFERRED", ("computes-monetary-value",), "HUMAN_DECISION"),
            ("INFERRED", ("changes-state",), "AUTO_PASS"),
        ):
            decision = route([], claims=[ClaimAssessment("claim-1", "afirmação", status, effects=effects)])
            with self.subTest(status=status, effects=effects):
                self.assertEqual(decision.decision_class, expected)

    def test_the_route_of_a_high_impact_claim_is_whatever_the_policy_configures(self) -> None:
        claim = ClaimAssessment("claim-1", "afirmação", "INFERRED", effects=("deletes-data",))
        not_high = variant(lambda d: d["impact"].update(high_impact=["data_integrity", "financial", "fiscal", "security", "external_transmission"]))
        self.assertEqual(route([], claims=[claim], policy=not_high).decision_class, "AUTO_PASS")
        as_gap = variant(lambda d: d["rules"]["UNVERIFIED_HIGH_IMPACT_CLAIM"].update(actions={"low": "SAMPLE_QA", "medium": "SAMPLE_QA", "high": "SOURCE_GAP", "critical": "SOURCE_GAP"}))
        decision = route([], claims=[claim], policy=as_gap)
        self.assertEqual(decision.decision_class, "SOURCE_GAP")
        wider = variant(lambda d: d["impact"].update(unverified_statuses=["OBSERVED", "INFERRED"]))
        observed = ClaimAssessment("claim-1", "afirmação", "OBSERVED", effects=("deletes-data",))
        self.assertEqual(route([], claims=[observed], policy=wider).decision_class, "HUMAN_DECISION")

    def test_a_derived_signal_equal_to_a_given_one_is_not_duplicated(self) -> None:
        claim = ClaimAssessment("claim-1", "afirmação", "INFERRED", effects=("deletes-data",))
        explicit = claim.as_signal(SUBJECT_ONE.ref, SUBJECT_ONE.kind)
        decision = route([explicit], claims=[claim])
        self.assertEqual(len(decision.signals), 1)

    def test_the_adapter_never_names_a_domain_concept_the_policy_maps_generic_effects(self) -> None:
        policy = default_policy()
        for effect in policy.data["impact"]["effects"]:
            self.assertRegex(effect, r"^[a-z]+(-[a-z]+)*$")
        self.assertEqual(policy.derive_impact(None, ("computes-regulatory-value",)), "fiscal")


class PrivacySecurityAndProviderRestrictions(unittest.TestCase):
    def test_a_concern_specific_to_the_subject_needs_a_person_and_lists_what_not_to_assume(self) -> None:
        decision = route([signal("PRIVACY_OR_SECURITY_CONCERN", "low", qualifier="subject_specific", boundary_limit="LOCAL")])
        self.assertEqual(decision.decision_class, "HUMAN_DECISION")
        (candidate,) = decision.human_decision_candidates
        self.assertEqual(candidate.decision_kind, "assess_concern")
        self.assertEqual(candidate.prohibited_assumptions, default_policy().prohibited_assumptions("assess_concern"))
        self.assertTrue(any("fronteira de dados" in item for item in candidate.prohibited_assumptions))

    def test_a_systemic_concern_already_handled_is_observed_not_a_trigger(self) -> None:
        decision = route([signal("PRIVACY_OR_SECURITY_CONCERN", "critical", qualifier="systemic_handled", boundary_limit="LOCAL")])
        self.assertEqual(decision.decision_class, "AUTO_PASS")
        self.assertEqual(decision.secondary_signals[0]["family"], "PRIVACY_OR_SECURITY_CONCERN")
        self.assertEqual(decision.constraints, {"max_boundary": "LOCAL"}, "mas a restrição de fronteira continua valendo")

    def test_an_unqualified_concern_is_treated_as_specific_from_medium_up(self) -> None:
        self.assertEqual(route([signal("PRIVACY_OR_SECURITY_CONCERN", "medium")]).decision_class, "HUMAN_DECISION")

    def test_a_boundary_limit_only_tightens_the_recheck(self) -> None:
        relaxed = variant(lambda d: d["recheck"].update(default_max_boundary="PRIVATE"))
        cases = (
            (relaxed, ("LOCAL",), "LOCAL"), (relaxed, (), "PRIVATE"), (relaxed, ("EXTERNAL",), "PRIVATE"),
            (relaxed, ("PRIVATE", "LOCAL"), "LOCAL"), (default_policy(), ("EXTERNAL",), "LOCAL"), (default_policy(), (), "LOCAL"),
        )
        for policy, limits, expected in cases:
            signals = [recheck_signal("critical")] + [
                signal("PRIVACY_OR_SECURITY_CONCERN", "low", qualifier="systemic_handled", boundary_limit=limit, target_ref=f"p{index}")
                for index, limit in enumerate(limits)
            ]
            decision = route(signals, policy=policy)
            with self.subTest(default=policy.default_max_boundary, limits=limits):
                self.assertEqual(decision.decision_class, "AI_RECHECK")
                self.assertEqual(decision.recheck_plan["max_boundary"], expected)

    def test_the_strictest_limit_is_recorded_as_a_constraint_whatever_the_class(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "high", boundary_limit="PRIVATE"), signal("LOW_CONFIDENCE", "low", boundary_limit="LOCAL", target_ref="t2")])
        self.assertEqual((decision.decision_class, decision.constraints), ("SOURCE_GAP", {"max_boundary": "LOCAL"}))

    def test_no_constraint_when_no_signal_limits_the_data(self) -> None:
        self.assertEqual(route([signal("MISSING_EVIDENCE", "high")]).constraints, {})


class PrecedenceOrderAndSecondarySignals(unittest.TestCase):
    ORDER = ("HUMAN_DECISION", "SOURCE_GAP_BLOCKING", "AI_RECHECK", "SOURCE_GAP", "SAMPLE_QA")
    RESULT_CLASS = {"HUMAN_DECISION": "HUMAN_DECISION", "SOURCE_GAP_BLOCKING": "SOURCE_GAP", "AI_RECHECK": "AI_RECHECK", "SOURCE_GAP": "SOURCE_GAP", "SAMPLE_QA": "SAMPLE_QA"}

    @staticmethod
    def maker(name: str) -> AnalysisSignal:
        return {
            "HUMAN_DECISION": lambda: signal("POLICY_CONFLICT", "medium", target_ref="t-hd"),
            "SOURCE_GAP_BLOCKING": lambda: signal("MISSING_EVIDENCE", "high", target_ref="t-sgb"),
            "AI_RECHECK": lambda: signal("AMBIGUOUS_INTERPRETATION", "high", target_ref="t-air", evidence=(doc_binding(),)),
            "SOURCE_GAP": lambda: signal("MISSING_EVIDENCE", "medium", target_ref="t-sgn"),
            "SAMPLE_QA": lambda: signal("LOW_CONFIDENCE", "low", target_ref="t-sq"),
        }[name]()

    def subsets(self):  # noqa: ANN201
        for size in range(1, len(self.ORDER) + 1):
            yield from itertools.combinations(self.ORDER, size)

    def test_the_highest_precedence_present_always_wins(self) -> None:
        count = 0
        for names in self.subsets():
            count += 1
            decision = route([self.maker(name) for name in names])
            winner = next(name for name in self.ORDER if name in names)
            with self.subTest(names=names):
                self.assertEqual(decision.decision_class, self.RESULT_CLASS[winner])
                primary = [item["signal_id"] for item in decision.assessments if item["role"] == "primary"]
                self.assertEqual(primary, [self.maker(winner).signal_id])
                self.assertEqual(len(decision.primary_reasons) + len(decision.secondary_signals), len(names))
        self.assertEqual(count, 31)

    def test_the_order_of_the_input_never_matters(self) -> None:
        signals = [self.maker(name) for name in self.ORDER]
        base = route(signals).to_dict()
        for permutation in itertools.islice(itertools.permutations(signals), 0, 120, 7):
            self.assertEqual(route(list(permutation)).to_dict(), base)

    def test_a_blocking_gap_outranks_a_recheck_but_a_non_blocking_one_does_not(self) -> None:
        self.assertEqual(route([self.maker("SOURCE_GAP_BLOCKING"), self.maker("AI_RECHECK")]).decision_class, "SOURCE_GAP")
        self.assertEqual(route([self.maker("SOURCE_GAP"), self.maker("AI_RECHECK")]).decision_class, "AI_RECHECK")

    def test_the_precedence_is_not_configurable_by_policy(self) -> None:
        for key in ("precedence", "order", "ranking"):
            data = default_policy().to_dict()
            data[key] = ["SAMPLE_QA", "HUMAN_DECISION"]
            with self.assertRaises(ReviewContractError):
                variant(lambda d, data=data: d.update(data))

    def test_every_signal_is_preserved_and_the_losers_stay_observable(self) -> None:
        signals = [self.maker(name) for name in self.ORDER]
        decision = route(signals)
        self.assertEqual(decision.decision_class, "HUMAN_DECISION")
        self.assertEqual(len(decision.signals), 5)
        self.assertEqual([item["family"] for item in decision.primary_reasons], ["POLICY_CONFLICT"])
        self.assertEqual(sorted(item["action"] for item in decision.secondary_signals), ["AI_RECHECK", "SAMPLE_QA", "SOURCE_GAP", "SOURCE_GAP"])
        self.assertIsNone(decision.recheck_plan, "a releitura fica para depois: a pessoa já será convocada")
        gaps = decision.source_gap_requirements
        self.assertEqual(sorted(item.blocking for item in gaps), [False, True], "os pedidos de fonte continuam saindo")

    def test_auto_pass_keeps_an_optional_unknown_relation_as_a_secondary_signal(self) -> None:
        decision = route([signal("UNKNOWN_RELATION", "low", target_ref="rel-1"), signal("STRUCTURE_ONLY", "high", target_ref="rel-2")])
        self.assertEqual(decision.decision_class, "AUTO_PASS")
        self.assertEqual(decision.primary_reasons, ())
        self.assertEqual({item["family"] for item in decision.secondary_signals}, {"UNKNOWN_RELATION", "STRUCTURE_ONLY"})
        self.assertEqual({item["action"] for item in decision.secondary_signals}, {"OBSERVE"})

    def test_a_source_gap_keeps_a_low_confidence_signal_as_secondary(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "high"), signal("LOW_CONFIDENCE", "low", confidence="low", evidence=(doc_binding(),), target_ref="t2")])
        self.assertEqual(decision.decision_class, "SOURCE_GAP")
        self.assertEqual([(item["family"], item["action"]) for item in decision.secondary_signals], [("LOW_CONFIDENCE", "SAMPLE_QA")])

    def test_a_signal_of_another_subject_is_refused(self) -> None:
        self.assertEqual(code_of(lambda: route([signal("LOW_CONFIDENCE", subject=SUBJECT_TWO)])), "SUBJECT_MISMATCH")

    def test_the_same_signal_twice_counts_once(self) -> None:
        item = signal("MISSING_EVIDENCE", "high")
        self.assertEqual(len(route([item, item]).signals), 1)


class DecisionIdentityIsDeterministicAndPolicyAware(TempDirCase):
    def decision(self, **kwargs: object) -> ReviewDecision:
        return route([recheck_signal("critical"), signal("LOW_CONFIDENCE", "low", target_ref="t2")], **kwargs)

    def test_same_inputs_same_policy_same_bytes(self) -> None:
        one, two = self.decision(), self.decision()
        self.assertEqual(one.decision_id, two.decision_id)
        self.assertEqual(canonical_pretty_bytes(one.to_dict()), canonical_pretty_bytes(two.to_dict()))
        self.assertRegex(one.decision_id, r"^art:review\.decision:sha256:[0-9a-f]{64}$")

    def test_a_fresh_interpreter_with_other_hash_seeds_gets_the_same_id(self) -> None:
        code = (
            "from tests.review_helpers import route, signal, recheck_signal\n"
            "print(route([recheck_signal('critical'), signal('LOW_CONFIDENCE', 'low', target_ref='t2')]).decision_id)\n"
        )
        seen = set()
        for seed in ("1", "2", "random"):
            env = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": seed}
            result = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.tmp, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            seen.add(result.stdout.decode().strip())
        self.assertEqual(seen, {self.decision().decision_id})

    def test_a_new_policy_version_changes_the_identity(self) -> None:
        newer = variant(lambda d: d.update(version="1.0.1"))
        old, new = self.decision(), self.decision(policy=newer)
        self.assertNotEqual(old.decision_id, new.decision_id)
        self.assertEqual((old.policy["version"], new.policy["version"]), ("1.0.0", "1.0.1"))
        self.assertEqual(old.decision_class, new.decision_class, "só a política mudou, a classe não")

    def test_changing_the_policy_content_without_a_version_still_changes_the_identity(self) -> None:
        edited = variant(lambda d: d["sampling"].update(seed="another-seed"))
        old, new = self.decision(), self.decision(policy=edited)
        self.assertEqual(old.policy["version"], new.policy["version"])
        self.assertNotEqual(old.policy["digest"], new.policy["digest"])
        self.assertNotEqual(old.decision_id, new.decision_id)

    def test_a_decision_made_with_an_old_policy_is_not_current_and_does_not_reproduce(self) -> None:
        old = self.decision()
        newer = ReviewRouter(variant(lambda d: d.update(version="2.0.0")))
        self.assertTrue(ReviewRouter().is_current(old))
        self.assertTrue(ReviewRouter().reproduces(old))
        self.assertFalse(newer.is_current(old))
        self.assertFalse(newer.reproduces(old))

    def test_the_subject_the_signals_and_the_history_are_all_part_of_the_identity(self) -> None:
        base = self.decision()
        other_software = ReviewSubject(SUBJECT_ONE.ref, SUBJECT_ONE.kind, SUBJECT_ONE.title, "synthetic-three")
        titled = ReviewSubject(SUBJECT_ONE.ref, SUBJECT_ONE.kind, "Outro título", SUBJECT_ONE.software_id)
        extra = route([recheck_signal("critical"), signal("LOW_CONFIDENCE", "low", target_ref="t2"), signal("STRUCTURE_ONLY", "low", target_ref="t3")])
        record = RecheckRecord(("c1",), (doc_binding(),), 1, "RESOLVED", "art:ai-recheck.request:sha256:" + "a" * 64, "art:ai-recheck.result:sha256:" + "b" * 64, {})
        with_history = self.decision(history=[record])
        ids = {
            base.decision_id,
            self.decision(subject=other_software).decision_id,
            self.decision(subject=titled).decision_id,
            extra.decision_id,
            with_history.decision_id,
        }
        self.assertEqual(len(ids), 5)

    def test_a_policy_change_makes_the_stage_engine_redo_the_routing_stage(self) -> None:
        signals = json.dumps([recheck_signal("critical").to_dict()])

        def build(policy):  # noqa: ANN001, ANN202
            def handler(ctx, default) -> None:  # noqa: ANN001
                if ctx.parameters != policy.stage_parameters():
                    raise StageFailure("os parâmetros do estágio não declaram esta política", retryable=False)
                items = [AnalysisSignal.from_dict(item) for item in json.loads(ctx.read_input("signals"))]
                ctx.write_output_json("decision", ReviewRouter(policy).route(SUBJECT_ONE, items).to_dict())

            return Scenario(self.tmp, [spec("route", inputs=["signals"], outputs=["decision"])], {"signals": signals}, params={"route": policy.stage_parameters()}, handlers={"route": handler})

        first = build(default_policy())
        result = first.run()
        self.assertEqual(first.states(result), {"route": "COMPLETED"})
        stored = json.loads(first.output_file("route", "decision").read_text(encoding="utf-8"))
        self.assertEqual(stored["policy"], default_policy().ref())
        self.assertEqual(first.run().plan.skipped, ("route",), "mesma política: nada é refeito")
        self.assertEqual(first.calls, [])

        newer = variant(lambda d: d.update(version="1.1.0"))
        second = build(newer)
        again = second.run()
        self.assertEqual(second.calls, ["route"], "outra política: o estágio é refeito")
        self.assertEqual(again.plan.of("route").decision, Decision.RUN)
        redone = json.loads(second.output_file("route", "decision").read_text(encoding="utf-8"))
        self.assertEqual(redone["policy"], newer.ref())
        self.assertNotEqual(redone["decision_id"], stored["decision_id"])


class QualitySamplingIsDeterministic(unittest.TestCase):
    def test_the_draw_depends_only_on_the_seed_and_the_decision_identity(self) -> None:
        identity = "art:review.decision:sha256:" + "1" * 64
        self.assertEqual(rr.sample_bucket("seed", identity), rr.sample_bucket("seed", identity))
        self.assertNotEqual(rr.sample_bucket("seed", identity), rr.sample_bucket("other", identity))
        self.assertTrue(0 <= rr.sample_bucket("seed", identity) < 1)

    def test_a_decision_draws_the_same_way_every_time(self) -> None:
        for _ in range(3):
            decision = route([signal("LOW_CONFIDENCE", "low")])
            self.assertEqual(decision.quality_sampling, route([signal("LOW_CONFIDENCE", "low")]).quality_sampling)

    def test_the_selection_rate_follows_the_configured_rate(self) -> None:
        for rate in (0.05, 0.2, 0.5):
            hits = sum(rr.sample_bucket("review-sampling/1", f"art:review.decision:sha256:{hashlib.sha256(str(i).encode()).hexdigest()}") < rate for i in range(5000))
            with self.subTest(rate=rate):
                self.assertAlmostEqual(hits / 5000, rate, delta=0.03)

    def test_rate_zero_never_selects_and_rate_one_always_does(self) -> None:
        never = variant(lambda d: d["sampling"]["rates"].update(SAMPLE_QA=0, AUTO_PASS=0))
        always = variant(lambda d: d["sampling"]["rates"].update(SAMPLE_QA=1, AUTO_PASS=1))
        for index in range(12):
            item = [signal("LOW_CONFIDENCE", "low", summary=f"variação {index}")]
            self.assertFalse(route(item, policy=never).quality_sampling["selected"])
            self.assertTrue(route(item, policy=always).quality_sampling["selected"])
            self.assertFalse(route([], policy=never).quality_sampling["selected"])
        self.assertTrue(route([], policy=always).quality_sampling["selected"])

    def test_being_sampled_never_changes_the_class(self) -> None:
        always = variant(lambda d: d["sampling"]["rates"].update(SAMPLE_QA=1, AUTO_PASS=1))
        qa = route([signal("LOW_CONFIDENCE", "low")], policy=always)
        audited = route([], policy=always)
        self.assertEqual((qa.decision_class, qa.next_action), ("SAMPLE_QA", "SAMPLE_REVIEW"))
        self.assertEqual((audited.decision_class, audited.next_action), ("AUTO_PASS", "SAMPLE_REVIEW"))
        self.assertEqual((audited.quality_sampling["purpose"], qa.quality_sampling["purpose"]), ("audit", "qa"))

    def test_an_unsampled_decision_just_proceeds(self) -> None:
        never = variant(lambda d: d["sampling"]["rates"].update(SAMPLE_QA=0, AUTO_PASS=0))
        self.assertEqual(route([signal("LOW_CONFIDENCE", "low")], policy=never).next_action, "PROCEED")

    def test_only_the_sampled_classes_carry_a_sample(self) -> None:
        self.assertIsNone(route([recheck_signal("critical")]).quality_sampling)
        self.assertIsNone(route([signal("MISSING_EVIDENCE", "high")]).quality_sampling)
        self.assertIsNone(route([signal("POLICY_CONFLICT", "low")]).quality_sampling)

    def test_changing_the_seed_redraws_the_sample(self) -> None:
        ids = [f"art:review.decision:sha256:{hashlib.sha256(str(i).encode()).hexdigest()}" for i in range(200)]
        one = [rr.sample_bucket("seed-a", item) < 0.5 for item in ids]
        two = [rr.sample_bucket("seed-b", item) < 0.5 for item in ids]
        self.assertNotEqual(one, two)

    def test_the_router_uses_neither_the_clock_nor_randomness(self) -> None:
        text = (REPO / "core" / "review_routing.py").read_text(encoding="utf-8")
        for forbidden in ("random", "time.", "datetime", "uuid", "secrets"):
            self.assertNotIn(forbidden, text)


class AutoPassIsAQueueDecisionNotKnowledge(unittest.TestCase):
    def test_no_class_promotes_knowledge(self) -> None:
        for signals in ([], [signal("LOW_CONFIDENCE", "low")], [recheck_signal()], [signal("MISSING_EVIDENCE", "high")], [signal("POLICY_CONFLICT")]):
            decision = route(signals)
            with self.subTest(decision.decision_class):
                self.assertEqual(decision.data["validation_effect"], "NONE")

    def test_the_decision_has_no_field_that_could_hold_a_validation(self) -> None:
        for decision in (route([]), route([recheck_signal()]), route([signal("POLICY_CONFLICT")])):
            keys = walk_keys(decision.to_dict())
            self.assertEqual(keys & {"validated_by", "validated_at", "validation", "status", "reviewed_by", "VALIDATED"}, set())

    def test_the_word_validated_is_not_in_any_auto_pass_decision(self) -> None:
        self.assertNotIn("VALIDATED", json.dumps(route([]).to_dict()))

    def test_an_edited_validation_effect_is_refused_by_the_schema_and_the_loader(self) -> None:
        data = route([]).to_dict()
        data["validation_effect"] = "VALIDATED"
        self.assertTrue(VALIDATOR.validate(data, "review-decision"))
        self.assertEqual(code_of(lambda: ReviewDecision.from_dict(data)), "SCHEMA")

    def test_auto_pass_with_a_sample_is_still_not_validation(self) -> None:
        always = variant(lambda d: d["sampling"]["rates"].update(AUTO_PASS=1))
        decision = route([], policy=always)
        self.assertEqual((decision.decision_class, decision.data["validation_effect"], decision.requires_named_human), ("AUTO_PASS", "NONE", False))


class ArtifactsForTheDestinations(unittest.TestCase):
    NEED = SourceNeed("o prazo efetivo configurado em produção", "decide qual dos prazos vale", ("runtime", "database"))

    def test_a_source_gap_requirement_says_what_is_missing_why_where_and_how_much_it_blocks(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "high", impact_class="financial", source_need=self.NEED, evidence=(doc_binding(),))])
        (requirement,) = decision.source_gap_requirements
        self.assertEqual(requirement.missing_information, self.NEED.missing_information)
        self.assertEqual(requirement.why_needed, self.NEED.why_needed)
        self.assertEqual(requirement.recommended_source_types, ("database", "runtime"))
        self.assertEqual((requirement.impact_class, requirement.criticality, requirement.blocking, requirement.origin), ("financial", "high", True, "signal"))
        self.assertEqual(requirement.evidence_consulted, (doc_binding(),))
        self.assertEqual(requirement.signal_ids, (decision.signals[0].signal_id,))
        self.assertEqual(VALIDATOR.validate(requirement.to_dict(), "source-gap-requirement"), [])
        self.assertEqual(SourceGapRequirement.from_dict(requirement.to_dict()), requirement)

    def test_the_source_types_are_generic_and_default_from_the_policy(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "medium")])
        (requirement,) = decision.source_gap_requirements
        self.assertEqual(requirement.recommended_source_types, ("human-expert",))
        self.assertFalse(requirement.blocking, "criticidade média não bloqueia")
        custom = variant(lambda d: d["source_gap"].update(default_source_types=["documentation", "external-system"]))
        self.assertEqual(route([signal("MISSING_EVIDENCE", "medium")], policy=custom).source_gap_requirements[0].recommended_source_types, ("documentation", "external-system"))
        self.assertTrue(set(requirement.recommended_source_types) <= set(SOURCE_TYPES))

    def test_a_requirement_keeps_its_identity_across_reevaluations(self) -> None:
        gap = signal("MISSING_EVIDENCE", "high", source_need=self.NEED)
        alone = route([gap]).source_gap_requirements[0]
        with_noise = route([gap, signal("STRUCTURE_ONLY", "low", target_ref="x")]).source_gap_requirements[0]
        self.assertEqual(alone.requirement_id, with_noise.requirement_id, "o backlog deduplica o mesmo pedido")

    def test_a_tampered_requirement_is_refused(self) -> None:
        data = route([signal("MISSING_EVIDENCE", "high")]).source_gap_requirements[0].to_dict()
        data["missing_information"] = "outra coisa"
        self.assertEqual(code_of(lambda: SourceGapRequirement.from_dict(data)), "ID_MISMATCH")

    def test_a_human_decision_candidate_is_the_boundary_artifact(self) -> None:
        decision = route([signal(
            "CONFLICTING_EVIDENCE", "high", qualifier="human_vs_observed", evidence=(doc_binding(),), recommended_role="domain-owner",
            positions=(Position("claim-h", "a pessoa afirma 90 dias", "human"), Position("claim-o", "o documento mostra 30 dias", "observed")),
        )])
        (candidate,) = decision.human_decision_candidates
        self.assertEqual(candidate.decision_kind, "resolve_conflict")
        self.assertIn("Qual posição vale", candidate.question)
        self.assertIn("contradizem", candidate.why_ai_cannot_resolve)
        self.assertEqual(candidate.evidence_available, (doc_binding(),))
        self.assertEqual({item.origin for item in candidate.conflicting_claims}, {"human", "observed"})
        self.assertEqual(candidate.options, ("a pessoa afirma 90 dias", "o documento mostra 30 dias"))
        self.assertEqual(candidate.recommended_role, "domain-owner")
        self.assertEqual(candidate.prohibited_assumptions, default_policy().prohibited_assumptions("resolve_conflict"))
        data = candidate.to_dict()
        self.assertEqual(VALIDATOR.validate(data, "human-decision-candidate"), [])
        self.assertEqual(HumanDecisionCandidate.from_dict(data), candidate)
        self.assertEqual(walk_keys(data) & {"answer", "answered_by", "validated_by", "status", "state"}, set(), "é só a fronteira: não carrega resposta")

    def test_the_role_is_absent_when_unknown(self) -> None:
        candidate = route([signal("POLICY_CONFLICT")]).human_decision_candidates[0]
        self.assertIsNone(candidate.recommended_role)
        self.assertNotIn("recommended_role", candidate.to_dict())

    def test_candidates_are_grouped_by_target_and_kind(self) -> None:
        same = route([
            signal("CONFLICTING_EVIDENCE", "high", qualifier="direct_conflict", target_ref="k1"),
            signal("CONFLICTING_EVIDENCE", "medium", qualifier="human_vs_observed", target_ref="k1"),
        ])
        self.assertEqual(len(same.human_decision_candidates), 1)
        self.assertEqual(len(same.human_decision_candidates[0].signal_ids), 2)
        different = route([signal("POLICY_CONFLICT", "low", target_ref="k1"), signal("POLICY_CONFLICT", "low", target_ref="k2")])
        self.assertEqual(len(different.human_decision_candidates), 2)
        ids = [item.candidate_id for item in different.human_decision_candidates]
        self.assertEqual(ids, sorted(ids))

    def test_a_candidate_keeps_its_identity_across_reevaluations(self) -> None:
        conflict = signal("POLICY_CONFLICT", "low")
        alone = route([conflict]).human_decision_candidates[0]
        noisy = route([conflict, signal("STRUCTURE_ONLY", "low", target_ref="x")]).human_decision_candidates[0]
        self.assertEqual(alone.candidate_id, noisy.candidate_id)

    def test_every_decision_validates_against_the_schema(self) -> None:
        for signals in ([], [signal("LOW_CONFIDENCE", "low")], [recheck_signal()], [signal("MISSING_EVIDENCE", "high")], [signal("POLICY_CONFLICT")]):
            decision = route(signals)
            with self.subTest(decision.decision_class):
                self.assertEqual(VALIDATOR.validate(decision.to_dict(), "review-decision"), [])
                self.assertEqual(ReviewDecision.from_dict(decision.to_dict()).to_dict(), decision.to_dict())


class ATamperedDecisionIsNeverTrusted(unittest.TestCase):
    def mutate(self, decision: ReviewDecision, change) -> dict:  # noqa: ANN001
        data = decision.to_dict()
        change(data)
        return data

    def refused(self, decision: ReviewDecision, change, *codes: str) -> None:  # noqa: ANN001
        data = self.mutate(decision, change)
        code = code_of(lambda: ReviewDecision.from_dict(data))
        if codes:
            self.assertIn(code, codes)

    def test_each_kind_of_edit_is_caught(self) -> None:
        auto, gap, human = route([]), route([signal("MISSING_EVIDENCE", "high")]), route([signal("POLICY_CONFLICT")])
        self.refused(auto, lambda d: d.update(decision_id="art:review.decision:sha256:" + "0" * 64), "ID_MISMATCH")
        self.refused(auto, lambda d: d["quality_sampling"].update(selected=not d["quality_sampling"]["selected"]), "SAMPLING_MISMATCH")
        self.refused(auto, lambda d: d.update(next_action="REQUEST_HUMAN_DECISION"), "NEXT_ACTION")
        self.refused(auto, lambda d: d.update(validation_effect="VALIDATED"), "SCHEMA")
        self.refused(auto, lambda d: d.update(engine="review-router/2"), "SCHEMA")
        self.refused(gap, lambda d: d.update(decision_class="SAMPLE_QA"), "SCHEMA", "CLASS_MISMATCH", "PLAN_MISMATCH")
        self.refused(gap, lambda d: d["assessments"][0].update(role="secondary"), "ROLE_MISMATCH", "SCHEMA")
        self.refused(gap, lambda d: d["policy"].update(digest="sha256:" + "2" * 64), "ID_MISMATCH")
        self.refused(gap, lambda d: d["evidence_snapshot"].update(digest="sha256:" + "3" * 64), "SNAPSHOT")
        self.refused(gap, lambda d: d.update(signals=[]), "ID_MISMATCH")
        self.refused(human, lambda d: d.update(requires_named_human=False), "SCHEMA")
        self.refused(human, lambda d: d.pop("human_decision_candidates"), "SCHEMA")
        self.refused(auto, lambda d: d.update(human_decision_candidates=human.data["human_decision_candidates"]), "SCHEMA")

    def test_a_non_object_is_refused(self) -> None:
        self.assertEqual(code_of(lambda: ReviewDecision.from_dict("not a decision")), "INVALID_DECISION")  # type: ignore[arg-type]

    def test_a_consistent_forgery_is_caught_by_recomputation(self) -> None:
        decision = route([signal("MISSING_EVIDENCE", "high")])
        newer = ReviewRouter(variant(lambda d: d.update(version="9.9.9")))
        self.assertFalse(newer.reproduces(decision))
        self.assertTrue(ReviewRouter().reproduces(decision))

    def test_the_schema_rejects_the_shapes_the_classes_forbid(self) -> None:
        plan = route([recheck_signal()]).to_dict()
        no_plan = {key: value for key, value in plan.items() if key != "recheck_plan"}
        self.assertTrue(VALIDATOR.validate(no_plan, "review-decision"))
        auto = route([]).to_dict()
        self.assertTrue(VALIDATOR.validate({**auto, "primary_reasons": [{"signal_id": "x"}]}, "review-decision"))
        no_sampling = {key: value for key, value in auto.items() if key != "quality_sampling"}
        self.assertTrue(VALIDATOR.validate(no_sampling, "review-decision"))
        human = route([signal("POLICY_CONFLICT")]).to_dict()
        self.assertTrue(VALIDATOR.validate({**human, "quality_sampling": auto["quality_sampling"]}, "review-decision"))
        self.assertTrue(VALIDATOR.validate({**human, "surprise": 1}, "review-decision"))


class TheTaxonomyAndPolicyExtendWithoutChangingCode(unittest.TestCase):
    def test_a_family_the_policy_does_not_know_follows_the_default_rule(self) -> None:
        decision = route([signal("CHECKSUM_DRIFT", "critical", evidence=(doc_binding(),))])
        self.assertEqual(decision.decision_class, "AI_RECHECK")
        self.assertEqual(decision.assessments[0]["rule"], "default")
        self.assertEqual(route([signal("CHECKSUM_DRIFT", "low")]).decision_class, "AUTO_PASS")

    def test_adding_a_rule_to_the_policy_gives_a_new_family_its_own_route_with_no_code_change(self) -> None:
        extended = variant(lambda d: d["rules"].update({"CHECKSUM_DRIFT": {"decision_kind": "assess_concern", "actions": {"low": "OBSERVE", "medium": "SOURCE_GAP", "high": "HUMAN_DECISION", "critical": "HUMAN_DECISION"}}}))
        self.assertEqual(route([signal("CHECKSUM_DRIFT", "high")], policy=extended).decision_class, "HUMAN_DECISION")
        self.assertEqual(route([signal("CHECKSUM_DRIFT", "medium")], policy=extended).decision_class, "SOURCE_GAP")
        # Sem a regra nova, a mesma família cai na regra padrão: pede releitura, e sem evidência isso vira lacuna de fonte.
        self.assertEqual(route([signal("CHECKSUM_DRIFT", "high")]).decision_class, "SOURCE_GAP")

    def test_a_qualifier_the_policy_does_not_know_falls_back_to_the_family_rule(self) -> None:
        decision = route([signal("CONFLICTING_EVIDENCE", "high", qualifier="brand_new_kind")])
        self.assertEqual((decision.decision_class, decision.assessments[0]["rule"]), ("HUMAN_DECISION", "CONFLICTING_EVIDENCE"))

    def test_a_policy_can_add_an_impact_class_and_the_router_uses_it(self) -> None:
        def extend(data: dict) -> None:
            impact = data["impact"]
            impact["order"] = ["informational", "operational", "reputational", "data_integrity", "financial", "fiscal", "security", "external_transmission", "irreversible_action"]
            impact["floors"]["reputational"] = "medium"
            impact["effects"]["posts-publicly"] = "reputational"

        policy = variant(extend)
        decision = route([signal("AMBIGUOUS_INTERPRETATION", "low", effects=("posts-publicly",))], policy=policy)
        self.assertEqual((decision.assessments[0]["impact_class"], decision.assessments[0]["effective_criticality"], decision.decision_class), ("reputational", "medium", "SAMPLE_QA"))

    def test_a_threshold_change_moves_which_gaps_block(self) -> None:
        strict = variant(lambda d: d.update(blocking_from="medium"))
        mixed = [signal("MISSING_EVIDENCE", "medium"), signal("AMBIGUOUS_INTERPRETATION", "high", evidence=(doc_binding(),), target_ref="t2")]
        self.assertEqual(route(mixed).decision_class, "AI_RECHECK")
        self.assertEqual(route(mixed, policy=strict).decision_class, "SOURCE_GAP")


class AnUnrelatedSecondSoftwareUsesTheSameEngine(unittest.TestCase):
    def test_a_database_schema_routes_through_every_class_with_no_code_change(self) -> None:
        cases = {
            "AUTO_PASS": [],
            "SAMPLE_QA": [signal("LOW_CONFIDENCE", "low", subject=SUBJECT_TWO)],
            "AI_RECHECK": [recheck_signal("critical", target="n1", subject=SUBJECT_TWO)],
            "SOURCE_GAP": [signal("TRUNCATED_EVIDENCE", "high", subject=SUBJECT_TWO, evidence=(ddl_binding(),))],
            "HUMAN_DECISION": [signal("CONFLICTING_EVIDENCE", "high", subject=SUBJECT_TWO, qualifier="direct_conflict")],
        }
        for expected, signals in cases.items():
            decision = route(signals, subject=SUBJECT_TWO)
            with self.subTest(expected):
                self.assertEqual(decision.decision_class, expected)
                self.assertEqual(decision.subject.kind, "database-schema")
                self.assertEqual(decision.data.get("software_id"), "synthetic-two")
                self.assertEqual(VALIDATOR.validate(decision.to_dict(), "review-decision"), [])

    def test_the_two_softwares_never_share_an_identity(self) -> None:
        self.assertNotEqual(route([], subject=SUBJECT_ONE).decision_id, route([], subject=SUBJECT_TWO).decision_id)

    def test_every_other_kind_of_subject_works_too(self) -> None:
        for kind in ("source-code", "runtime-trace", "configuration", "api", "workflow", "business-rule", "release-note", "artifact"):
            subject = ReviewSubject(f"thing:{kind}", kind)
            decision = route([AnalysisSignal("AMBIGUOUS_INTERPRETATION", subject.ref, subject.kind, "dúvida", "high", evidence=(doc_binding(),))], subject=subject)
            with self.subTest(kind):
                self.assertEqual(decision.decision_class, "AI_RECHECK")

    def test_no_screen_vocabulary_appears_in_any_key_of_any_decision(self) -> None:
        widgets = {"screen", "button", "tab", "tabs", "grid", "lookup", "menu", "menu_path", "combobox", "field", "codigo"}
        for signals in ([], [recheck_signal()], [signal("MISSING_EVIDENCE", "high")], [signal("POLICY_CONFLICT")], [signal("LOW_CONFIDENCE", "low")]):
            self.assertEqual(walk_keys(route(signals).to_dict()) & widgets, set())


class ReviewSubjectIsChecked(unittest.TestCase):
    def test_the_subject_is_validated(self) -> None:
        with self.assertRaises(ModelContractError):
            ReviewSubject("C:\\Users\\x\\y", "documentation")
        with self.assertRaises(ModelContractError):
            ReviewSubject("doc:x", "Not A Kind")
        with self.assertRaises(ReviewContractError):
            ReviewSubject("doc:x", "documentation", software_id="Bad Id")

    def test_the_router_refuses_wrong_types(self) -> None:
        router = ReviewRouter()
        with self.assertRaises(TypeError):
            router.route("not a subject", [])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            router.route(SUBJECT_ONE, ["not a signal"])  # type: ignore[list-item]
        with self.assertRaises(TypeError):
            ReviewRouter("not a policy")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            router.route(SUBJECT_ONE, [], history=["not a record"])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
