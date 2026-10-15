"""Escalonamento: os casos de avaliação e as propriedades da escada."""
from __future__ import annotations

import itertools
import unittest

from tests.helpers import REPO, load_json
from core.escalation import DEFAULT_SIGNALS, NO_ESCALATION, Decision, decide, dedupe_questions, load_policy

POLICY = load_policy()
HUMAN = ("HUMAN_DECISION", "HUMAN_VALIDATION")


class PolicyFile(unittest.TestCase):
    def test_ladder_order(self) -> None:
        self.assertEqual(
            [step["step"] for step in POLICY["ladder"]],
            ["prior-validated-knowledge", "evidence-reuse", "documentation", "relations", "ai-recheck", "runtime"],
        )

    def test_runtime_is_the_last_automatic_step(self) -> None:
        self.assertEqual(POLICY["ladder"][-1]["step"], "runtime")

    def test_only_human_routes_require_a_named_person(self) -> None:
        required = {name: route["requires_named_person"] for name, route in POLICY["routes"].items()}
        self.assertEqual(
            required,
            {"AI_RECHECK": False, "SOURCE_GAP": False, "HUMAN_DECISION": True, "HUMAN_VALIDATION": True},
        )

    def test_invalid_policy_is_rejected(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            bad = {**POLICY, "thresholds": {"low_below": 2, "high_from": 0.8}}
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_policy(path)


class EvaluationCases(unittest.TestCase):
    cases = load_json(REPO / "evaluations" / "escalation-cases.json")["cases"]

    def test_case_ids_are_unique(self) -> None:
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_route_and_reason_is_covered(self) -> None:
        self.assertEqual(
            {c["expected_route"] for c in self.cases},
            {"NO_ESCALATION", "AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION"},
        )
        self.assertEqual(
            {c["expected_reason"] for c in self.cases},
            {"reuse", "already-validated", "no-evidence", "needs-choice", "ambiguous-reading", "recheck-unresolved",
             "high-impact-unvalidated", "sufficient"},
        )

    def test_each_case(self) -> None:
        for case in self.cases:
            with self.subTest(case["id"]):
                decision = decide(case["signals"], POLICY)
                self.assertEqual(decision.route, case["expected_route"])
                self.assertEqual(decision.reason, case["expected_reason"])
                self.assertEqual(decision.needed_source, case.get("expected_needed_source"))


class LadderProperties(unittest.TestCase):
    """Propriedades sobre todo o espaço de sinais (2592 combinações)."""

    @staticmethod
    def grid():
        for prior, status, evidence, choices, conf, done, resolved, inference, impact in itertools.product(
            (False, True), ("OBSERVED", "INFERRED", "VALIDATED"), (False, True), (0, 1, 2), (0.2, 0.5, 0.9),
            (False, True), (False, True), (False, True), ("low", "normal", "high"),
        ):
            signals = {
                "prior_validated_applies": prior, "status": status, "evidence_present": evidence, "choice_options": choices,
                "confidence": conf, "recheck_done": done, "recheck_resolved": resolved, "inference_to_confirm": inference,
                "impact": impact,
            }
            yield signals, decide(signals, POLICY)

    def test_grid_size(self) -> None:
        # 2 (prior) x 3 (status) x 2 (evidência) x 3 (escolhas) x 3 (confiança) x 2 x 2 x 2 (recheck, resolvido, inferência) x 3 (impacto)
        self.assertEqual(sum(1 for _ in self.grid()), 2 * 3 * 2 * 3 * 3 * 2 * 2 * 2 * 3)

    def test_validated_or_reusable_knowledge_never_escalates(self) -> None:
        for s, d in self.grid():
            if s["prior_validated_applies"] or s["status"] == "VALIDATED":
                self.assertEqual(d.route, NO_ESCALATION, s)

    def test_missing_evidence_is_always_a_source_gap(self) -> None:
        for s, d in self.grid():
            if not s["evidence_present"] and not s["prior_validated_applies"] and s["status"] != "VALIDATED":
                self.assertEqual(d.route, "SOURCE_GAP", s)

    def test_humans_are_asked_only_when_the_ladder_cannot_help(self) -> None:
        for s, d in self.grid():
            if d.route not in HUMAN:
                continue
            reread_failed = s["confidence"] < 0.5 and s["recheck_done"] and not s["recheck_resolved"]
            allowed = (
                s["choice_options"] >= 2
                or (s["inference_to_confirm"] and (s["impact"] == "high" or reread_failed))
            )
            self.assertTrue(allowed, (s, d))
            self.assertTrue(s["evidence_present"], s)

    def test_ai_goes_first_on_ambiguous_readings(self) -> None:
        for s, d in self.grid():
            ambiguous_first_pass = (
                s["evidence_present"] and s["choice_options"] < 2 and s["confidence"] < 0.5 and not s["recheck_done"]
                and not s["prior_validated_applies"] and s["status"] != "VALIDATED"
            )
            if ambiguous_first_pass:
                self.assertEqual(d.route, "AI_RECHECK", s)

    def test_ai_recheck_only_with_evidence_and_no_previous_recheck(self) -> None:
        for s, d in self.grid():
            if d.route == "AI_RECHECK":
                self.assertTrue(s["evidence_present"] and not s["recheck_done"], s)

    def test_validation_always_has_something_to_confirm(self) -> None:
        for s, d in self.grid():
            if d.route == "HUMAN_VALIDATION":
                self.assertTrue(s["inference_to_confirm"], s)

    def test_needed_source_only_on_source_gap(self) -> None:
        for s, d in self.grid():
            self.assertEqual(d.needed_source is not None, d.route == "SOURCE_GAP", (s, d))

    def test_is_human_matches_route(self) -> None:
        self.assertTrue(Decision("HUMAN_DECISION", "x").is_human)
        self.assertTrue(Decision("HUMAN_VALIDATION", "x").is_human)
        for route in ("AI_RECHECK", "SOURCE_GAP", NO_ESCALATION):
            self.assertFalse(Decision(route, "x").is_human)

    def test_decide_is_deterministic(self) -> None:
        signals = {"confidence": 0.3, "choice_options": 0}
        self.assertEqual(decide(signals, POLICY), decide(dict(signals), POLICY))


class PolicyDrivesDecisions(unittest.TestCase):
    def test_thresholds_come_from_the_policy(self) -> None:
        strict = {**POLICY, "thresholds": {"low_below": 0.9, "high_from": 0.95}}
        self.assertEqual(decide({"confidence": 0.85}, strict).route, "AI_RECHECK")
        self.assertEqual(decide({"confidence": 0.85}, POLICY).route, NO_ESCALATION)

    def test_high_impact_rule_can_be_switched_off(self) -> None:
        lax = {**POLICY, "impact_rules": {"high_requires_validation": False}}
        signals = {"impact": "high", "inference_to_confirm": True, "confidence": 0.95}
        self.assertEqual(decide(signals, POLICY).route, "HUMAN_VALIDATION")
        self.assertEqual(decide(signals, lax).route, NO_ESCALATION)

    def test_unknown_signals_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decide({"confidnce": 0.1}, POLICY)

    def test_defaults_are_documented_signals(self) -> None:
        self.assertEqual(set(DEFAULT_SIGNALS), {
            "prior_validated_applies", "status", "evidence_present", "source_obtainable", "confidence", "recheck_done",
            "recheck_resolved", "choice_options", "inference_to_confirm", "impact",
        })


class Deduplication(unittest.TestCase):
    def review(self, rid, question, family=None) -> dict:
        record = {"id": rid, "question": question}
        if family:
            record["family_key"] = family
        return record

    def test_same_family_key_is_asked_once(self) -> None:
        groups = dedupe_questions([
            self.review("rev:a", "Qual o destino?", "lookup-target"),
            self.review("rev:b", "Para onde leva este botão?", "lookup-target"),
            self.review("rev:c", "Outra coisa?"),
        ])
        self.assertEqual([[r["id"] for r in g] for g in groups], [["rev:a", "rev:b"], ["rev:c"]])

    def test_normalised_question_groups_when_there_is_no_family(self) -> None:
        groups = dedupe_questions([
            self.review("rev:a", "Para onde leva o botão '...'?"),
            self.review("rev:b", "  para ONDE leva o botao ...  "),
        ])
        self.assertEqual(len(groups), 1)

    def test_distinct_questions_stay_separate_and_order_is_stable(self) -> None:
        groups = dedupe_questions([self.review("rev:b", "B?"), self.review("rev:a", "A?")])
        self.assertEqual([g[0]["id"] for g in groups], ["rev:b", "rev:a"])

    def test_empty_input(self) -> None:
        self.assertEqual(dedupe_questions([]), [])


if __name__ == "__main__":
    unittest.main()
