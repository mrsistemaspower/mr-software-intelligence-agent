"""ModelTask: evidência autorizada, conteúdo preso por hash, identidade determinística e contrato de saída."""
from __future__ import annotations

import base64
import copy
import unittest

from tests.helpers import REPO, load_json
from tests.recheck_helpers import DOC, DOC_REF, IMAGE, evidence
from core import model_task as mt
from core.ai_recheck import OUTPUT_CONTRACT
from core.model_task import (
    EVIDENCE_KEY,
    EvidenceRef,
    ModelContractError,
    ModelTask,
    OutputContract,
    TaskRequirements,
    check_output,
    effective_capabilities,
    evidence_payload,
)
from core.schema_lite import Registry, Validator

CONTRACT = OutputContract("answer", "1", "urn:mr-software-intelligence:schema:ai-recheck-result:1#/$defs/modelOutput")


def make_task(**overrides: object) -> ModelTask:
    refs = overrides.pop("evidence_refs", (evidence(),))
    context = overrides.pop("allowed_context", {"question": "q"})
    fields: dict = dict(
        task_type="probe", objective="Responder a pergunta.", instructions=("Seja breve.",), evidence_refs=refs, allowed_context=context,
        output_contract=CONTRACT, required_capabilities=("structured-output",), max_boundary="LOCAL",
    )
    fields.update(overrides)
    return ModelTask(**fields)  # type: ignore[arg-type]


def context_with(item: EvidenceRef, data: bytes) -> dict:
    return {"question": "q", EVIDENCE_KEY: {item.ref: evidence_payload(item, data)}}


def code_of(exc: ModelContractError) -> str:
    return exc.code


class ConstantsAgreeWithTheSchemas(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.task_schema = load_json(REPO / "schemas" / "model-task.schema.json")
        cls.common = load_json(REPO / "schemas" / "common.schema.json")

    def test_source_kinds_are_the_foundation_vocabulary(self) -> None:
        self.assertEqual(list(mt.SOURCE_KINDS), self.common["$defs"]["sourceKind"]["enum"])

    def test_capabilities_reuse_the_foundation_names(self) -> None:
        self.assertEqual(list(mt.CAPABILITIES), self.task_schema["$defs"]["capability"]["enum"])
        shared = {"vision", "structured-output", "long-context"}
        self.assertLessEqual(shared, set(self.common["$defs"]["capability"]["enum"]))
        self.assertLessEqual(shared, set(mt.CAPABILITIES))

    def test_boundaries_modalities_and_risk_levels(self) -> None:
        defs = self.task_schema["$defs"]
        self.assertEqual(list(mt.BOUNDARIES), defs["boundary"]["enum"])
        self.assertEqual(list(mt.MODALITIES), defs["evidenceRef"]["properties"]["modality"]["enum"])
        self.assertEqual(list(mt.RISK_LEVELS), defs["riskLevel"]["enum"])
        self.assertEqual(list(mt.ENCODINGS), defs["evidencePayload"]["properties"]["encoding"]["enum"])

    def test_boundaries_are_ordered_by_exposure(self) -> None:
        self.assertEqual(mt.BOUNDARIES, ("LOCAL", "PRIVATE", "EXTERNAL"))
        self.assertLess(mt.boundary_rank("LOCAL"), mt.boundary_rank("PRIVATE"))
        self.assertLess(mt.boundary_rank("PRIVATE"), mt.boundary_rank("EXTERNAL"))

    def test_patterns_in_code_match_the_schema(self) -> None:
        defs = self.task_schema["$defs"]
        self.assertEqual(mt.REF_PATTERN, defs["ref"]["pattern"])
        self.assertEqual(mt.NAME_PATTERN, defs["name"]["pattern"])
        self.assertEqual(mt.VERSION_PATTERN, defs["version"]["pattern"])
        self.assertEqual(mt.MEDIA_TYPE_PATTERN, defs["evidenceRef"]["properties"]["media_type"]["pattern"])


class EvidenceRefs(unittest.TestCase):
    def test_of_pins_the_content_by_hash_and_size(self) -> None:
        item = EvidenceRef.of("a-1", b"abc", "documentation")
        self.assertEqual(item.sha256, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(item.byte_length, 3)
        self.assertIsNone(item.contains_sensitive_data)
        self.assertEqual(item.media_type, "text/plain")

    def test_sensitivity_is_tri_state_and_only_false_proves_clear(self) -> None:
        self.assertTrue(EvidenceRef.of("a", b"x", "documentation", contains_sensitive_data=False).sensitivity_verified_clear)
        self.assertFalse(EvidenceRef.of("a", b"x", "documentation", contains_sensitive_data=True).sensitivity_verified_clear)
        self.assertFalse(EvidenceRef.of("a", b"x", "documentation", contains_sensitive_data=None).sensitivity_verified_clear)

    def test_non_boolean_sensitivity_is_refused(self) -> None:
        for bad in (0, 1, "false", "no"):
            with self.subTest(bad), self.assertRaises(ModelContractError):
                EvidenceRef.of("a", b"x", "documentation", contains_sensitive_data=bad)  # type: ignore[arg-type]

    def test_invalid_fields_are_refused_with_stable_codes(self) -> None:
        base = dict(ref="a", sha256="0" * 64, source_type="documentation", modality="text", byte_length=1, contains_sensitive_data=False)
        cases = {
            "INVALID_REF": dict(ref="C:\\temp\\x"),
            "INVALID_HASH": dict(sha256="ABC"),
            "INVALID_CHOICE": dict(source_type="chat"),
            "INVALID_LENGTH": dict(byte_length=-1),
            "INVALID_MEDIA_TYPE": dict(media_type="not a media type"),
        }
        for code, change in cases.items():
            with self.subTest(code), self.assertRaises(ModelContractError) as caught:
                EvidenceRef(**{**base, **change})  # type: ignore[arg-type]
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ModelContractError) as caught:
            EvidenceRef(**{**base, "modality": "audio"})  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "INVALID_CHOICE")

    def test_a_locator_is_a_logical_name_never_a_local_path(self) -> None:
        for bad in ("C:\\Users\\x\\file.txt", "C:/work/file.txt", "\\\\server\\share\\a"):
            with self.subTest(bad), self.assertRaises(ModelContractError) as caught:
                EvidenceRef.of("a", b"x", "documentation", locator=bad)
            self.assertEqual(caught.exception.code, "LOCAL_PATH")
        self.assertEqual(EvidenceRef.of("a", b"x", "documentation", locator="input:manual").locator, "input:manual")

    def test_round_trip_and_strict_keys(self) -> None:
        item = evidence()
        self.assertEqual(EvidenceRef.from_dict(item.to_dict()), item)
        data = item.to_dict()
        data["extra"] = 1
        with self.assertRaises(ModelContractError):
            EvidenceRef.from_dict(data)
        del data["extra"], data["sha256"]
        with self.assertRaises(ModelContractError):
            EvidenceRef.from_dict(data)

    def test_without_locator(self) -> None:
        stripped = evidence().without_locator()
        self.assertIsNone(stripped.locator)
        self.assertNotIn("locator", stripped.to_dict())

    def test_an_image_implies_vision(self) -> None:
        self.assertEqual(evidence("img", IMAGE, "screenshot", "image").implied_capabilities(), ("vision",))
        self.assertEqual(evidence().implied_capabilities(), ())


class TaskIdentity(unittest.TestCase):
    def test_same_meaningful_input_same_id_and_the_id_is_an_artifact_identity(self) -> None:
        first, second = make_task(), make_task()
        self.assertEqual(first.task_id, second.task_id)
        self.assertRegex(first.task_id, r"^art:model\.task:sha256:[0-9a-f]{64}$")

    def test_the_attempt_is_metadata_and_never_in_the_identity(self) -> None:
        self.assertEqual(make_task(attempt=1).task_id, make_task(attempt=7).task_id)

    def test_the_locator_is_not_part_of_the_task(self) -> None:
        one = make_task(evidence_refs=(evidence(locator="input:one"),))
        two = make_task(evidence_refs=(evidence(locator="input:two"),))
        self.assertEqual(one.task_id, two.task_id)
        self.assertTrue(all(item.locator is None for item in one.evidence_refs))

    def test_evidence_order_does_not_matter(self) -> None:
        a, b = evidence("a-1", b"one"), evidence("b-1", b"two")
        self.assertEqual(make_task(evidence_refs=(a, b)).task_id, make_task(evidence_refs=(b, a)).task_id)

    def test_every_meaningful_field_changes_the_id(self) -> None:
        base = make_task().task_id
        variants = {
            "objective": make_task(objective="Outra pergunta."),
            "instructions": make_task(instructions=("Seja breve.", "Cite as refs.")),
            "task_type": make_task(task_type="other"),
            "context": make_task(allowed_context={"question": "outra"}),
            "evidence": make_task(evidence_refs=(evidence(data=DOC + b"x"),)),
            "evidence ref": make_task(evidence_refs=(evidence("doc-2"),)),
            "boundary": make_task(max_boundary="PRIVATE"),
            "risk": make_task(risk_level="HIGH"),
            "capabilities": make_task(required_capabilities=("structured-output", "long-context")),
            "sensitivity": make_task(evidence_refs=(evidence(sensitive=None),)),
            "contract": make_task(output_contract=OutputContract("answer", "2", CONTRACT.schema_ref)),
        }
        for name, task in variants.items():
            with self.subTest(name):
                self.assertNotEqual(task.task_id, base)

    def test_a_wrong_task_id_is_refused(self) -> None:
        with self.assertRaises(ModelContractError) as caught:
            make_task(task_id="art:model.task:sha256:" + "0" * 64)
        self.assertEqual(caught.exception.code, "ID_MISMATCH")

    def test_the_identity_does_not_depend_on_the_embedded_content_but_on_its_hash(self) -> None:
        item = evidence()
        plain = make_task(evidence_refs=(item,), allowed_context=context_with(item, DOC))
        self.assertEqual(plain.task_id, make_task(evidence_refs=(item,)).task_id)


class EvidenceDisciplineInTheTask(unittest.TestCase):
    def test_content_must_match_the_authorized_hash(self) -> None:
        item = evidence()
        with self.assertRaises(ModelContractError) as caught:
            make_task(evidence_refs=(item,), allowed_context=context_with(item, DOC + b" changed"))
        self.assertEqual(caught.exception.code, "EVIDENCE_HASH")

    def test_content_of_an_unauthorized_evidence_cannot_ride_in_the_context(self) -> None:
        item, other = evidence(), evidence("secret-1", b"not authorized")
        context = {EVIDENCE_KEY: {item.ref: evidence_payload(item, DOC), other.ref: evidence_payload(other, b"not authorized")}}
        with self.assertRaises(ModelContractError) as caught:
            make_task(evidence_refs=(item,), allowed_context=context)
        self.assertEqual(caught.exception.code, "UNAUTHORIZED_EVIDENCE")

    def test_text_uses_utf8_and_binary_uses_base64(self) -> None:
        text = evidence_payload(evidence(), DOC)
        self.assertEqual((text["encoding"], text["content"]), ("utf-8", DOC.decode("utf-8")))
        image = evidence("img", IMAGE, "screenshot", "image", media_type="image/png")
        payload = evidence_payload(image, IMAGE)
        self.assertEqual(payload["encoding"], "base64")
        self.assertEqual(base64.b64decode(payload["content"]), IMAGE)
        task = make_task(evidence_refs=(image,), allowed_context={EVIDENCE_KEY: {"img": payload}}, required_capabilities=("structured-output", "vision"))
        self.assertEqual(task.evidence_refs[0].media_type, "image/png")

    def test_the_wrong_encoding_for_the_modality_is_refused(self) -> None:
        item = evidence()
        wrong = {"media_type": "text/plain", "encoding": "base64", "content": base64.b64encode(DOC).decode("ascii")}
        with self.assertRaises(ModelContractError) as caught:
            make_task(evidence_refs=(item,), allowed_context={EVIDENCE_KEY: {item.ref: wrong}})
        self.assertEqual(caught.exception.code, "EVIDENCE_ENCODING")

    def test_text_that_is_not_utf8_cannot_become_a_payload(self) -> None:
        item = evidence("bin-1", b"\xff\xfe\x00bad", "documentation", "text")
        with self.assertRaises(ModelContractError) as caught:
            evidence_payload(item, b"\xff\xfe\x00bad")
        self.assertEqual(caught.exception.code, "EVIDENCE_UNDECODABLE")

    def test_an_image_without_vision_is_an_inconsistent_task(self) -> None:
        image = evidence("img", IMAGE, "screenshot", "image")
        with self.assertRaises(ModelContractError) as caught:
            make_task(evidence_refs=(image,), required_capabilities=("structured-output",))
        self.assertEqual(caught.exception.code, "MISSING_IMPLIED_CAPABILITY")

    def test_duplicate_refs_are_refused(self) -> None:
        with self.assertRaises(ModelContractError) as caught:
            make_task(evidence_refs=(evidence(), evidence()))
        self.assertEqual(caught.exception.code, "DUPLICATE_EVIDENCE")

    def test_the_context_is_canonical_json_and_portable(self) -> None:
        with self.assertRaises(ModelContractError):
            make_task(allowed_context={"bad": {1, 2}})
        with self.assertRaises(ModelContractError) as caught:
            make_task(allowed_context={"note": "veja C:\\Users\\alguem\\arquivo.txt"})
        self.assertEqual(caught.exception.code, "LOCAL_PATH")

    def test_raw_evidence_content_may_mention_a_path_because_it_is_data_of_the_world(self) -> None:
        data = b"Install dir: C:\\Program Files\\Tool\\bin\n"
        item = evidence("cfg-1", data, "documentation")
        task = make_task(evidence_refs=(item,), allowed_context=context_with(item, data))
        self.assertIn("C:\\Program Files", task.allowed_context[EVIDENCE_KEY]["cfg-1"]["content"])

    def test_the_task_does_not_alias_the_callers_context(self) -> None:
        context = {"question": "q"}
        task = make_task(allowed_context=context)
        context["question"] = "changed"
        self.assertEqual(task.allowed_context["question"], "q")


class TaskSerialization(unittest.TestCase):
    validator = Validator(Registry())

    def test_to_dict_validates_against_the_schema_and_round_trips(self) -> None:
        item = evidence()
        task = make_task(evidence_refs=(item,), allowed_context=context_with(item, DOC))
        data = task.to_dict()
        self.assertEqual(self.validator.validate(data, "model-task"), [])
        self.assertEqual(ModelTask.from_dict(data), task)

    def test_a_tampered_dictionary_is_refused(self) -> None:
        item = evidence()
        data = make_task(evidence_refs=(item,), allowed_context=context_with(item, DOC)).to_dict()
        for mutate in (
            lambda d: d.__setitem__("objective", "outro objetivo"),
            lambda d: d["allowed_context"][EVIDENCE_KEY][DOC_REF].__setitem__("content", "adulterado"),
            lambda d: d.__setitem__("extra", 1),
            lambda d: d.__setitem__("max_boundary", "SOMEWHERE"),
        ):
            copy_ = copy.deepcopy(data)
            mutate(copy_)
            with self.subTest(), self.assertRaises(ModelContractError):
                ModelTask.from_dict(copy_)

    def test_the_wire_form_carries_no_locator_and_no_provider_prompt(self) -> None:
        data = make_task().to_dict()
        self.assertTrue(all("locator" not in item for item in data["evidence_refs"]))
        self.assertLess(len(" ".join(data["instructions"])), 1000)


class OutputContractChecks(unittest.TestCase):
    def test_contract_validation_rejects_bad_fields(self) -> None:
        for kwargs in (
            dict(name="Bad Name", version="1", schema_ref=CONTRACT.schema_ref),
            dict(name="ok", version="", schema_ref=CONTRACT.schema_ref),
            dict(name="ok", version="1", schema_ref="https://example.invalid/schema"),
        ):
            with self.subTest(kwargs), self.assertRaises(ModelContractError):
                OutputContract(**kwargs)

    def test_check_output_uses_the_schema_of_the_contract(self) -> None:
        good = {"status": "INCONCLUSIVE", "rationale": "r", "evidence_used": ["a"], "unresolved": ["c1"]}
        self.assertEqual(check_output(OUTPUT_CONTRACT, good), [])
        self.assertTrue(check_output(OUTPUT_CONTRACT, {"status": "INCONCLUSIVE"}))
        self.assertTrue(check_output(OUTPUT_CONTRACT, ["not", "an", "object"]))
        self.assertTrue(check_output(OUTPUT_CONTRACT, {**good, "validated_by": "Fulano"}))

    def test_an_unknown_contract_is_a_violation_not_a_crash(self) -> None:
        unknown = OutputContract("x", "1", "urn:mr-software-intelligence:schema:does-not-exist:1")
        self.assertTrue(check_output(unknown, {}))

    def test_violations_are_capped(self) -> None:
        self.assertLessEqual(len(check_output(OUTPUT_CONTRACT, {"findings": [{}] * 100})), mt.MAX_VIOLATIONS)


class Requirements(unittest.TestCase):
    def test_effective_capabilities_add_what_the_modality_imposes(self) -> None:
        image = evidence("img", IMAGE, "screenshot", "image")
        self.assertEqual(effective_capabilities(["structured-output"], [image]), ("structured-output", "vision"))
        self.assertEqual(effective_capabilities([], []), ())

    def test_requirements_validate(self) -> None:
        with self.assertRaises(ModelContractError):
            TaskRequirements(("telepathy",), "LOCAL")
        with self.assertRaises(ModelContractError):
            TaskRequirements(("vision",), "MOON")
        self.assertEqual(TaskRequirements(("vision", "vision"), "LOCAL").required_capabilities, ("vision",))

    def test_capabilities_must_be_a_list_not_a_string(self) -> None:
        with self.assertRaises(ModelContractError):
            mt.capabilities_tuple("vision")


if __name__ == "__main__":
    unittest.main()
