"""Política de roteamento como dado: carga, validação estrita, regras de segurança, identidade e versionamento."""
from __future__ import annotations

import json
import unittest

from core import review_policy as rp
from core.analysis_signal import ACTIONS, BASE_FAMILIES, CRITICALITIES, DECISION_KINDS, ReviewContractError
from core.model_task import ModelContractError
from core.review_policy import ReviewPolicy, default_policy, load_policy, policy_problems
from core.serialization import canonical_pretty_bytes
from tests.helpers import REPO, TempDirCase, load_json
from tests.review_helpers import variant

POLICY_FILE = REPO / "policies" / "review-routing.policy.json"


def code_of(call) -> str:  # noqa: ANN001
    try:
        call()
    except ModelContractError as exc:
        return exc.code
    raise AssertionError("esperava ModelContractError")


def broken(change) -> list:  # noqa: ANN001
    data = default_policy().to_dict()
    change(data)
    return policy_problems(data)


class TheDefaultPolicyIsReadableData(unittest.TestCase):
    def test_the_file_loads_and_is_exactly_the_policy_object(self) -> None:
        policy = default_policy()
        self.assertEqual(policy.to_dict(), load_json(POLICY_FILE))
        self.assertEqual(policy, load_policy())
        self.assertEqual((policy.policy_id, policy.version), ("review-routing.default", "1.0.0"))

    def test_it_passes_its_own_validation_with_no_problem(self) -> None:
        self.assertEqual(policy_problems(load_json(POLICY_FILE)), [])

    def test_every_base_family_has_a_rule_with_all_four_criticalities(self) -> None:
        policy = default_policy()
        for family in BASE_FAMILIES:
            rule = policy.rule_for(family)
            with self.subTest(family):
                self.assertEqual([criticality for criticality, _ in rule.actions], list(CRITICALITIES))
                self.assertTrue(all(action in ACTIONS for _, action in rule.actions))

    def test_the_default_table_is_readable_and_matches_the_documented_behaviour(self) -> None:
        table = {
            ("LOW_CONFIDENCE", "low"): "SAMPLE_QA", ("LOW_CONFIDENCE", "high"): "AI_RECHECK",
            ("AMBIGUOUS_INTERPRETATION", "low"): "OBSERVE", ("AMBIGUOUS_INTERPRETATION", "critical"): "AI_RECHECK",
            ("MISSING_EVIDENCE", "low"): "OBSERVE", ("MISSING_EVIDENCE", "medium"): "SOURCE_GAP",
            ("TRUNCATED_EVIDENCE", "high"): "SOURCE_GAP", ("UNKNOWN_RELATION", "medium"): "OBSERVE", ("UNKNOWN_RELATION", "high"): "SOURCE_GAP",
            ("STRUCTURE_ONLY", "critical"): "OBSERVE", ("POLICY_CONFLICT", "low"): "HUMAN_DECISION",
            ("UNVERIFIED_HIGH_IMPACT_CLAIM", "high"): "HUMAN_DECISION", ("UNVERIFIED_HIGH_IMPACT_CLAIM", "medium"): "SAMPLE_QA",
        }
        policy = default_policy()
        for (family, criticality), action in table.items():
            with self.subTest(family=family, criticality=criticality):
                self.assertEqual(policy.rule_for(family).action(criticality), action)

    def test_conflict_kinds_do_not_all_need_a_person(self) -> None:
        policy = default_policy()
        high = {kind: policy.rule_for("CONFLICTING_EVIDENCE", kind).action("high") for kind in
                ("compatible_tension", "resolvable_ambiguity", "direct_conflict", "human_vs_observed")}
        self.assertEqual(high, {"compatible_tension": "SAMPLE_QA", "resolvable_ambiguity": "AI_RECHECK", "direct_conflict": "HUMAN_DECISION", "human_vs_observed": "HUMAN_DECISION"})
        self.assertEqual(policy.rule_for("CONFLICTING_EVIDENCE", "compatible_tension").action("low"), "OBSERVE")

    def test_the_policy_text_names_no_software_and_no_provider(self) -> None:
        text = POLICY_FILE.read_text(encoding="utf-8").lower()
        for term in ("syscom", "acme", "firebird", "dokuwiki", "claude", "openai", "anthropic", "gemini", "copilot"):
            self.assertNotIn(term, text)

    def test_the_file_is_stable_canonical_pretty_json_free_of_local_paths(self) -> None:
        data = load_json(POLICY_FILE)
        from core.paths import find_local_paths

        self.assertEqual(find_local_paths(data), [])
        self.assertIsNotNone(canonical_pretty_bytes(data))


class StrictValidationRefusesWholePolicies(unittest.TestCase):
    def assert_refused(self, change, fragment: str = "") -> None:  # noqa: ANN001
        problems = broken(change)
        self.assertTrue(problems, "a política inválida foi aceita")
        if fragment:
            self.assertIn(fragment, " | ".join(problems))
        data = default_policy().to_dict()
        change(data)
        self.assertEqual(code_of(lambda: ReviewPolicy(data)), "INVALID_POLICY")

    def test_unknown_and_missing_keys(self) -> None:
        self.assert_refused(lambda d: d.update(surprise=1), "chaves")
        self.assert_refused(lambda d: d.pop("sampling"), "chaves")
        self.assert_refused(lambda d: d["impact"].update(extra=1), "impact")
        self.assert_refused(lambda d: d["recheck"].pop("max_rounds"), "recheck")
        self.assert_refused(lambda d: d["rules"]["LOW_CONFIDENCE"].update(extra=1), "regra")

    def test_kind_id_and_version(self) -> None:
        self.assert_refused(lambda d: d.update(kind="something-else"), "kind")
        self.assert_refused(lambda d: d.update(policy_id="Bad Id"), "policy_id")
        self.assert_refused(lambda d: d.update(version=""), "version")

    def test_a_more_critical_signal_never_gets_a_weaker_action(self) -> None:
        self.assert_refused(lambda d: d["rules"]["LOW_CONFIDENCE"]["actions"].update(critical="OBSERVE"), "menos severa")
        self.assert_refused(lambda d: d["rules"]["MISSING_EVIDENCE"]["actions"].update(low="HUMAN_DECISION", critical="SOURCE_GAP"), "menos severa")
        self.assert_refused(lambda d: d["default_rule"]["actions"].update(high="OBSERVE"), "menos severa")

    def test_actions_must_be_known_and_cover_every_criticality(self) -> None:
        self.assert_refused(lambda d: d["rules"]["LOW_CONFIDENCE"]["actions"].update(low="DELETE_IT"), "ação")
        self.assert_refused(lambda d: d["rules"]["LOW_CONFIDENCE"]["actions"].pop("critical"), "criticidades")

    def test_a_rule_that_can_ask_for_a_person_must_say_what_for(self) -> None:
        self.assert_refused(lambda d: d["rules"]["POLICY_CONFLICT"].pop("decision_kind"), "decision_kind")
        self.assert_refused(lambda d: d["rules"]["POLICY_CONFLICT"].update(decision_kind="because"), "decision_kind")

    def test_an_inconclusive_recheck_never_loops_and_never_hides(self) -> None:
        self.assert_refused(lambda d: d["recheck"]["on_inconclusive"].update(high="AI_RECHECK", critical="AI_RECHECK"), "ação")
        self.assert_refused(lambda d: d["recheck"]["on_inconclusive"].update(low="OBSERVE"), "ação")
        self.assert_refused(lambda d: d["recheck"]["on_inconclusive"].update(critical="SAMPLE_QA"), "menos severa")

    def test_round_budget_is_bounded(self) -> None:
        for bad in (0, 6, True, 1.5, "1"):
            with self.subTest(bad=bad):
                self.assert_refused(lambda d, bad=bad: d["recheck"].update(max_rounds=bad), "max_rounds")

    def test_boundary_and_risk_vocabularies_are_closed(self) -> None:
        self.assert_refused(lambda d: d["recheck"].update(default_max_boundary="OFFSITE"), "default_max_boundary")
        self.assert_refused(lambda d: d["recheck"]["risk_by_criticality"].update(low="MAYBE"), "risk_by_criticality")

    def test_impact_rules(self) -> None:
        self.assert_refused(lambda d: d["impact"]["floors"].update(external_transmission="low"), "piso")
        self.assert_refused(lambda d: d["impact"]["floors"].pop("fiscal"), "floors")
        self.assert_refused(lambda d: d["impact"]["high_impact"].append("made_up"), "high_impact")
        self.assert_refused(lambda d: d["impact"]["effects"].update({"deletes-data": "made_up"}), "classe")
        self.assert_refused(lambda d: d["impact"].update(default_effect_impact="made_up"), "default_effect_impact")
        self.assert_refused(lambda d: d["impact"].update(order=["informational", "informational"]), "order")

    def test_validated_knowledge_is_never_unverified(self) -> None:
        self.assert_refused(lambda d: d["impact"].update(unverified_statuses=["INFERRED", "VALIDATED"]), "VALIDATED")
        self.assert_refused(lambda d: d["impact"].update(unverified_statuses=["GUESSED"]), "unverified_statuses")

    def test_sampling_and_source_gap_and_prohibitions(self) -> None:
        self.assert_refused(lambda d: d["sampling"]["rates"].update(SAMPLE_QA=1.5), "rates")
        self.assert_refused(lambda d: d["sampling"]["rates"].update(AUTO_PASS=-0.1), "rates")
        self.assert_refused(lambda d: d["sampling"]["rates"].update(AUTO_PASS=True), "rates")
        self.assert_refused(lambda d: d["sampling"]["rates"].update(HUMAN_DECISION=1), "rates")
        self.assert_refused(lambda d: d["sampling"].update(seed=""), "seed")
        self.assert_refused(lambda d: d["source_gap"].update(default_source_types=["a-vendor-name"]), "default_source_types")
        self.assert_refused(lambda d: d["source_gap"].update(default_source_types=[]), "default_source_types")
        self.assert_refused(lambda d: d["prohibited_assumptions"].pop("validate_claim"), "prohibited_assumptions")
        self.assert_refused(lambda d: d["prohibited_assumptions"].update(validate_claim=[]), "validate_claim")

    def test_rule_keys_are_families_with_optional_qualifier(self) -> None:
        self.assert_refused(lambda d: d["rules"].update({"lowercase": d["default_rule"]}), "chave")
        self.assert_refused(lambda d: d["rules"].update({"FAMILY:Bad Qualifier": d["default_rule"]}), "chave")

    def test_blocking_threshold_is_a_criticality(self) -> None:
        self.assert_refused(lambda d: d.update(blocking_from="urgent"), "blocking_from")

    def test_a_non_object_is_refused(self) -> None:
        self.assertEqual(policy_problems([]), ["a política deve ser objeto"])
        self.assertEqual(code_of(lambda: ReviewPolicy([])), "INVALID_POLICY")  # type: ignore[arg-type]

    def test_every_decision_kind_has_prohibitions_and_the_sets_match(self) -> None:
        self.assertEqual(set(default_policy().data["prohibited_assumptions"]), set(DECISION_KINDS))
        for kind in DECISION_KINDS:
            self.assertTrue(default_policy().prohibited_assumptions(kind))


class RulesAndImpactBehaviour(unittest.TestCase):
    def test_rule_lookup_prefers_the_qualifier_then_the_family_then_the_default(self) -> None:
        policy = default_policy()
        self.assertEqual(policy.rule_for("CONFLICTING_EVIDENCE", "direct_conflict").key, "CONFLICTING_EVIDENCE:direct_conflict")
        self.assertEqual(policy.rule_for("CONFLICTING_EVIDENCE", "unheard_of").key, "CONFLICTING_EVIDENCE")
        self.assertEqual(policy.rule_for("CONFLICTING_EVIDENCE").key, "CONFLICTING_EVIDENCE")
        unknown = policy.rule_for("NEVER_HEARD_OF", "x")
        self.assertEqual((unknown.key, unknown.fallback), (rp.RULE_DEFAULT, True))
        self.assertFalse(policy.is_known_family("NEVER_HEARD_OF"))

    def test_the_blocking_threshold(self) -> None:
        policy = default_policy()
        self.assertEqual([policy.is_blocking(item) for item in CRITICALITIES], [False, False, True, True])
        self.assertEqual(variant(lambda d: d.update(blocking_from="critical")).is_blocking("high"), False)
        self.assertEqual(variant(lambda d: d.update(blocking_from="medium")).is_blocking("medium"), True)

    def test_impact_is_derived_from_effects_and_the_highest_wins(self) -> None:
        policy = default_policy()
        self.assertEqual(policy.derive_impact(None, ()), "informational")
        self.assertEqual(policy.derive_impact(None, ("reads-data",)), "informational")
        self.assertEqual(policy.derive_impact(None, ("reads-data", "deletes-data")), "irreversible_action")
        self.assertEqual(policy.derive_impact("fiscal", ("changes-state",)), "fiscal")
        self.assertEqual(policy.derive_impact("operational", ("transmits-externally",)), "external_transmission")

    def test_an_unknown_effect_is_not_harmless(self) -> None:
        policy = default_policy()
        self.assertEqual(policy.derive_impact(None, ("does-something-new",)), policy.data["impact"]["default_effect_impact"])
        self.assertNotEqual(policy.derive_impact(None, ("does-something-new",)), "informational")

    def test_an_unknown_declared_impact_class_is_refused_not_guessed(self) -> None:
        self.assertEqual(code_of(lambda: default_policy().derive_impact("cosmic", ())), "UNKNOWN_IMPACT")

    def test_floors_raise_with_gravity_and_the_high_impact_set_is_the_policys(self) -> None:
        policy = default_policy()
        floors = [criticality for criticality in (policy.impact_floor(name) for name in policy.impact_order)]
        self.assertEqual(floors, sorted(floors, key=CRITICALITIES.index))
        self.assertTrue(policy.is_high_impact("fiscal"))
        self.assertFalse(policy.is_high_impact("operational"))
        self.assertEqual(policy.impact_floor("irreversible_action"), "critical")

    def test_only_unverified_statuses_count_and_validated_never_does(self) -> None:
        policy = default_policy()
        self.assertEqual([policy.is_unverified(status) for status in ("OBSERVED", "INFERRED", "VALIDATED")], [False, True, False])

    def test_accessors_hand_out_copies(self) -> None:
        policy = default_policy()
        copy = policy.to_dict()
        copy["blocking_from"] = "low"
        copy["rules"].clear()
        self.assertEqual(policy.blocking_from, "high")
        self.assertTrue(policy.rule_for("LOW_CONFIDENCE").actions)

    def test_the_escalation_after_an_inconclusive_recheck(self) -> None:
        policy = default_policy()
        self.assertEqual([policy.on_inconclusive(item) for item in CRITICALITIES], ["SAMPLE_QA", "SAMPLE_QA", "HUMAN_DECISION", "HUMAN_DECISION"])
        self.assertEqual([policy.risk_level(item) for item in CRITICALITIES], ["LOW", "NORMAL", "HIGH", "HIGH"])
        self.assertEqual((policy.max_rounds, policy.default_max_boundary), (1, "LOCAL"))

    def test_sampling_rates_exist_only_for_the_sampled_classes(self) -> None:
        policy = default_policy()
        self.assertEqual((policy.sampling_rate("SAMPLE_QA"), policy.sampling_rate("AUTO_PASS")), (0.2, 0.05))
        for review_class in ("AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION"):
            self.assertIsNone(policy.sampling_rate(review_class))
        self.assertEqual(tuple(rp.SAMPLING_CLASSES), ("SAMPLE_QA", "AUTO_PASS"))


class PolicyIdentityAndVersioning(unittest.TestCase):
    def test_the_reference_has_id_version_and_a_digest_of_everything(self) -> None:
        ref = default_policy().ref()
        self.assertEqual(set(ref), {"policy_id", "version", "digest"})
        self.assertRegex(ref["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_loading_twice_gives_the_same_digest(self) -> None:
        self.assertEqual(load_policy().digest, load_policy().digest)
        self.assertEqual(ReviewPolicy(default_policy().to_dict()).digest, default_policy().digest)

    def test_key_order_does_not_change_the_digest(self) -> None:
        data = default_policy().to_dict()
        reordered = {key: data[key] for key in reversed(list(data))}
        self.assertEqual(ReviewPolicy(reordered).digest, default_policy().digest)

    def test_a_version_bump_changes_the_reference(self) -> None:
        bumped = variant(lambda d: d.update(version="1.0.1"))
        self.assertNotEqual(bumped.ref(), default_policy().ref())
        self.assertNotEqual(bumped.digest, default_policy().digest)

    def test_a_content_change_without_a_version_bump_still_changes_the_digest(self) -> None:
        edited = variant(lambda d: d["rules"]["LOW_CONFIDENCE"]["actions"].update(low="OBSERVE"))
        self.assertEqual(edited.version, default_policy().version)
        self.assertNotEqual(edited.digest, default_policy().digest)

    def test_stage_parameters_carry_the_reference_so_the_stage_engine_invalidates(self) -> None:
        one, two = default_policy(), variant(lambda d: d.update(version="2.0.0"))
        self.assertEqual(one.stage_parameters(), {rp.PARAMETER_KEY: {"policy": one.ref()}})
        self.assertNotEqual(one.stage_parameters(), two.stage_parameters())
        json.dumps(one.stage_parameters())


class LoadingIsTheOnlyFileRead(TempDirCase):
    def test_a_policy_file_elsewhere_loads_when_valid(self) -> None:
        target = self.tmp / "other.policy.json"
        target.write_bytes(canonical_pretty_bytes(default_policy().to_dict()))
        self.assertEqual(load_policy(target).digest, default_policy().digest)

    def test_missing_malformed_and_invalid_files_are_refused_with_stable_codes(self) -> None:
        self.assertEqual(code_of(lambda: load_policy(self.tmp / "absent.json")), "UNREADABLE_POLICY")
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(code_of(lambda: load_policy(bad)), "UNREADABLE_POLICY")
        invalid = self.tmp / "invalid.json"
        invalid.write_text(json.dumps({"kind": "review-routing-policy"}), encoding="utf-8")
        self.assertEqual(code_of(lambda: load_policy(invalid)), "INVALID_POLICY")

    def test_the_error_never_leaks_a_local_path(self) -> None:
        try:
            load_policy(self.tmp / "absent.json")
        except ReviewContractError as exc:
            self.assertNotIn(str(self.tmp), str(exc))


if __name__ == "__main__":
    unittest.main()
