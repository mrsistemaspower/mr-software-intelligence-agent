"""Schemas válidos e regras epistêmicas impostas por eles."""
from __future__ import annotations

import copy
import unittest

from tests.helpers import REPO, load_json
from core.schema_lite import Registry, Validator

EXPECTED_SCHEMAS = {
    "agent-workflow", "business-rule", "common", "contradiction", "escalation-policy", "evidence", "fact",
    "human-review", "integration-reference", "interaction", "knowledge-snapshot", "llm-profile", "lookup", "release-change", "screen",
    "run-config", "run-state", "stage-checkpoint", "stage-graph",
    "ai-recheck-request", "ai-recheck-result", "model-result", "model-task",
    "software", "software-pack-manifest", "source", "source-adapter", "workflow",
}

PROV = [{"source_id": "src:a", "evidence_id": "evd:a"}]


def epistemic(status="INFERRED", confidence=0.8, **extra) -> dict:
    base = {"status": status, "confidence": confidence, "provenance": copy.deepcopy(PROV)}
    if status == "INFERRED":
        base["basis"] = "porque sim"
    if status == "VALIDATED":
        base["validation"] = {
            "validated_by": "Pessoa Exemplo", "validated_at": "2026-10-14T10:00:00-03:00",
            "method": "human_review", "review_id": "rev:a",
        }
    base.update(extra)
    return base


def fact(**overrides) -> dict:
    record = {"id": "fct:a", "statement": "x", "epistemic": epistemic()}
    record.update(overrides)
    return record


class SchemaInventory(unittest.TestCase):
    def test_exact_set_of_schemas(self) -> None:
        names = {p.name[: -len(".schema.json")] for p in (REPO / "schemas").glob("*.schema.json")}
        self.assertEqual(names, EXPECTED_SCHEMAS)

    def test_meta_check_has_no_problems(self) -> None:
        self.assertEqual(Registry().check_all(), [])

    def test_ids_are_unique_urns(self) -> None:
        reg = Registry()
        self.assertEqual(len(reg.by_id), len(EXPECTED_SCHEMAS))
        for sid in reg.by_id:
            self.assertRegex(sid, r"^urn:mr-software-intelligence:schema:[a-z-]+:1$")

    def test_twelve_knowledge_concepts_are_covered(self) -> None:
        concepts = {"software", "source", "evidence", "fact", "screen", "interaction", "lookup", "workflow",
                    "business-rule", "contradiction", "human-review", "release-change"}
        self.assertTrue(concepts <= EXPECTED_SCHEMAS)

    def test_every_schema_file_is_valid_json_with_draft(self) -> None:
        for path in (REPO / "schemas").glob("*.schema.json"):
            self.assertEqual(load_json(path)["$schema"], "https://json-schema.org/draft/2020-12/schema", path.name)


class EpistemicRules(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def errors(self, record, schema="fact"):
        return self.v.validate(record, schema)

    def test_baseline_fact_is_valid(self) -> None:
        self.assertEqual(self.errors(fact()), [])

    def test_status_and_confidence_are_separate_required_axes(self) -> None:
        for status in ("OBSERVED", "INFERRED", "VALIDATED"):
            for confidence in (0.05, 0.93):
                record = fact(supported_by=["evd:a"], epistemic=epistemic(status, confidence))
                self.assertEqual(self.errors(record), [], (status, confidence))
        no_conf = fact()
        del no_conf["epistemic"]["confidence"]
        self.assertTrue(self.errors(no_conf))
        no_status = fact()
        del no_status["epistemic"]["status"]
        self.assertTrue(self.errors(no_status))

    def test_confidence_is_a_number_between_zero_and_one(self) -> None:
        for bad in (1.2, -0.1, "high", None, True):
            self.assertTrue(self.errors(fact(epistemic=epistemic(confidence=bad))), bad)

    def test_provenance_is_mandatory_and_non_empty(self) -> None:
        record = fact()
        record["epistemic"]["provenance"] = []
        self.assertTrue(self.errors(record))
        del record["epistemic"]["provenance"]
        self.assertTrue(self.errors(record))

    def test_inferred_requires_basis(self) -> None:
        record = fact()
        del record["epistemic"]["basis"]
        self.assertTrue(self.errors(record))

    def test_validated_requires_validation(self) -> None:
        record = fact(epistemic=epistemic("VALIDATED"))
        del record["epistemic"]["validation"]
        self.assertTrue(self.errors(record))

    def test_validated_human_review_requires_review_id(self) -> None:
        record = fact(epistemic=epistemic("VALIDATED"))
        del record["epistemic"]["validation"]["review_id"]
        self.assertTrue(self.errors(record))
        record["epistemic"]["validation"]["method"] = "runtime_test"
        self.assertEqual(self.errors(record), [])

    def test_observed_and_inferred_cannot_carry_validation(self) -> None:
        for status in ("OBSERVED", "INFERRED"):
            record = fact(supported_by=["evd:a"], epistemic=epistemic(status))
            record["epistemic"]["validation"] = epistemic("VALIDATED")["validation"]
            self.assertTrue(self.errors(record), status)

    def test_quarantined_record_cannot_be_validated(self) -> None:
        quarantine = {"reason": "contradiction", "ref": "ctr:a", "since": "2026-10-14T10:00:00-03:00"}
        ok = fact(epistemic=epistemic(quarantine=quarantine))
        self.assertEqual(self.errors(ok), [])
        bad = fact(epistemic=epistemic("VALIDATED", quarantine=quarantine))
        self.assertTrue(self.errors(bad))

    def test_observed_fact_needs_evidence(self) -> None:
        self.assertTrue(self.errors(fact(epistemic=epistemic("OBSERVED"))))
        self.assertEqual(self.errors(fact(supported_by=["evd:a"], epistemic=epistemic("OBSERVED"))), [])
        self.assertTrue(self.errors(fact(supported_by=[], epistemic=epistemic("OBSERVED"))))

    def test_unknown_properties_are_rejected(self) -> None:
        self.assertTrue(self.errors(fact(surprise=1)))
        record = fact()
        record["epistemic"]["band"] = "high"  # a faixa é derivada, nunca gravada
        self.assertTrue(self.errors(record))

    def test_record_id_prefix_is_enforced_by_schema(self) -> None:
        self.assertTrue(self.errors(fact(id="src:a")))
        self.assertTrue(self.errors(fact(id="fct:UPPER")))


class LookupRules(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def lookup(self, status, ref=None, ep="OBSERVED") -> dict:
        target = {"status": status}
        if ref:
            target["ref"] = ref
        return {
            "id": "lku:a",
            "from": {"screen_id": "scr:a", "element_key": "f"},
            "target": target,
            "epistemic": epistemic(ep),
        }

    def test_unknown_target_cannot_have_a_reference(self) -> None:
        self.assertEqual(self.v.validate(self.lookup("UNKNOWN"), "lookup"), [])
        self.assertTrue(self.v.validate(self.lookup("UNKNOWN", "scr:b"), "lookup"))

    def test_known_and_probable_need_a_reference(self) -> None:
        self.assertTrue(self.v.validate(self.lookup("KNOWN"), "lookup"))
        self.assertTrue(self.v.validate(self.lookup("PROBABLE", ep="INFERRED"), "lookup"))
        self.assertEqual(self.v.validate(self.lookup("KNOWN", "scr:b"), "lookup"), [])

    def test_status_of_the_claim_follows_target_status(self) -> None:
        self.assertTrue(self.v.validate(self.lookup("KNOWN", "scr:b", ep="INFERRED"), "lookup"))
        self.assertTrue(self.v.validate(self.lookup("PROBABLE", "scr:b", ep="OBSERVED"), "lookup"))
        self.assertEqual(self.v.validate(self.lookup("PROBABLE", "scr:b", ep="INFERRED"), "lookup"), [])

    def test_lookup_target_must_be_a_screen(self) -> None:
        self.assertTrue(self.v.validate(self.lookup("KNOWN", "fct:b"), "lookup"))


class InteractionRules(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def interaction(self, tested, status) -> dict:
        return {
            "id": "int:a", "screen_id": "scr:a",
            "trigger": {"element_key": "b", "event": "click"},
            "effect": {"kind": "persists", "description": "x"},
            "runtime_tested": tested,
            "epistemic": epistemic(status),
        }

    def test_untested_interaction_is_never_observed(self) -> None:
        self.assertTrue(self.v.validate(self.interaction(False, "OBSERVED"), "interaction"))
        self.assertEqual(self.v.validate(self.interaction(False, "INFERRED"), "interaction"), [])
        self.assertEqual(self.v.validate(self.interaction(True, "OBSERVED"), "interaction"), [])

    def test_runtime_tested_is_required_and_boolean(self) -> None:
        record = self.interaction(True, "OBSERVED")
        del record["runtime_tested"]
        self.assertTrue(self.v.validate(record, "interaction"))
        record["runtime_tested"] = "yes"
        self.assertTrue(self.v.validate(record, "interaction"))


class ContradictionRules(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def contradiction(self, status="OPEN", **extra) -> dict:
        record = {
            "id": "ctr:a", "topic": "t",
            "positions": [{"ref": "fct:a", "summary": "a"}, {"ref": "fct:b", "summary": "b"}],
            "status": status, "detected_by": {"actor": "ai", "name": "x"},
        }
        record.update(extra)
        return record

    def resolution(self, **extra) -> dict:
        base = {"decided_by": "Pessoa Exemplo", "decided_at": "2026-10-14T10:00:00-03:00", "method": "human-decision",
                "rationale": "r", "history_preserved": True}
        base.update(extra)
        return base

    def test_open_and_resolved_shapes(self) -> None:
        self.assertEqual(self.v.validate(self.contradiction(), "contradiction"), [])
        self.assertEqual(self.v.validate(self.contradiction("RESOLVED", resolution=self.resolution()), "contradiction"), [])

    def test_resolved_needs_resolution_and_open_forbids_it(self) -> None:
        self.assertTrue(self.v.validate(self.contradiction("RESOLVED"), "contradiction"))
        self.assertTrue(self.v.validate(self.contradiction("OPEN", resolution=self.resolution()), "contradiction"))

    def test_history_must_be_preserved(self) -> None:
        bad = self.contradiction("RESOLVED", resolution=self.resolution(history_preserved=False))
        self.assertTrue(self.v.validate(bad, "contradiction"))

    def test_needs_at_least_two_positions(self) -> None:
        bad = self.contradiction()
        bad["positions"] = bad["positions"][:1]
        self.assertTrue(self.v.validate(bad, "contradiction"))


class HumanReviewRules(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def review(self, route, state="OPEN", **extra) -> dict:
        record = {"id": "rev:a", "route": route, "state": state, "subject_refs": ["fct:a"], "question": "q?", "rationale": "r"}
        record.update(extra)
        return record

    attempts = [{"step": "evidence-reuse", "outcome": "nada"}]
    answer = {"actor": "person", "answered_by": "Pessoa Exemplo", "answered_at": "2026-10-14T10:00:00-03:00", "text": "sim"}

    def test_each_route_requires_its_own_fields(self) -> None:
        self.assertTrue(self.v.validate(self.review("AI_RECHECK"), "human-review"))
        self.assertEqual(self.v.validate(self.review("AI_RECHECK", recheck={"plan": "reler"}), "human-review"), [])
        self.assertTrue(self.v.validate(self.review("SOURCE_GAP"), "human-review"))
        ok = self.review("SOURCE_GAP", needed_source={"kind": "runtime", "description": "d"})
        self.assertEqual(self.v.validate(ok, "human-review"), [])

    def test_human_decision_needs_options_and_prior_attempts(self) -> None:
        base = self.review("HUMAN_DECISION", options=["a", "b"], attempted_before=self.attempts)
        self.assertEqual(self.v.validate(base, "human-review"), [])
        for missing in ("options", "attempted_before"):
            bad = {k: v for k, v in base.items() if k != missing}
            self.assertTrue(self.v.validate(bad, "human-review"), missing)
        self.assertTrue(self.v.validate({**base, "options": ["a"]}, "human-review"))
        self.assertTrue(self.v.validate({**base, "attempted_before": []}, "human-review"))

    def test_human_validation_needs_hypothesis_and_prior_attempts(self) -> None:
        base = self.review("HUMAN_VALIDATION", hypothesis_ref="fct:a", attempted_before=self.attempts)
        self.assertEqual(self.v.validate(base, "human-review"), [])
        for missing in ("hypothesis_ref", "attempted_before"):
            bad = {k: v for k, v in base.items() if k != missing}
            self.assertTrue(self.v.validate(bad, "human-review"), missing)

    def test_answered_requires_answer_and_open_forbids_it(self) -> None:
        base = self.review("HUMAN_VALIDATION", hypothesis_ref="fct:a", attempted_before=self.attempts)
        self.assertTrue(self.v.validate({**base, "state": "ANSWERED"}, "human-review"))
        self.assertEqual(self.v.validate({**base, "state": "ANSWERED", "answer": self.answer}, "human-review"), [])
        self.assertTrue(self.v.validate({**base, "answer": self.answer}, "human-review"))

    def test_human_routes_must_be_answered_by_a_person(self) -> None:
        base = self.review("HUMAN_VALIDATION", "ANSWERED", hypothesis_ref="fct:a", attempted_before=self.attempts)
        for actor in ("agent", "tool"):
            self.assertTrue(self.v.validate({**base, "answer": {**self.answer, "actor": actor}}, "human-review"), actor)
        recheck = self.review("AI_RECHECK", "ANSWERED", recheck={"plan": "p"}, answer={**self.answer, "actor": "agent"})
        self.assertEqual(self.v.validate(recheck, "human-review"), [])


class SourceAndManifestRules(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def source(self, **locator) -> dict:
        return {"id": "src:a", "kind": "screenshot", "locator": {"workspace": "pack", "path": "a/b.png", **locator},
                "contains_sensitive_data": None}

    def test_null_sensitivity_is_allowed_but_the_field_is_required(self) -> None:
        self.assertEqual(self.v.validate(self.source(), "source"), [])
        record = self.source()
        del record["contains_sensitive_data"]
        self.assertTrue(self.v.validate(record, "source"))

    def test_absolute_and_parent_paths_are_rejected(self) -> None:
        for bad in ("C:\\x\\y.png", "C:/x/y.png", "/etc/x", "\\\\srv\\x", "../x.png", "a/../x.png", "a\\..\\x.png"):
            self.assertTrue(self.v.validate(self.source(path=bad), "source"), bad)
        for good in ("x.png", "a/b/c.png", "a b/c.png", "a/..x/c.png"):
            self.assertEqual(self.v.validate(self.source(path=good), "source"), [], good)

    def test_sha256_shape(self) -> None:
        self.assertEqual(self.v.validate({**self.source(), "sha256": "a" * 64}, "source"), [])
        for bad in ("A" * 64, "a" * 63, "g" * 64, None):
            self.assertTrue(self.v.validate({**self.source(), "sha256": bad}, "source"), bad)

    def manifest(self) -> dict:
        return load_json(REPO / "software-packs" / "_template" / "manifest.json")

    def test_template_manifest_is_valid(self) -> None:
        self.assertEqual(self.v.validate(self.manifest(), "software-pack-manifest"), [])

    def test_external_canonical_knowledge_forbids_collections(self) -> None:
        manifest = load_json(REPO / "software-packs" / "syscom" / "manifest.json")
        self.assertEqual(self.v.validate(manifest, "software-pack-manifest"), [])
        manifest["collections"] = ["facts"]
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))

    def test_first_validation_case_must_be_index_one(self) -> None:
        manifest = load_json(REPO / "software-packs" / "syscom" / "manifest.json")
        del manifest["validation_case_index"]
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))
        manifest["validation_case_index"] = 2
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))

    def test_workspace_access_must_be_read_only(self) -> None:
        manifest = self.manifest()
        manifest["external_workspace"]["access"] = "read-write"
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))

    def test_embedded_workspace_is_the_pack_itself(self) -> None:
        manifest = self.manifest()
        manifest["external_workspace"]["workspace_id"] = "other"
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))

    def test_integration_requires_reference_file_unless_not_applicable(self) -> None:
        manifest = self.manifest()
        manifest["integration"] = {"status": "reference-only"}
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))

    def test_identity_terms_are_mandatory(self) -> None:
        manifest = self.manifest()
        manifest["isolation"]["leak_terms"]["identity"] = []
        self.assertTrue(self.v.validate(manifest, "software-pack-manifest"))


if __name__ == "__main__":
    unittest.main()
