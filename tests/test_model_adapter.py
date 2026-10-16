"""Fronteira do adaptador de modelo: descritor, resposta, elegibilidade (capacidade e fronteira de dados) e a base."""
from __future__ import annotations

import unittest

from tests.helpers import REPO, load_json
from tests.recheck_helpers import DOC, DOC_REF, IMAGE, evidence, resolved
from adapters.models.base import BaseModelAdapter
from adapters.models.scripted import RawReturn, RuleBasedModelAdapter, ScriptedModelAdapter
from core import model_adapter as ma
from core.model_adapter import (
    AdapterDescriptor,
    ModelAdapter,
    ModelFailure,
    ModelIdentity,
    ModelResult,
    check_eligibility,
    config_fingerprint,
)
from core.model_task import EVIDENCE_KEY, ModelContractError, ModelTask, OutputContract, TaskRequirements, evidence_payload
from core.schema_lite import Registry, Validator

TASK_ID = "art:model.task:sha256:" + "a" * 64
CONTRACT = OutputContract("answer", "1", "urn:mr-software-intelligence:schema:ai-recheck-result:1#/$defs/modelOutput")


def descriptor(**overrides: object) -> AdapterDescriptor:
    fields: dict = dict(adapter_id="synthetic-a", adapter_version="1", capabilities=("structured-output",), data_boundary="LOCAL")
    fields.update(overrides)
    return AdapterDescriptor(**fields)  # type: ignore[arg-type]


def requirements(evidence_items: tuple = (), **overrides: object) -> TaskRequirements:
    fields: dict = dict(required_capabilities=("structured-output",), max_boundary="LOCAL", evidence=evidence_items)
    fields.update(overrides)
    return TaskRequirements(**fields)  # type: ignore[arg-type]


def task() -> ModelTask:
    item = evidence()
    return ModelTask(
        task_type="probe", objective="x", instructions=(), evidence_refs=(item,),
        allowed_context={EVIDENCE_KEY: {item.ref: evidence_payload(item, DOC)}}, output_contract=CONTRACT,
        required_capabilities=("structured-output",), max_boundary="LOCAL",
    )


class Descriptors(unittest.TestCase):
    def test_minimal_descriptor_needs_no_network_no_model_no_token_accounting(self) -> None:
        item = descriptor()
        self.assertIsNone(item.model_id)
        self.assertFalse(item.deterministic)
        self.assertEqual(item.capabilities, ("structured-output",))

    def test_capabilities_are_normalized_and_closed(self) -> None:
        self.assertEqual(descriptor(capabilities=("vision", "structured-output", "vision")).capabilities, ("structured-output", "vision"))
        with self.assertRaises(ModelContractError):
            descriptor(capabilities=("telepathy",))

    def test_invalid_identity_fields_are_refused(self) -> None:
        for change in (
            dict(adapter_id="Bad Id"), dict(adapter_version=""), dict(data_boundary="CLOUDY"), dict(deterministic="yes"),
            dict(model_id="has space"), dict(config_fingerprint="abc"), dict(profile_id="Bad_Profile"),
        ):
            with self.subTest(change), self.assertRaises(ModelContractError):
                descriptor(**change)

    def test_identity_dict_is_portable_stable_and_round_trips(self) -> None:
        item = descriptor(model_id="model-x:7b", deterministic=True, config_fingerprint=config_fingerprint({"temperature": 0}), profile_id="local")
        data = item.identity_dict()
        self.assertEqual(data, descriptor(model_id="model-x:7b", deterministic=True, config_fingerprint=data["config_fingerprint"], profile_id="local").identity_dict())
        self.assertEqual(AdapterDescriptor.from_dict(data), item)
        self.assertEqual(Validator(Registry()).validate(data, {"$ref": "urn:mr-software-intelligence:schema:model-result:1#/$defs/adapterIdentity"}), [])

    def test_the_fingerprint_is_the_hash_of_the_configuration_and_never_the_configuration(self) -> None:
        one, two = config_fingerprint({"a": 1, "b": 2}), config_fingerprint({"b": 2, "a": 1})
        self.assertEqual(one, two)
        self.assertNotEqual(one, config_fingerprint({"a": 1, "b": 3}))
        self.assertRegex(one, r"^sha256:[0-9a-f]{64}$")

    def test_from_dict_is_strict(self) -> None:
        data = descriptor().identity_dict()
        for mutate in (lambda d: d.update(extra=1), lambda d: d.pop("data_boundary")):
            broken = dict(data)
            mutate(broken)
            with self.subTest(), self.assertRaises(ModelContractError):
                AdapterDescriptor.from_dict(broken)

    def test_profile_id_can_point_at_a_real_environment_profile(self) -> None:
        profiles = sorted((REPO / "adapters" / "llm").glob("*.profile.json"))
        self.assertGreaterEqual(len(profiles), 4)
        for path in profiles:
            profile = load_json(path)
            with self.subTest(profile["id"]):
                self.assertEqual(descriptor(profile_id=profile["id"]).profile_id, profile["id"])


class ModelResults(unittest.TestCase):
    identity = ModelIdentity("synthetic-a", "model-1")

    def test_success_xor_failure(self) -> None:
        ok = ModelResult.success(TASK_ID, self.identity, {"a": 1})
        self.assertTrue(ok.ok)
        failed = ModelResult.failed(TASK_ID, self.identity, ModelFailure("TIMEOUT", "demorou", True))
        self.assertFalse(failed.ok)
        with self.assertRaises(ModelContractError):
            ModelResult(TASK_ID, self.identity)
        with self.assertRaises(ModelContractError):
            ModelResult(TASK_ID, self.identity, output={"a": 1}, failure=ModelFailure("ERROR", "x"))

    def test_the_output_must_be_canonical_json_and_is_copied(self) -> None:
        source = {"a": [1, 2]}
        result = ModelResult.success(TASK_ID, self.identity, source)
        source["a"].append(3)
        self.assertEqual(result.output, {"a": [1, 2]})
        for bad in ({"a": {1, 2}}, {"a": float("nan")}, {"a": b"bytes"}):
            with self.subTest(bad), self.assertRaises(ModelContractError):
                ModelResult.success(TASK_ID, self.identity, bad)

    def test_usage_is_optional_and_validated(self) -> None:
        self.assertIsNone(ModelResult.success(TASK_ID, self.identity, {}).usage)
        self.assertEqual(ModelResult.success(TASK_ID, self.identity, {}, {"output_tokens": 3, "input_tokens": 9}).usage, {"input_tokens": 9, "output_tokens": 3})
        self.assertEqual(ModelResult.success(TASK_ID, self.identity, {}, {"input_tokens": 9}).usage, {"input_tokens": 9})
        for bad in ({"cost": 1}, {"input_tokens": -1}, {"input_tokens": 1.5}, {"input_tokens": True}):
            with self.subTest(bad), self.assertRaises(ModelContractError):
                ModelResult.success(TASK_ID, self.identity, {}, bad)

    def test_failures_are_closed_and_carry_no_local_path(self) -> None:
        self.assertEqual(ma.FAILURE_KINDS, ("TIMEOUT", "UNAVAILABLE", "REFUSED", "ERROR"))
        with self.assertRaises(ModelContractError):
            ModelFailure("MELTDOWN", "x")
        with self.assertRaises(ModelContractError) as caught:
            ModelFailure("ERROR", "falhou em C:\\Users\\alguem\\segredo.txt")
        self.assertEqual(caught.exception.code, "LOCAL_PATH")
        with self.assertRaises(ModelContractError):
            ModelFailure("ERROR", "x" * 401)

    def test_the_response_hash_covers_the_canonical_output(self) -> None:
        one = ModelResult.success(TASK_ID, self.identity, {"a": 1, "b": 2})
        two = ModelResult.success(TASK_ID, self.identity, {"b": 2, "a": 1})
        self.assertEqual(one.response_sha256(), two.response_sha256())
        self.assertIsNone(ModelResult.failed(TASK_ID, self.identity, ModelFailure("ERROR", "x")).response_sha256())

    def test_wire_form_validates_and_round_trips(self) -> None:
        validator = Validator(Registry())
        for result in (
            ModelResult.success(TASK_ID, self.identity, {"a": 1}, {"input_tokens": 1}),
            ModelResult.failed(TASK_ID, ModelIdentity("synthetic-a"), ModelFailure("REFUSED", "recusou")),
        ):
            data = result.to_dict()
            self.assertEqual(validator.validate(data, "model-result"), [])
            self.assertEqual(ModelResult.from_dict(data), result)

    def test_the_wire_envelope_rejects_inconsistent_documents(self) -> None:
        good = ModelResult.success(TASK_ID, self.identity, {"a": 1}).to_dict()
        both = dict(good, failure={"kind": "ERROR", "message": "x", "retryable": False})
        neither = {key: value for key, value in good.items() if key != "output"}
        flipped = dict(good, ok=False)
        for bad in (both, neither, flipped, dict(good, task_id="x"), dict(good, extra=1)):
            with self.subTest(), self.assertRaises(ModelContractError):
                ModelResult.from_dict(bad)


class Eligibility(unittest.TestCase):
    def test_a_capable_local_adapter_is_eligible(self) -> None:
        self.assertEqual(check_eligibility(descriptor(), requirements((evidence(),))), ())

    def test_missing_capability_is_named(self) -> None:
        found = check_eligibility(descriptor(), requirements(required_capabilities=("structured-output", "vision", "long-context")))
        self.assertEqual([(item.code, item.detail) for item in found], [(ma.MISSING_CAPABILITY, "long-context"), (ma.MISSING_CAPABILITY, "vision")])

    def test_boundary_beyond_the_allowed_is_refused_and_below_is_fine(self) -> None:
        self.assertEqual([item.code for item in check_eligibility(descriptor(data_boundary="PRIVATE"), requirements())], [ma.BOUNDARY_EXCEEDED])
        self.assertEqual(check_eligibility(descriptor(data_boundary="LOCAL"), requirements(max_boundary="EXTERNAL")), ())
        self.assertEqual(check_eligibility(descriptor(data_boundary="PRIVATE"), requirements(max_boundary="PRIVATE")), ())

    def test_sensitive_or_unchecked_evidence_never_reaches_an_external_adapter(self) -> None:
        external = descriptor(data_boundary="EXTERNAL")
        clear = evidence("clear", b"x", sensitive=False)
        for label, sensitive in (("sensitive", True), ("unchecked", None)):
            with self.subTest(label):
                found = check_eligibility(external, requirements((clear, evidence(label, b"y", sensitive=sensitive)), max_boundary="EXTERNAL"))
                self.assertEqual([item.code for item in found], [ma.SENSITIVE_EVIDENCE_EXTERNAL])
                self.assertIn(label, found[0].detail)
        self.assertEqual(check_eligibility(external, requirements((clear,), max_boundary="EXTERNAL")), ())

    def test_sensitive_evidence_may_go_to_a_private_adapter(self) -> None:
        sensitive = evidence("s", b"x", sensitive=True)
        self.assertEqual(check_eligibility(descriptor(data_boundary="PRIVATE"), requirements((sensitive,), max_boundary="PRIVATE")), ())

    def test_every_reason_is_reported_in_stable_order(self) -> None:
        found = check_eligibility(
            descriptor(capabilities=(), data_boundary="EXTERNAL"),
            requirements((evidence(sensitive=None),), required_capabilities=("vision", "structured-output"), max_boundary="LOCAL"),
        )
        self.assertEqual([item.code for item in found], sorted([ma.BOUNDARY_EXCEEDED, ma.MISSING_CAPABILITY, ma.MISSING_CAPABILITY, ma.SENSITIVE_EVIDENCE_EXTERNAL]))
        self.assertEqual(found, check_eligibility(
            descriptor(capabilities=(), data_boundary="EXTERNAL"),
            requirements((evidence(sensitive=None),), required_capabilities=("structured-output", "vision"), max_boundary="LOCAL"),
        ))

    def test_wrong_argument_types_are_a_usage_error(self) -> None:
        with self.assertRaises(TypeError):
            check_eligibility("adapter", requirements())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            check_eligibility(descriptor(), "requirements")  # type: ignore[arg-type]

    def test_the_check_does_not_call_the_adapter(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        check_eligibility(adapter.descriptor, requirements())
        self.assertEqual(adapter.calls, 0)


class TheBaseAdapter(unittest.TestCase):
    def test_synthetic_adapters_satisfy_the_protocol_without_network(self) -> None:
        for adapter in (ScriptedModelAdapter([]), RuleBasedModelAdapter(lambda t: {})):
            self.assertIsInstance(adapter.descriptor, AdapterDescriptor)
            self.assertTrue(callable(adapter.execute))
            typed: ModelAdapter = adapter  # noqa: F841 - o contrato é estrutural

    def test_a_mapping_becomes_a_successful_result_for_the_right_task(self) -> None:
        adapter = ScriptedModelAdapter([{"x": 1}], model_id="m-1")
        result = adapter.execute(task())
        self.assertTrue(result.ok)
        self.assertEqual((result.task_id, result.model.model_id, result.output), (task().task_id, "m-1", {"x": 1}))

    def test_timeouts_and_connection_errors_become_retryable_failures_without_leaking_the_message(self) -> None:
        for exc, kind in ((TimeoutError("host 10.0.0.9 token=abc"), "TIMEOUT"), (ConnectionError("C:\\secret"), "UNAVAILABLE")):
            result = ScriptedModelAdapter([exc]).execute(task())
            with self.subTest(kind):
                self.assertFalse(result.ok)
                self.assertEqual((result.failure.kind, result.failure.retryable), (kind, True))
                self.assertNotIn("abc", result.failure.message)
                self.assertNotIn("secret", result.failure.message)

    def test_unexpected_exceptions_are_not_swallowed_by_the_base(self) -> None:
        with self.assertRaises(RuntimeError):
            ScriptedModelAdapter([RuntimeError("bug")]).execute(task())

    def test_a_model_failure_step_is_returned_as_a_failed_result(self) -> None:
        result = ScriptedModelAdapter([ModelFailure("REFUSED", "recusou")]).execute(task())
        self.assertEqual((result.ok, result.failure.kind), (False, "REFUSED"))

    def test_the_raw_return_escape_hatch_bypasses_the_contract_for_negative_tests(self) -> None:
        self.assertEqual(ScriptedModelAdapter([RawReturn("lixo")]).execute(task()), "lixo")

    def test_an_exhausted_script_is_a_failure_not_a_crash(self) -> None:
        adapter = ScriptedModelAdapter([])
        self.assertEqual(adapter.execute(task()).failure.kind, "ERROR")
        self.assertEqual(adapter.calls, 1)

    def test_a_callable_step_sees_the_task(self) -> None:
        adapter = ScriptedModelAdapter([lambda t: {"saw": t.task_id}])
        self.assertEqual(adapter.execute(task()).output, {"saw": task().task_id})

    def test_execute_requires_a_model_task(self) -> None:
        with self.assertRaises(TypeError):
            ScriptedModelAdapter([{}]).execute({"not": "a task"})  # type: ignore[arg-type]

    def test_the_base_validates_its_descriptor_and_is_abstract(self) -> None:
        with self.assertRaises(TypeError):
            BaseModelAdapter(descriptor())  # type: ignore[abstract]

        class Bad(BaseModelAdapter):
            def _invoke(self, task: ModelTask) -> object:  # type: ignore[override]
                return 42

        with self.assertRaises(TypeError):
            Bad("not a descriptor")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Bad(descriptor()).execute(task())

    def test_usage_is_attached_only_when_the_adapter_knows_it(self) -> None:
        self.assertIsNone(ScriptedModelAdapter([{}]).execute(task()).usage)
        self.assertEqual(ScriptedModelAdapter([{}], usage={"input_tokens": 5}).execute(task()).usage, {"input_tokens": 5})

    def test_the_image_task_is_representable_for_a_vision_adapter(self) -> None:
        image = evidence("img", IMAGE, "screenshot", "image")
        built = ModelTask(
            task_type="probe", objective="x", instructions=(), evidence_refs=(image,),
            allowed_context={EVIDENCE_KEY: {"img": evidence_payload(image, IMAGE)}}, output_contract=CONTRACT,
            required_capabilities=("structured-output", "vision"), max_boundary="LOCAL",
        )
        self.assertEqual(check_eligibility(descriptor(capabilities=("structured-output", "vision")), built.requirements()), ())
        self.assertTrue(check_eligibility(descriptor(), built.requirements()))
        self.assertEqual(DOC_REF, "doc-1")


if __name__ == "__main__":
    unittest.main()
