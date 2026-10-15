"""Checkpoint: identidade determinística, validade só se tudo confere, adulteração recusada, gravação atômica."""
from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempDirCase
from core.checkpoint import (
    CORRUPT,
    CheckpointError,
    CheckpointStore,
    OutputRecord,
    StageCheckpoint,
    check_checkpoint,
    execution_identity,
    output_logical_path,
    outputs_digest,
    parameters_digest,
)
from core.hashing import hash_bytes, hash_canonical_json
from core.paths import find_local_paths
from core.stage_graph import RetryPolicy, StageSpec

SPEC = StageSpec("analyze", depends_on=("discover",), input_contracts=("source",), output_contracts=("report", "summary"))
SNAP = "snap:sha256:" + "1" * 64
SNAP2 = "snap:sha256:" + "2" * 64
INPUTS = {"source": "sha256:" + "a" * 64}
UPSTREAM = {"discover": "sha256:" + "b" * 64}
PARAMS = parameters_digest({"depth": 2})


class Base(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = self.tmp / "out"
        self.contents = {"report": b"report body", "summary": b"summary body"}
        for name, data in self.contents.items():
            path = self.out / output_logical_path("analyze", name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def records(self) -> list:
        return [OutputRecord.of("analyze", name, hash_bytes(data)) for name, data in self.contents.items()]

    def make(self, **over) -> StageCheckpoint:
        args = dict(workflow_id="flow", software_id="synthetic-one", spec=SPEC, snapshot_id=SNAP, parameters_digest=PARAMS,
                    inputs=INPUTS, upstream=UPSTREAM, outputs=self.records(), attempt=1)
        args.update(over)
        return StageCheckpoint.create(**args)

    def check(self, checkpoint: StageCheckpoint, **over) -> list:
        args = dict(workflow_id="flow", software_id="synthetic-one", spec=SPEC, snapshot_id=SNAP, parameters_digest=PARAMS,
                    inputs=INPUTS, upstream=UPSTREAM, output_root=self.out)
        args.update(over)
        return [(m.code, m.detail) for m in check_checkpoint(checkpoint, **args)]


class Determinism(Base):
    def test_same_facts_same_ids(self) -> None:
        a, b = self.make(), self.make()
        self.assertEqual((a.checkpoint_id, a.execution_identity), (b.checkpoint_id, b.execution_identity))
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_ids_have_the_documented_shape(self) -> None:
        cp = self.make()
        self.assertRegex(cp.checkpoint_id, r"^ckpt:sha256:[0-9a-f]{64}$")
        self.assertRegex(cp.execution_identity, r"^art:stage\.execution:sha256:[0-9a-f]{64}$")
        self.assertRegex(cp.outputs_digest, r"^sha256:[0-9a-f]{64}$")

    def test_attempt_number_is_metadata_not_identity(self) -> None:
        self.assertEqual(self.make(attempt=1).checkpoint_id, self.make(attempt=7).checkpoint_id)
        self.assertEqual(self.make(attempt=1).execution_identity, self.make(attempt=7).execution_identity)

    def test_output_order_is_irrelevant(self) -> None:
        reversed_outputs = list(reversed(self.records()))
        self.assertEqual(self.make().checkpoint_id, self.make(outputs=reversed_outputs).checkpoint_id)

    def test_identity_is_independent_of_the_location_on_disk(self) -> None:
        other = self.tmp / "elsewhere" / "deeper"
        for name, data in self.contents.items():
            path = other / output_logical_path("analyze", name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        a = self.make()
        self.assertEqual(self.check(a, output_root=other), [])
        self.assertEqual(self.make().to_dict(), a.to_dict())

    def test_every_identity_ingredient_changes_the_identity(self) -> None:
        base = self.make()
        variants = {
            "snapshot": self.make(snapshot_id=SNAP2),
            "no snapshot": self.make(snapshot_id=None),
            "parameters": self.make(parameters_digest=parameters_digest({"depth": 3})),
            "input": self.make(inputs={"source": "sha256:" + "c" * 64}),
            "upstream": self.make(upstream={"discover": "sha256:" + "d" * 64}),
            "software": self.make(software_id="synthetic-two"),
            "no software": self.make(software_id=None),
            "workflow": self.make(workflow_id="other-flow"),
            "version": self.make(spec=StageSpec("analyze", version="2", depends_on=("discover",), input_contracts=("source",), output_contracts=("report", "summary"))),
        }
        for label, other in variants.items():
            with self.subTest(label):
                self.assertNotEqual(other.execution_identity, base.execution_identity)
                self.assertNotEqual(other.checkpoint_id, base.checkpoint_id)

    def test_output_content_changes_the_checkpoint_id_but_not_the_execution_identity(self) -> None:
        base = self.make()
        changed = [OutputRecord.of("analyze", "report", hash_bytes(b"different")), OutputRecord.of("analyze", "summary", hash_bytes(b"summary body"))]
        other = self.make(outputs=changed)
        self.assertEqual(other.execution_identity, base.execution_identity)
        self.assertNotEqual(other.checkpoint_id, base.checkpoint_id)

    def test_retry_policy_does_not_change_the_identity(self) -> None:
        relaxed = StageSpec("analyze", depends_on=("discover",), input_contracts=("source",), output_contracts=("report", "summary"), retry=RetryPolicy(9))
        self.assertEqual(self.make(spec=relaxed).checkpoint_id, self.make().checkpoint_id)

    def test_execution_identity_function_matches_the_checkpoint(self) -> None:
        direct = execution_identity(workflow_id="flow", software_id="synthetic-one", spec=SPEC, snapshot_id=SNAP,
                                    parameters_digest=PARAMS, inputs=INPUTS, upstream=UPSTREAM)
        self.assertEqual(direct.artifact_id, self.make().execution_identity)

    def test_no_local_path_or_timestamp_in_the_checkpoint(self) -> None:
        data = self.make().to_dict()
        self.assertEqual(find_local_paths(data), [])
        self.assertNotIn(str(self.tmp), json.dumps(data))
        self.assertFalse({"created_at", "timestamp", "hostname", "pid"} & set(data))

    def test_outputs_digest_matches_an_independent_computation(self) -> None:
        records = sorted(self.records(), key=lambda item: item.name)
        expected = "sha256:" + hash_canonical_json([item.to_dict() for item in records]).digest
        self.assertEqual(outputs_digest(self.records()), expected)


class Validity(Base):
    def test_a_fresh_checkpoint_is_valid(self) -> None:
        self.assertEqual(self.check(self.make()), [])

    def test_changed_definition_version(self) -> None:
        newer = StageSpec("analyze", version="2", depends_on=("discover",), input_contracts=("source",), output_contracts=("report", "summary"))
        self.assertEqual(self.check(self.make(), spec=newer), [("DEFINITION_CHANGED", None)])

    def test_changed_definition_contracts(self) -> None:
        other = StageSpec("analyze", depends_on=("discover",), input_contracts=("source",), output_contracts=("report", "summary", "extra"))
        codes = [code for code, _ in self.check(self.make(), spec=other)]
        self.assertIn("DEFINITION_CHANGED", codes)
        self.assertIn("OUTPUT_SET_CHANGED", codes)

    def test_wrong_snapshot_is_rejected(self) -> None:
        self.assertEqual(self.check(self.make(), snapshot_id=SNAP2), [("SNAPSHOT_CHANGED", None)])
        self.assertEqual(self.check(self.make(), snapshot_id=None), [("SNAPSHOT_CHANGED", None)])
        self.assertEqual(self.check(self.make(snapshot_id=None), snapshot_id=SNAP), [("SNAPSHOT_CHANGED", None)])

    def test_changed_configuration(self) -> None:
        self.assertEqual(self.check(self.make(), parameters_digest=parameters_digest({"depth": 3})), [("CONFIG_CHANGED", None)])

    def test_changed_input(self) -> None:
        self.assertEqual(self.check(self.make(), inputs={"source": "sha256:" + "f" * 64}), [("INPUT_CHANGED", "source")])

    def test_unavailable_input_counts_as_changed(self) -> None:
        self.assertEqual(self.check(self.make(), inputs={"source": None}), [("INPUT_CHANGED", "source")])
        self.assertEqual(self.check(self.make(), inputs={}), [("INPUT_CHANGED", "source")])

    def test_changed_upstream(self) -> None:
        self.assertEqual(self.check(self.make(), upstream={"discover": "sha256:" + "e" * 64}), [("UPSTREAM_CHANGED", "discover")])
        self.assertEqual(self.check(self.make(), upstream={}), [("UPSTREAM_CHANGED", "discover")])

    def test_mutated_output(self) -> None:
        (self.out / output_logical_path("analyze", "report")).write_bytes(b"tampered")
        self.assertEqual(self.check(self.make()), [("OUTPUT_MUTATED", "report")])

    def test_same_size_mutation_is_still_caught(self) -> None:
        (self.out / output_logical_path("analyze", "report")).write_bytes(b"REPORT BODY")
        self.assertEqual(self.check(self.make()), [("OUTPUT_MUTATED", "report")])

    def test_missing_output(self) -> None:
        (self.out / output_logical_path("analyze", "summary")).unlink()
        self.assertEqual(self.check(self.make()), [("OUTPUT_MISSING", "summary")])

    def test_output_replaced_by_a_directory_is_unreadable(self) -> None:
        target = self.out / output_logical_path("analyze", "summary")
        target.unlink()
        target.mkdir()
        self.assertEqual(self.check(self.make()), [("OUTPUT_UNREADABLE", "summary")])

    def test_existence_alone_never_suffices(self) -> None:
        for name in self.contents:
            (self.out / output_logical_path("analyze", name)).write_bytes(b"")
        self.assertEqual({detail for _, detail in self.check(self.make())}, {"report", "summary"})

    def test_workflow_or_software_change(self) -> None:
        self.assertEqual(self.check(self.make(), workflow_id="other-flow"), [("WORKFLOW_CHANGED", None)])
        self.assertEqual(self.check(self.make(), software_id="synthetic-two"), [("WORKFLOW_CHANGED", None)])

    def test_several_reasons_are_all_reported_in_priority_order(self) -> None:
        (self.out / output_logical_path("analyze", "report")).write_bytes(b"x")
        found = self.check(self.make(), snapshot_id=SNAP2, inputs={"source": "sha256:" + "0" * 64})
        self.assertEqual([code for code, _ in found], ["SNAPSHOT_CHANGED", "INPUT_CHANGED", "OUTPUT_MUTATED"])

    def test_a_stage_with_no_outputs_is_valid_on_identity_alone(self) -> None:
        bare = StageSpec("gate", input_contracts=("source",))
        cp = StageCheckpoint.create(workflow_id="flow", software_id=None, spec=bare, snapshot_id=None, parameters_digest=PARAMS,
                                    inputs=INPUTS, upstream={}, outputs=[], attempt=1)
        found = check_checkpoint(cp, workflow_id="flow", software_id=None, spec=bare, snapshot_id=None, parameters_digest=PARAMS,
                                 inputs=INPUTS, upstream={}, output_root=self.out)
        self.assertEqual(found, ())


class Tampering(Base):
    def tampered(self, mutate) -> dict:
        data = copy.deepcopy(self.make().to_dict())
        mutate(data)
        return data

    def assert_rejected(self, mutate) -> None:
        with self.assertRaises(CheckpointError):
            StageCheckpoint.from_dict(self.tampered(mutate))

    def test_untouched_document_loads(self) -> None:
        self.assertEqual(StageCheckpoint.from_dict(self.make().to_dict()), self.make())

    def test_each_tampered_field_is_rejected(self) -> None:
        cases = {
            "output hash": lambda d: d["outputs"][0].update(sha256="0" * 64),
            "output size": lambda d: d["outputs"][0].update(byte_length=1),
            "output path": lambda d: d["outputs"][0].update(path="outputs/other/report"),
            "outputs digest": lambda d: d.update(outputs_digest="sha256:" + "0" * 64),
            "identity": lambda d: d.update(execution_identity="art:stage.execution:sha256:" + "0" * 64),
            "checkpoint id": lambda d: d.update(checkpoint_id="ckpt:sha256:" + "0" * 64),
            "input": lambda d: d["inputs"].update(source="sha256:" + "0" * 64),
            "upstream": lambda d: d["upstream"].update(discover="sha256:" + "0" * 64),
            "snapshot": lambda d: d.update(knowledge_snapshot_id=SNAP2),
            "parameters digest": lambda d: d.update(parameters_digest="sha256:" + "0" * 64),
            "definition digest": lambda d: d.update(definition_digest="sha256:" + "0" * 64),
            "stage version": lambda d: d.update(stage_version="9"),
            "workflow": lambda d: d.update(workflow_id="other-flow"),
            "software": lambda d: d.update(software_id="synthetic-two"),
            "stage id": lambda d: d.update(stage_id="other"),
            "dropped output": lambda d: d["outputs"].pop(),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assert_rejected(mutate)

    def test_schema_violations_are_rejected(self) -> None:
        cases = {
            "extra field": lambda d: d.update(surprise=1),
            "wrong status": lambda d: d.update(status="FAILED"),
            "missing field": lambda d: d.pop("attempt"),
            "bad attempt": lambda d: d.update(attempt=0),
            "wrong marker": lambda d: d.update(schema="x"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assert_rejected(mutate)

    def test_corruption_codes(self) -> None:
        with self.assertRaises(CheckpointError) as caught:
            StageCheckpoint.from_dict(self.tampered(lambda d: d["outputs"][0].update(sha256="0" * 64)))
        self.assertEqual(caught.exception.code, CORRUPT)

    def test_non_document_is_rejected(self) -> None:
        for value in (None, [], "x", 3):
            with self.subTest(value), self.assertRaises(CheckpointError):
                StageCheckpoint.from_dict(value)


class Store(Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = CheckpointStore(self.tmp / "state" / "checkpoints")

    def test_missing_checkpoint_is_none(self) -> None:
        self.assertIsNone(self.store.load("analyze"))

    def test_save_load_round_trip(self) -> None:
        cp = self.make()
        self.store.save(cp)
        self.assertEqual(self.store.load("analyze"), cp)

    def test_saving_the_same_checkpoint_does_not_rewrite_the_file(self) -> None:
        cp = self.make()
        self.assertEqual(self.store.save(cp).status, "CREATED")
        self.assertEqual(self.store.save(cp).status, "NO_CHANGE")

    def test_garbage_and_truncated_files_are_corrupt_not_silently_ignored(self) -> None:
        self.store.save(self.make())
        path = self.store.path("analyze")
        good = path.read_bytes()
        for label, payload in (("garbage", b"\x00\xffnot json"), ("truncated", good[: len(good) // 2]), ("empty", b""), ("bom", b"\xef\xbb\xbf" + good), ("array", b"[]")):
            path.write_bytes(payload)
            with self.subTest(label), self.assertRaises(CheckpointError) as caught:
                self.store.load("analyze")
            self.assertEqual(caught.exception.code, CORRUPT)

    def test_duplicate_keys_are_refused(self) -> None:
        self.store.save(self.make())
        path = self.store.path("analyze")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"attempt": 1', '"attempt": 1,\n  "attempt": 2'), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.store.load("analyze")

    def test_a_checkpoint_filed_under_another_stage_is_refused(self) -> None:
        self.store.save(self.make())
        os.replace(self.store.path("analyze"), self.store.path("other"))
        with self.assertRaises(CheckpointError):
            self.store.load("other")

    def test_partial_write_leaves_the_previous_checkpoint_intact(self) -> None:
        first = self.make()
        self.store.save(first)
        second = self.make(attempt=2, outputs=[OutputRecord.of("analyze", "report", hash_bytes(b"new")), OutputRecord.of("analyze", "summary", hash_bytes(b"summary body"))])
        with mock.patch("core.atomic.os.replace", side_effect=OSError("queda simulada")):
            with self.assertRaises(OSError):
                self.store.save(second)
        self.assertEqual(self.store.load("analyze"), first)
        self.assertEqual([p.name for p in self.store.path("analyze").parent.iterdir()], ["analyze.json"], "sem temporário abandonado")

    def test_failed_first_write_leaves_nothing_behind(self) -> None:
        with mock.patch("core.atomic.os.replace", side_effect=OSError("queda simulada")):
            with self.assertRaises(OSError):
                self.store.save(self.make())
        self.assertIsNone(self.store.load("analyze"))
        self.assertEqual(list(self.store.path("analyze").parent.iterdir()), [])

    def test_delete(self) -> None:
        self.store.save(self.make())
        self.assertTrue(self.store.delete("analyze"))
        self.assertFalse(self.store.delete("analyze"))
        self.assertIsNone(self.store.load("analyze"))

    def test_invalid_stage_ids_never_reach_the_file_system(self) -> None:
        for bad in ("../x", "a/b", "A", "", "a\\b"):
            with self.subTest(bad), self.assertRaises(CheckpointError):
                self.store.path(bad)


class OutputPaths(unittest.TestCase):
    def test_canonical_output_path(self) -> None:
        self.assertEqual(output_logical_path("analyze", "report.json"), "outputs/analyze/report.json")

    def test_unsafe_names_are_rejected(self) -> None:
        for stage, name in (("../x", "r"), ("a", "../r"), ("a", "r/s"), ("A", "r"), ("a", "")):
            with self.subTest((stage, name)), self.assertRaises(CheckpointError):
                output_logical_path(stage, name)

    def test_output_record_validation(self) -> None:
        good = OutputRecord("r", "outputs/a/r", "0" * 64, 0)
        self.assertEqual(OutputRecord.from_dict(good.to_dict()), good)
        for kwargs in ({"sha256": "XYZ"}, {"byte_length": -1}, {"path": "/abs"}, {"path": "a\\b"}, {"name": "BAD"}):
            with self.subTest(kwargs), self.assertRaises(CheckpointError):
                OutputRecord(**{"name": "r", "path": "outputs/a/r", "sha256": "0" * 64, "byte_length": 0, **kwargs})


if __name__ == "__main__":
    unittest.main()
