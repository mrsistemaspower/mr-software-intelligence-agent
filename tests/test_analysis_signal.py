"""AnalysisSignal: contrato portátil, identidade determinística, vocabulário igual ao dos schemas e nenhuma dependência de software."""
from __future__ import annotations

import unittest

from core import analysis_signal as sig
from core.analysis_signal import (
    AnalysisSignal,
    ClaimAssessment,
    EvidenceBinding,
    Position,
    SourceNeed,
    bindings_tuple,
    confidence_band,
)
from core.epistemics import band
from core.model_task import SOURCE_KINDS, EvidenceRef, ModelContractError
from core.review_policy import default_policy
from core.schema_lite import Registry, Validator
from tests.helpers import REPO, load_json
from tests.recheck_helpers import DOC, DOC_REF
from tests.review_helpers import SUBJECT_ONE, SUBJECT_TWO, doc_binding, signal

VALIDATOR = Validator(Registry())
SIGNAL_SCHEMA = load_json(REPO / "schemas" / "analysis-signal.schema.json")


def code_of(call) -> str:  # noqa: ANN001
    try:
        call()
    except ModelContractError as exc:  # a base: os verificadores de model_task levantam a base, os de revisão, a subclasse
        return exc.code
    raise AssertionError("esperava ModelContractError")


class TheSignalIsAPortableContract(unittest.TestCase):
    def test_a_minimal_signal_has_sensible_defaults(self) -> None:
        item = AnalysisSignal("LOW_CONFIDENCE", "doc:x", "documentation", "pouca certeza")
        self.assertEqual((item.criticality, item.target_ref), ("medium", "doc:x"))
        self.assertEqual((item.effects, item.evidence, item.positions), ((), (), ()))
        self.assertRegex(item.signal_id, r"^art:analysis\.signal:sha256:[0-9a-f]{64}$")

    def test_the_stored_form_validates_and_round_trips(self) -> None:
        item = signal(
            "CONFLICTING_EVIDENCE", "high", qualifier="direct_conflict", confidence="high", impact_class="financial",
            effects=("computes-monetary-value", "writes-data"), evidence=(doc_binding(),), boundary_limit="LOCAL",
            positions=(Position("c1", "A", "observed"), Position("c2", "B", "human")), recommended_role="domain-owner",
            source_need=SourceNeed("o valor efetivo", "decide a posição", ("runtime", "human-expert")),
        )
        data = item.to_dict()
        self.assertEqual(VALIDATOR.validate(data, "analysis-signal"), [])
        self.assertEqual(AnalysisSignal.from_dict(data), item)
        self.assertEqual(AnalysisSignal.from_dict(data).to_dict(), data)

    def test_any_kind_of_subject_is_accepted(self) -> None:
        for kind in ("source-code", "database-schema", "runtime-trace", "configuration", "api", "workflow", "business-rule", "release-note", "artifact", "unknown-object"):
            with self.subTest(kind=kind):
                item = AnalysisSignal("AMBIGUOUS_INTERPRETATION", "thing:1", kind, "dúvida")
                self.assertEqual(VALIDATOR.validate(item.to_dict(), "analysis-signal"), [])

    def test_the_schema_does_not_know_any_widget_or_screen_vocabulary(self) -> None:
        words = ("screen", "button", "tab", "grid", "lookup", "menu", "combobox", "dialog")
        text = (REPO / "schemas" / "analysis-signal.schema.json").read_text(encoding="utf-8").lower()
        self.assertEqual([word for word in words if f'"{word}"' in text or f'"{word}_' in text], [])

    def test_unknown_fields_are_refused_by_the_schema(self) -> None:
        data = signal("LOW_CONFIDENCE").to_dict()
        self.assertTrue(VALIDATOR.validate({**data, "surprise": 1}, "analysis-signal"))
        self.assertEqual(code_of(lambda: AnalysisSignal.from_dict({**data, "surprise": 1})), "SCHEMA")

    def test_the_evidence_is_referenced_by_hash_never_embedded(self) -> None:
        data = signal("MISSING_EVIDENCE", evidence=(doc_binding(),)).to_dict()
        self.assertEqual(set(data["evidence"][0]), {"ref", "sha256"})
        self.assertNotIn("content", str(data))
        self.assertNotIn(DOC.decode("utf-8")[:20], str(data))

    def test_no_local_path_travels_in_a_signal(self) -> None:
        self.assertEqual(code_of(lambda: signal("LOW_CONFIDENCE", summary="veja C:\\Users\\alguem\\x.txt")), "LOCAL_PATH")

    def test_the_family_is_open_and_qualifiers_refine_it(self) -> None:
        item = signal("CHECKSUM_DRIFT", qualifier="minor")
        self.assertEqual(item.rule_key, "CHECKSUM_DRIFT:minor")
        self.assertEqual(signal("CHECKSUM_DRIFT").rule_key, "CHECKSUM_DRIFT")
        self.assertEqual(VALIDATOR.validate(item.to_dict(), "analysis-signal"), [])


class IdentityIsDeterministicAndContentBased(unittest.TestCase):
    def test_same_content_same_id_regardless_of_construction_order(self) -> None:
        one = signal("CONFLICTING_EVIDENCE", effects=("deletes-data", "writes-data"), positions=(Position("a", "x"), Position("b", "y")))
        two = signal("CONFLICTING_EVIDENCE", effects=("writes-data", "deletes-data"), positions=(Position("b", "y"), Position("a", "x")))
        self.assertEqual(one.signal_id, two.signal_id)

    def test_every_meaningful_field_changes_the_id(self) -> None:
        base = signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),))
        changed = [
            signal("AMBIGUOUS_INTERPRETATION", "medium", evidence=(doc_binding(),), summary=base.summary),
            signal("LOW_CONFIDENCE", "high", evidence=(doc_binding(),), summary=base.summary),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary="outro resumo"),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, qualifier="q"),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, confidence="low"),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, impact_class="financial"),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, effects=("deletes-data",)),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, target_ref="other"),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, boundary_limit="LOCAL"),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, recommended_role="owner"),
            signal("LOW_CONFIDENCE", "medium", evidence=(), summary=base.summary),
            signal("LOW_CONFIDENCE", "medium", evidence=(EvidenceBinding(DOC_REF, "b" * 64),), summary=base.summary),
            signal("LOW_CONFIDENCE", "medium", evidence=(doc_binding(),), summary=base.summary, subject=SUBJECT_TWO),
        ]
        ids = {item.signal_id for item in changed} | {base.signal_id}
        self.assertEqual(len(ids), len(changed) + 1)

    def test_a_tampered_signal_is_refused_on_load(self) -> None:
        data = signal("LOW_CONFIDENCE").to_dict()
        data["summary"] = "adulterado"
        self.assertEqual(code_of(lambda: AnalysisSignal.from_dict(data)), "ID_MISMATCH")
        self.assertEqual(code_of(lambda: AnalysisSignal("LOW_CONFIDENCE", "doc:x", "documentation", "x", signal_id="art:analysis.signal:sha256:" + "0" * 64)), "ID_MISMATCH")

    def test_no_clock_machine_or_random_enters_the_id(self) -> None:
        self.assertEqual(signal("LOW_CONFIDENCE").signal_id, signal("LOW_CONFIDENCE").signal_id)
        data = signal("LOW_CONFIDENCE").to_dict()
        for forbidden in ("timestamp", "created_at", "uuid", "hostname", "provider"):
            self.assertNotIn(forbidden, data)


class FieldsAreChecked(unittest.TestCase):
    def test_bad_values_are_refused_with_stable_codes(self) -> None:
        cases = {
            "INVALID_FAMILY": lambda: signal("low_confidence"),
            "INVALID_CHOICE": lambda: signal("LOW_CONFIDENCE", "urgent"),
            "INVALID_QUALIFIER": lambda: signal("LOW_CONFIDENCE", qualifier="Direct Conflict"),
            "INVALID_IMPACT": lambda: signal("LOW_CONFIDENCE", impact_class="Financial!"),
            "INVALID_EFFECT": lambda: signal("LOW_CONFIDENCE", effects=("Deletes Data",)),
            "LOCAL_PATH": lambda: signal("LOW_CONFIDENCE", summary="C:\\Users\\a\\b.txt"),
            "EVIDENCE_HASH_CONFLICT": lambda: signal("LOW_CONFIDENCE", evidence=(EvidenceBinding("e1", "a" * 64), EvidenceBinding("e1", "b" * 64))),
            "INVALID_HASH": lambda: EvidenceBinding("e1", "XYZ"),
            "INVALID_SOURCE_TYPES": lambda: SourceNeed("x", "y", "runtime"),
        }
        for code, call in cases.items():
            with self.subTest(code=code):
                self.assertEqual(code_of(call), code)

    def test_confidence_and_boundary_are_closed_sets(self) -> None:
        for bad in ("very-high", "HIGH", 0.9):
            with self.subTest(confidence=bad), self.assertRaises(ModelContractError):
                signal("LOW_CONFIDENCE", confidence=bad)  # type: ignore[arg-type]
        with self.assertRaises(ModelContractError):
            signal("LOW_CONFIDENCE", boundary_limit="OFFSITE")

    def test_collections_are_bounded(self) -> None:
        self.assertEqual(code_of(lambda: signal("LOW_CONFIDENCE", effects=tuple(f"effect-{i}" for i in range(sig.MAX_EFFECTS + 1)))), "TOO_MANY_ITEMS")
        self.assertEqual(
            code_of(lambda: signal("LOW_CONFIDENCE", positions=tuple(Position(f"p{i}", "x") for i in range(sig.MAX_POSITIONS + 1)))), "TOO_MANY_ITEMS"
        )

    def test_a_string_is_not_a_list(self) -> None:
        self.assertEqual(code_of(lambda: signal("LOW_CONFIDENCE", effects="deletes-data")), "INVALID_EFFECT")  # type: ignore[arg-type]
        self.assertEqual(code_of(lambda: bindings_tuple("doc-1")), "INVALID_EVIDENCE")

    def test_bindings_accept_the_authorized_evidence_itself(self) -> None:
        item = EvidenceRef.of(DOC_REF, DOC, "documentation", contains_sensitive_data=False)
        self.assertEqual(bindings_tuple([item]), (EvidenceBinding(DOC_REF, item.sha256),))

    def test_the_same_binding_twice_collapses(self) -> None:
        self.assertEqual(len(bindings_tuple([doc_binding(), doc_binding()])), 1)


class CriticalityAndConfidenceAreSeparateAxes(unittest.TestCase):
    def test_criticality_is_ordered_and_confidence_is_not_derived_from_it(self) -> None:
        self.assertEqual([sig.criticality_rank(item) for item in sig.CRITICALITIES], [0, 1, 2, 3])
        self.assertEqual(sig.max_criticality("low", "critical", "medium"), "critical")
        for criticality in sig.CRITICALITIES:
            for confidence in sig.CONFIDENCE_BANDS:
                item = signal("LOW_CONFIDENCE", criticality, confidence=confidence)
                self.assertEqual((item.criticality, item.confidence), (criticality, confidence))

    def test_the_confidence_band_is_calculated_never_stored(self) -> None:
        self.assertIs(confidence_band, band)
        self.assertEqual([confidence_band(value) for value in (0.1, 0.6, 0.95)], ["low", "medium", "high"])
        self.assertNotIn("confidence_band", signal("LOW_CONFIDENCE").to_dict())

    def test_max_criticality_needs_a_value(self) -> None:
        self.assertEqual(code_of(sig.max_criticality), "INVALID_CRITICALITY")


class VocabularyMatchesTheSchemas(unittest.TestCase):
    def defs(self, name: str) -> dict:
        return SIGNAL_SCHEMA["$defs"][name]

    def test_closed_sets_equal_the_schema_enums(self) -> None:
        self.assertEqual(list(sig.CRITICALITIES), self.defs("criticality")["enum"])
        self.assertEqual(list(sig.CONFIDENCE_BANDS), self.defs("confidenceBand")["enum"])
        self.assertEqual(list(sig.SOURCE_TYPES), self.defs("sourceType")["enum"])

    def test_decision_vocabulary_equals_the_decision_schema(self) -> None:
        schema = load_json(REPO / "schemas" / "review-decision.schema.json")["$defs"]
        self.assertEqual(list(sig.REVIEW_CLASSES), schema["reviewClass"]["enum"])
        self.assertEqual(list(sig.ACTIONS), schema["action"]["enum"])
        candidate = load_json(REPO / "schemas" / "human-decision-candidate.schema.json")
        self.assertEqual(list(sig.DECISION_KINDS), candidate["properties"]["decision_kind"]["enum"])

    def test_family_effect_qualifier_and_impact_patterns_equal_the_schema(self) -> None:
        self.assertEqual(sig.FAMILY_PATTERN, self.defs("family")["pattern"])
        self.assertEqual(sig.QUALIFIER_PATTERN, self.defs("qualifier")["pattern"])
        self.assertEqual(sig.EFFECT_PATTERN, self.defs("effect")["pattern"])
        self.assertEqual(sig.IMPACT_PATTERN, self.defs("impactClass")["pattern"])


class SourceTypesAreGenericAndMapToRegisteredKinds(unittest.TestCase):
    def test_the_generic_types_are_the_agreed_ones(self) -> None:
        self.assertEqual(
            set(sig.SOURCE_TYPES),
            {"documentation", "runtime", "database", "source-code", "screenshot", "filesystem", "release-notes", "human-expert", "external-system"},
        )

    def test_every_mapped_kind_is_a_registered_source_kind(self) -> None:
        common = load_json(REPO / "schemas" / "common.schema.json")["$defs"]["sourceKind"]["enum"]
        self.assertEqual(list(SOURCE_KINDS), common)
        self.assertEqual(set(sig.SOURCE_TYPE_TO_KIND), set(sig.SOURCE_TYPES))
        for source_type, kind in sig.SOURCE_TYPE_TO_KIND.items():
            with self.subTest(source_type):
                self.assertTrue(kind is None or kind in SOURCE_KINDS)
        self.assertEqual(set(sig.SOURCE_KIND_TO_TYPE), set(SOURCE_KINDS))
        for kind, source_type in sig.SOURCE_KIND_TO_TYPE.items():
            self.assertIn(source_type, sig.SOURCE_TYPES, kind)

    def test_mapping_in_both_directions(self) -> None:
        self.assertEqual(sig.source_type_for_kind("database-schema"), "database")
        self.assertEqual(sig.source_type_for_kind("wiki"), "documentation")
        self.assertEqual(sig.source_kind_for_type("database"), "database-schema")
        self.assertIsNone(sig.source_kind_for_type("external-system"))
        self.assertEqual(code_of(lambda: sig.source_type_for_kind("mystery")), "INVALID_SOURCE_KIND")
        with self.assertRaises(ModelContractError):
            sig.source_kind_for_type("mystery")

    def test_no_adapter_or_product_name_is_a_source_type(self) -> None:
        for name in sig.SOURCE_TYPES:
            self.assertNotRegex(name, r"(?i)firebird|dokuwiki|d8|d9|oracle|postgres|mysql")


class TheTaxonomyIsCoherentAndMinimal(unittest.TestCase):
    def test_the_base_families_are_what_the_default_policy_knows(self) -> None:
        policy = default_policy()
        self.assertEqual(len(sig.BASE_FAMILIES), 11)
        self.assertEqual(len(set(sig.BASE_FAMILIES)), 11)
        for family in sig.BASE_FAMILIES:
            with self.subTest(family):
                self.assertTrue(policy.is_known_family(family))
                self.assertFalse(policy.rule_for(family).fallback)
        known = {key.split(":")[0] for key in policy.data["rules"]}
        self.assertEqual(known, set(sig.BASE_FAMILIES))

    def test_the_evidence_availability_families_exist(self) -> None:
        # A) existe mas é incerta; B) não existe ou está truncada; C) existe e se contradiz.
        for family in ("LOW_CONFIDENCE", "AMBIGUOUS_INTERPRETATION", "SELF_AUDIT_REVIEW", "MISSING_EVIDENCE", "TRUNCATED_EVIDENCE", "CONFLICTING_EVIDENCE"):
            self.assertIn(family, sig.BASE_FAMILIES)


class ClaimAssessmentDeclaresEffectsNotImpactDecisions(unittest.TestCase):
    def test_it_turns_into_the_high_impact_signal_without_deciding_anything(self) -> None:
        claim = ClaimAssessment("claim-1", "Remove registros antigos.", "INFERRED", effects=("deletes-data",), recommended_role="data-owner")
        item = claim.as_signal(SUBJECT_ONE.ref, SUBJECT_ONE.kind)
        self.assertEqual((item.family, item.target_ref, item.effects, item.recommended_role), ("UNVERIFIED_HIGH_IMPACT_CLAIM", "claim-1", ("deletes-data",), "data-owner"))
        self.assertIsNone(item.impact_class, "a classe de impacto sai da política, não do adaptador")

    def test_status_is_the_epistemic_vocabulary(self) -> None:
        for status in ("OBSERVED", "INFERRED", "VALIDATED"):
            ClaimAssessment("claim-1", "x", status)
        with self.assertRaises(ModelContractError):
            ClaimAssessment("claim-1", "x", "PROVEN")


if __name__ == "__main__":
    unittest.main()
