"""Configuração de execução: portátil, validada antes de rodar e com hash estável."""
from __future__ import annotations

import copy
import json
import unittest

from core.protection import Protection, RootPolicy
from core.run_config import ConfigError, InputBinding, InterruptionPolicy, RunConfig
from core.stage_graph import StageGraph, StageSpec

RO, MU = Protection.READ_ONLY, Protection.MUTABLE_OUTPUT


def graph() -> StageGraph:
    return StageGraph([
        StageSpec("first", input_contracts=("source",), output_contracts=("listing",)),
        StageSpec("second", depends_on=("first",), output_contracts=("report",)),
    ])


def build(**over) -> RunConfig:
    args = dict(
        workflow_id="flow", workflow_version="1", graph=graph(),
        roots=(RootPolicy("src", RO), RootPolicy("work", MU)), state_root="work", output_root="work",
        inputs={"source": InputBinding("src", "data/source.txt")}, stage_parameters={"first": {"depth": 2}},
        software_id="synthetic-one",
    )
    args.update(over)
    return RunConfig(**args)


class Validation(unittest.TestCase):
    def test_valid_configuration(self) -> None:
        config = build()
        self.assertEqual(config.parameters_of("first"), {"depth": 2})
        self.assertEqual(config.parameters_of("second"), {})
        self.assertIs(config.interruption_policy, InterruptionPolicy.REQUEUE)
        self.assertEqual(config.run_key, "default")

    def test_state_and_output_roots_must_be_declared_and_mutable(self) -> None:
        for over in ({"state_root": "ghost"}, {"output_root": "ghost"}, {"state_root": "src"}, {"output_root": "src"}):
            with self.subTest(over), self.assertRaises(ConfigError):
                build(**over)

    def test_every_declared_input_needs_a_binding(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            build(inputs={})
        self.assertIn("source", str(caught.exception))

    def test_a_binding_nobody_declares_is_a_typo_and_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build(inputs={"source": InputBinding("src", "a.txt"), "sorce": InputBinding("src", "b.txt")})

    def test_inputs_cannot_come_from_a_mutable_root(self) -> None:
        with self.assertRaises(ConfigError):
            build(inputs={"source": InputBinding("work", "a.txt")})

    def test_inputs_must_use_a_declared_root(self) -> None:
        with self.assertRaises(ConfigError):
            build(inputs={"source": InputBinding("nowhere", "a.txt")})

    def test_binding_validation(self) -> None:
        for root, path in (("BAD", "a.txt"), ("src", "../a.txt"), ("src", "/abs.txt"), ("src", "a\\b.txt"), ("src", "C:/x.txt"), ("src", "")):
            with self.subTest((root, path)), self.assertRaises(ConfigError):
                InputBinding(root, path)

    def test_parameters_for_unknown_stages_are_a_typo(self) -> None:
        with self.assertRaises(ConfigError):
            build(stage_parameters={"frist": {}})

    def test_parameters_must_be_json_objects_that_fit_the_canonical_contract(self) -> None:
        for value in ([], "x", 3, {"nan": float("nan")}, {"set": {1}}, {1: "non-string key"}):
            with self.subTest(repr(value)), self.assertRaises(ConfigError):
                build(stage_parameters={"first": value})

    def test_local_paths_are_refused_everywhere_in_the_configuration(self) -> None:
        with self.assertRaises(ConfigError):
            build(stage_parameters={"first": {"where": "C:\\Users\\someone\\data"}})
        with self.assertRaises(ConfigError):
            build(stage_parameters={"first": {"where": "/home/someone/data"}})

    def test_identifiers_are_validated(self) -> None:
        for over in ({"workflow_id": "BAD"}, {"workflow_version": ""}, {"software_id": "Bad Id"}, {"run_key": "A"}, {"interruption_policy": "SHRUG"}):
            with self.subTest(over), self.assertRaises(ConfigError):
                build(**over)

    def test_duplicate_root_names_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build(roots=(RootPolicy("src", RO), RootPolicy("src", RO), RootPolicy("work", MU)))

    def test_graph_must_be_a_stage_graph(self) -> None:
        with self.assertRaises(ConfigError):
            build(graph={"stages": []})  # type: ignore[arg-type]

    def test_software_id_is_optional(self) -> None:
        self.assertIsNone(build(software_id=None).software_id)


class Hashing(unittest.TestCase):
    def test_digest_is_stable_and_order_independent(self) -> None:
        a = build(roots=(RootPolicy("src", RO), RootPolicy("work", MU)), stage_parameters={"first": {"a": 1, "b": 2}})
        b = build(roots=(RootPolicy("work", MU), RootPolicy("src", RO)), stage_parameters={"first": {"b": 2, "a": 1}})
        self.assertEqual(a.digest(), b.digest())
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_every_meaningful_change_changes_the_digest(self) -> None:
        base = build().digest()
        changes = [
            {"workflow_version": "2"}, {"software_id": "synthetic-two"}, {"software_id": None}, {"run_key": "other"},
            {"stage_parameters": {"first": {"depth": 3}}}, {"inputs": {"source": InputBinding("src", "data/other.txt")}},
            {"interruption_policy": InterruptionPolicy.FAIL}, {"roots": (RootPolicy("src", Protection.FROZEN), RootPolicy("work", MU))},
            {"roots": (RootPolicy("src", RO, None, ("data",)), RootPolicy("work", MU))},
        ]
        for over in changes:
            with self.subTest(list(over)):
                self.assertNotEqual(build(**over).digest(), base)

    def test_digest_has_no_machine_dependence(self) -> None:
        self.assertNotIn(":\\", json.dumps(build().to_dict()))


class Contract(unittest.TestCase):
    def test_round_trip(self) -> None:
        config = build(interruption_policy=InterruptionPolicy.FAIL, run_key="batch-1")
        again = RunConfig.from_dict(copy.deepcopy(config.to_dict()))
        self.assertEqual(again.to_dict(), config.to_dict())
        self.assertEqual(again.digest(), config.digest())

    def test_defaults_apply_when_optional_fields_are_missing(self) -> None:
        data = build().to_dict()
        for key in ("run_key", "inputs", "stage_parameters", "interruption_policy", "software_id"):
            data.pop(key, None)
        data["graph"] = StageGraph([StageSpec("only")]).to_dict()
        config = RunConfig.from_dict(data)
        self.assertEqual((config.run_key, dict(config.inputs), config.software_id), ("default", {}, None))

    def test_documents_outside_the_contract_are_rejected(self) -> None:
        good = build().to_dict()
        cases = {
            "extra field": lambda d: d.update(surprise=1),
            "missing roots": lambda d: d.pop("roots"),
            "unknown protection": lambda d: d["roots"][0].update(protection="WRITABLE"),
            "unknown policy": lambda d: d.update(interruption_policy="SHRUG"),
            "bad binding": lambda d: d["inputs"]["source"].pop("path"),
            "wrong marker": lambda d: d.update(schema="x"),
        }
        for label, mutate in cases.items():
            data = copy.deepcopy(good)
            mutate(data)
            with self.subTest(label), self.assertRaises(ConfigError):
                RunConfig.from_dict(data)

    def test_semantic_errors_inside_a_valid_document_are_rejected(self) -> None:
        data = build().to_dict()
        data["state_root"] = "src"
        with self.assertRaises(ConfigError):
            RunConfig.from_dict(data)
        data = build().to_dict()
        data["graph"]["stages"][1]["depends_on"] = ["second"]
        with self.assertRaises(ConfigError):
            RunConfig.from_dict(data)

    def test_frozen_root_that_switches_verification_off_is_rejected_on_load(self) -> None:
        data = build().to_dict()
        data["roots"][1] = {"name": "src", "protection": "FROZEN", "verify": "NONE"}
        with self.assertRaises(ConfigError):
            RunConfig.from_dict(data)


if __name__ == "__main__":
    unittest.main()
