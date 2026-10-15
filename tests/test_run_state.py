"""Estado da execução: identidade determinística, invariantes ao carregar e gravação atômica."""
from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from tests.helpers import TempDirCase
from core.paths import find_local_paths
from core.protection import ProtectionRecord, Violation
from core.run_state import (
    CORRUPT,
    MAX_VIOLATIONS,
    RunState,
    RunStateError,
    RunStatus,
    compute_input_identity,
    compute_run_id,
    load_run_state,
    save_run_state,
)
from core.stage_state import Cause, FailureInfo, StageRecord, StageState, UPSTREAM_FAILED, UPSTREAM_INVALIDATED

DIGEST = "sha256:" + "a" * 64
SNAP = "snap:sha256:" + "1" * 64
CKPT = "ckpt:sha256:" + "c" * 64
IDENT = "art:stage.execution:sha256:" + "d" * 64


def make(stages=None, status=RunStatus.RUNNING, current=None, **over) -> RunState:
    stages = stages if stages is not None else (StageRecord("a"), StageRecord("b"))
    args = dict(
        run_id=compute_run_id("flow", "synthetic-one", "1", "default"), workflow_id="flow", workflow_version="1", run_key="default",
        graph_digest=DIGEST, config_digest=DIGEST, input_identity=compute_input_identity(DIGEST, DIGEST, SNAP, {"x": DIGEST}),
        status=status, stages=tuple(stages), software_id="synthetic-one", knowledge_snapshot_id=SNAP, current_stage=current,
    )
    args.update(over)
    return RunState(**args)


class RunIdentity(unittest.TestCase):
    def test_run_id_is_deterministic(self) -> None:
        self.assertEqual(compute_run_id("flow", "s", "1", "k"), compute_run_id("flow", "s", "1", "k"))
        self.assertRegex(compute_run_id("flow", None, "1", "default"), r"^run:sha256:[0-9a-f]{64}$")

    def test_each_identity_component_matters(self) -> None:
        base = compute_run_id("flow", "s", "1", "k")
        for other in (compute_run_id("flow2", "s", "1", "k"), compute_run_id("flow", "t", "1", "k"), compute_run_id("flow", None, "1", "k"),
                      compute_run_id("flow", "s", "2", "k"), compute_run_id("flow", "s", "1", "k2")):
            self.assertNotEqual(other, base)

    def test_run_id_matches_an_independent_computation(self) -> None:
        import hashlib

        payload = {"identity_version": 1, "kind": "workflow-run", "run_key": "k", "software_id": "s", "workflow_id": "flow", "workflow_version": "1"}
        expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(compute_run_id("flow", "s", "1", "k"), "run:sha256:" + expected)

    def test_snapshot_and_inputs_are_not_part_of_the_run_identity_but_of_the_input_identity(self) -> None:
        self.assertNotEqual(compute_input_identity(DIGEST, DIGEST, SNAP, {"x": DIGEST}), compute_input_identity(DIGEST, DIGEST, None, {"x": DIGEST}))
        self.assertNotEqual(compute_input_identity(DIGEST, DIGEST, SNAP, {"x": DIGEST}), compute_input_identity(DIGEST, DIGEST, SNAP, {"x": "sha256:" + "b" * 64}))
        self.assertNotEqual(compute_input_identity(DIGEST, DIGEST, SNAP, {}), compute_input_identity(DIGEST, "sha256:" + "b" * 64, SNAP, {}))
        self.assertEqual(compute_input_identity(DIGEST, DIGEST, SNAP, {"a": DIGEST, "b": DIGEST}), compute_input_identity(DIGEST, DIGEST, SNAP, {"b": DIGEST, "a": DIGEST}))

    def test_no_clock_or_machine_data_in_the_serialized_state(self) -> None:
        text = json.dumps(make().to_dict())
        for word in ("hostname", "pid", "timestamp", "created_at", "updated_at"):
            self.assertNotIn(word, text)
        self.assertEqual(find_local_paths(make().to_dict()), [])


class Invariants(unittest.TestCase):
    def test_round_trip_with_every_stage_state(self) -> None:
        stages = (
            StageRecord("a", StageState.COMPLETED, 1, checkpoint_id=CKPT, last_identity=IDENT),
            StageRecord("b", StageState.FAILED, 2, failure=FailureInfo("b", "HANDLER_ERROR", "falhou", 2)),
            StageRecord("c", StageState.BLOCKED, 0, cause=Cause(UPSTREAM_FAILED, "b")),
            StageRecord("d", StageState.INVALIDATED, 0, cause=Cause(UPSTREAM_INVALIDATED, "a")),
            StageRecord("e"),
        )
        state = make(stages, RunStatus.FAILED, started_at="2030-01-02T03:04:05Z", resume_count=2,
                     protection=(ProtectionRecord("src", "READ_ONLY", "EACH_STAGE", SNAP, 4),),
                     violations=(Violation("src", "READ_ONLY", "MUTATED", "a.txt"),), violation_total=1, environment={"probe": {"at_start": {"x": 1}}})
        again = RunState.from_dict(copy.deepcopy(state.to_dict()))
        self.assertEqual(again, state)
        self.assertEqual(again.to_dict(), state.to_dict())

    def test_status_must_agree_with_stages(self) -> None:
        running = StageRecord("a", StageState.RUNNING, 1)
        cases = [
            ("pending with progress", lambda: make((StageRecord("a", StageState.COMPLETED, 1, checkpoint_id=CKPT, last_identity=IDENT), StageRecord("b")), RunStatus.PENDING)),
            ("completed with pending", lambda: make((StageRecord("a"), StageRecord("b")), RunStatus.COMPLETED)),
            ("completed with running", lambda: make((running,), RunStatus.COMPLETED, "a")),
            ("failed without failure", lambda: make((StageRecord("a"),), RunStatus.FAILED)),
            ("failed with running", lambda: make((running, StageRecord("b", StageState.FAILED, 1, failure=FailureInfo("b", "HANDLER_ERROR", "x", 1))), RunStatus.FAILED, "a")),
        ]
        for label, build in cases:
            with self.subTest(label), self.assertRaises(RunStateError):
                build()

    def test_completed_run_may_keep_optional_failures_and_blocked_stages(self) -> None:
        stages = (
            StageRecord("a", StageState.COMPLETED, 1, checkpoint_id=CKPT, last_identity=IDENT),
            StageRecord("b", StageState.FAILED, 1, failure=FailureInfo("b", "HANDLER_ERROR", "x", 1)),
            StageRecord("c", StageState.BLOCKED, 0, cause=Cause(UPSTREAM_FAILED, "b")),
        )
        self.assertIs(make(stages, RunStatus.COMPLETED).status, RunStatus.COMPLETED)

    def test_running_stage_must_match_current_stage(self) -> None:
        running = StageRecord("a", StageState.RUNNING, 1)
        make((running, StageRecord("b")), RunStatus.RUNNING, "a")
        with self.assertRaises(RunStateError):
            make((running, StageRecord("b")), RunStatus.RUNNING, None)
        with self.assertRaises(RunStateError):
            make((running, StageRecord("b")), RunStatus.RUNNING, "b")
        with self.assertRaises(RunStateError):
            make((StageRecord("a"), StageRecord("b")), RunStatus.RUNNING, "a")

    def test_two_running_stages_are_impossible(self) -> None:
        with self.assertRaises(RunStateError):
            make((StageRecord("a", StageState.RUNNING, 1), StageRecord("b", StageState.RUNNING, 1)), RunStatus.RUNNING, "a")

    def test_duplicate_or_empty_stage_lists_are_rejected(self) -> None:
        with self.assertRaises(RunStateError):
            make((StageRecord("a"), StageRecord("a")))
        with self.assertRaises(RunStateError):
            make(())

    def test_run_id_must_match_the_workflow_identity(self) -> None:
        with self.assertRaises(RunStateError) as caught:
            make(run_id=compute_run_id("other", "synthetic-one", "1", "default"))
        self.assertEqual(caught.exception.code, CORRUPT)

    def test_field_validation(self) -> None:
        for over in ({"graph_digest": "x"}, {"config_digest": "x"}, {"input_identity": "x"}, {"knowledge_snapshot_id": "x"},
                     {"software_id": "Bad"}, {"workflow_version": ""}, {"resume_count": -1}, {"started_at": "yesterday"},
                     {"violation_total": 5, "violations": (Violation("src", "READ_ONLY", "MUTATED", "a"),) * 6},
                     {"violation_total": 0, "violations": (Violation("src", "READ_ONLY", "MUTATED", "a"),)}):
            with self.subTest(over), self.assertRaises(RunStateError):
                make(**over)

    def test_violation_cap_is_part_of_the_contract(self) -> None:
        many = tuple(Violation("src", "READ_ONLY", "MUTATED", f"f{i}.txt") for i in range(MAX_VIOLATIONS + 1))
        with self.assertRaises(RunStateError):
            make(violations=many, violation_total=len(many))

    def test_environment_must_be_canonical_json_without_local_paths(self) -> None:
        with self.assertRaises(RunStateError):
            make(environment={"x": float("nan")})
        with self.assertRaises(RunStateError):
            make(environment={"x": "C:\\Users\\someone"})

    def test_with_stage_keeps_current_stage_consistent(self) -> None:
        state = make()
        running = state.with_stage(state.stage("a").transition(StageState.RUNNING))
        self.assertEqual(running.current_stage, "a")
        failed = running.with_stage(running.stage("a").transition(StageState.FAILED, failure=FailureInfo("a", "HANDLER_ERROR", "x", 1)))
        self.assertIsNone(failed.current_stage)
        with self.assertRaises(KeyError):
            state.stage("ghost")


class Contract(unittest.TestCase):
    def test_schema_violations_are_rejected(self) -> None:
        good = make().to_dict()
        cases = {
            "extra": lambda d: d.update(surprise=1),
            "status": lambda d: d.update(status="DONE"),
            "no stages": lambda d: d.update(stages=[]),
            "stage state": lambda d: d["stages"][0].update(state="WAITING"),
            "marker": lambda d: d.update(schema="x"),
            "missing started": lambda d: d.pop("started"),
            "too many violations": lambda d: d.update(violations=[{"root": "src", "protection": "READ_ONLY", "kind": "MUTATED", "path": "a"}] * 51),
        }
        for label, mutate in cases.items():
            data = copy.deepcopy(good)
            mutate(data)
            with self.subTest(label), self.assertRaises(RunStateError):
                RunState.from_dict(data)

    def test_tampering_with_identity_fields_is_caught(self) -> None:
        data = make().to_dict()
        data["workflow_version"] = "2"
        with self.assertRaises(RunStateError):
            RunState.from_dict(data)

    def test_stage_level_violations_surface_as_run_state_errors(self) -> None:
        data = make().to_dict()
        data["stages"][0] = {"stage_id": "a", "state": "COMPLETED", "attempts": 1}
        with self.assertRaises(RunStateError):
            RunState.from_dict(data)


class Persistence(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.tmp / "state" / "run-state.json"

    def test_save_and_load(self) -> None:
        state = make()
        self.assertEqual(save_run_state(self.path, state).status, "CREATED")
        self.assertEqual(load_run_state(self.path), state)
        self.assertEqual(save_run_state(self.path, state).status, "NO_CHANGE")

    def test_missing_file_is_reported_as_missing_not_corrupt(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_run_state(self.path)

    def test_corrupt_files_raise_and_are_left_untouched(self) -> None:
        save_run_state(self.path, make())
        good = self.path.read_bytes()
        for label, payload in (("garbage", b"\x00\xff\x01"), ("truncated", good[: len(good) // 3]), ("empty", b""), ("array", b"[]"), ("bom", b"\xef\xbb\xbf" + good)):
            self.path.write_bytes(payload)
            with self.subTest(label), self.assertRaises(RunStateError) as caught:
                load_run_state(self.path)
            self.assertEqual(caught.exception.code, CORRUPT)
            self.assertEqual(self.path.read_bytes(), payload, "um estado ilegível não é reescrito em silêncio")

    def test_duplicate_keys_in_the_file_are_refused(self) -> None:
        save_run_state(self.path, make())
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text.replace('"status": "RUNNING"', '"status": "RUNNING",\n  "status": "COMPLETED"'), encoding="utf-8")
        with self.assertRaises(RunStateError):
            load_run_state(self.path)

    def test_a_crash_during_the_save_keeps_the_previous_state(self) -> None:
        first = make()
        save_run_state(self.path, first)
        second = first.with_changes(resume_count=1)
        with mock.patch("core.atomic.os.replace", side_effect=OSError("queda simulada")):
            with self.assertRaises(OSError):
                save_run_state(self.path, second)
        self.assertEqual(load_run_state(self.path), first)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["run-state.json"])

    def test_an_interrupted_write_with_a_leftover_temporary_does_not_confuse_loading(self) -> None:
        first = make()
        save_run_state(self.path, first)
        (self.path.parent / ".run-state.json.deadbeef.tmp").write_bytes(b'{"half": ')
        self.assertEqual(load_run_state(self.path), first)

    def test_serialization_error_before_touching_the_disk(self) -> None:
        state = make()
        bad = state.with_changes(environment={"x": 1})
        object.__setattr__(bad, "environment", {"x": float("nan")})
        with self.assertRaises(Exception):
            save_run_state(self.path, bad)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists() and any(self.path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
