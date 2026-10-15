"""Máquina de estados do estágio: transições, invariantes, contagem de tentativas e falha portátil."""
from __future__ import annotations

import itertools
import unittest

from core.serialization import canonical_dumps
from core.stage_state import (
    CAUSE_CODES,
    FAILURE_CATEGORIES,
    HANDLER_ERROR,
    INTERRUPTED,
    TRANSITIONS,
    UPSTREAM_FAILED,
    UPSTREAM_INVALIDATED,
    Cause,
    FailureInfo,
    StageRecord,
    StageState,
    TransitionError,
    check_transition,
    failure_from_exception,
    is_runnable,
    sanitize_message,
)

CKPT = "ckpt:sha256:" + "a" * 64
IDENT = "art:stage.execution:sha256:" + "b" * 64
P, R, C, F, B, I = (StageState.PENDING, StageState.RUNNING, StageState.COMPLETED, StageState.FAILED, StageState.BLOCKED, StageState.INVALIDATED)

DOCUMENTED = {
    (P, R), (P, B),
    (R, C), (R, F), (R, P),
    (C, I),
    (F, P), (F, B),
    (B, P),
    (I, R), (I, B),
}


def failure(stage_id: str = "s", attempt: int = 1) -> FailureInfo:
    return FailureInfo(stage_id, HANDLER_ERROR, "falhou", attempt)


def record_in(state: StageState, attempts: int = 1, stage_id: str = "s") -> StageRecord:
    if state is P:
        return StageRecord(stage_id)
    if state is R:
        return StageRecord(stage_id, R, attempts)
    if state is C:
        return StageRecord(stage_id, C, attempts, checkpoint_id=CKPT, last_identity=IDENT)
    if state is F:
        return StageRecord(stage_id, F, attempts, failure=failure(stage_id, attempts))
    if state is B:
        return StageRecord(stage_id, B, attempts, cause=Cause(UPSTREAM_FAILED, "dep"))
    return StageRecord(stage_id, I, 0, cause=Cause(UPSTREAM_INVALIDATED, "dep"))


class TransitionTable(unittest.TestCase):
    def test_exactly_the_documented_transitions_are_allowed(self) -> None:
        for current, target in itertools.product(StageState, StageState):
            allowed = (current, target) in DOCUMENTED
            with self.subTest(f"{current.value}->{target.value}"):
                if allowed:
                    check_transition(current, target, recovery=True)
                else:
                    with self.assertRaises(TransitionError):
                        check_transition(current, target, recovery=True)

    def test_published_table_matches_the_documented_one(self) -> None:
        published = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
        self.assertEqual(published, {(a.value, b.value) for a, b in DOCUMENTED})

    def test_running_back_to_pending_is_recovery_only(self) -> None:
        with self.assertRaises(TransitionError):
            check_transition(R, P)
        check_transition(R, P, recovery=True)

    def test_completed_never_goes_back_without_invalidation(self) -> None:
        for target in (P, R, F, B, C):
            with self.subTest(target.value), self.assertRaises(TransitionError):
                record_in(C).transition(target, recovery=True)

    def test_no_state_loops_to_itself(self) -> None:
        for state in StageState:
            with self.subTest(state.value), self.assertRaises(TransitionError):
                check_transition(state, state, recovery=True)

    def test_non_enum_states_are_rejected(self) -> None:
        with self.assertRaises(TransitionError):
            check_transition("PENDING", "RUNNING")  # type: ignore[arg-type]

    def test_six_states_and_ready_is_derived_not_stored(self) -> None:
        self.assertEqual({s.value for s in StageState}, {"PENDING", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "INVALIDATED"})
        self.assertNotIn("READY", {s.value for s in StageState})

    def test_is_runnable_is_derived_from_dependencies(self) -> None:
        self.assertTrue(is_runnable(P, []))
        self.assertTrue(is_runnable(I, [C, C]))
        self.assertFalse(is_runnable(P, [C, R]))
        self.assertFalse(is_runnable(P, [F]))
        self.assertFalse(is_runnable(C, [C]))
        self.assertFalse(is_runnable(B, []))
        self.assertFalse(is_runnable(F, []))


class Invariants(unittest.TestCase):
    def test_every_state_has_a_valid_record(self) -> None:
        for state in StageState:
            with self.subTest(state.value):
                self.assertEqual(record_in(state).state, state)

    def test_completed_requires_checkpoint_and_identity(self) -> None:
        with self.assertRaises(TransitionError):
            StageRecord("s", C, 1, checkpoint_id=CKPT)
        with self.assertRaises(TransitionError):
            StageRecord("s", C, 1, last_identity=IDENT)

    def test_failed_requires_a_failure_and_no_checkpoint(self) -> None:
        with self.assertRaises(TransitionError):
            StageRecord("s", F, 1)
        with self.assertRaises(TransitionError):
            StageRecord("s", F, 1, failure=failure(), checkpoint_id=CKPT)

    def test_blocked_and_invalidated_require_the_right_cause(self) -> None:
        with self.assertRaises(TransitionError):
            StageRecord("s", B, 0)
        with self.assertRaises(TransitionError):
            StageRecord("s", B, 0, cause=Cause(UPSTREAM_INVALIDATED, "dep"))
        with self.assertRaises(TransitionError):
            StageRecord("s", I, 0)
        with self.assertRaises(TransitionError):
            StageRecord("s", I, 0, cause=Cause(UPSTREAM_FAILED, "dep"))

    def test_running_needs_an_attempt_and_carries_no_result(self) -> None:
        with self.assertRaises(TransitionError):
            StageRecord("s", R, 0)
        with self.assertRaises(TransitionError):
            StageRecord("s", R, 1, checkpoint_id=CKPT)

    def test_pending_is_clean(self) -> None:
        for extra in ({"failure": failure()}, {"cause": Cause(UPSTREAM_FAILED)}, {"checkpoint_id": CKPT}, {"last_identity": IDENT}):
            with self.subTest(list(extra)), self.assertRaises(TransitionError):
                StageRecord("s", P, 0, **extra)

    def test_malformed_fields_are_rejected(self) -> None:
        for kwargs in (
            {"stage_id": "BAD"}, {"attempts": -1}, {"attempts": True}, {"attempts": "1"},
            {"state": "PENDING"}, {"checkpoint_id": "x"}, {"last_identity": "y"},
        ):
            with self.subTest(kwargs), self.assertRaises(TransitionError):
                StageRecord(**{"stage_id": "s", **kwargs})

    def test_failure_of_another_stage_is_rejected(self) -> None:
        with self.assertRaises(TransitionError):
            StageRecord("s", F, 1, failure=failure("other"))

    def test_invalid_transition_through_the_record_api_fails(self) -> None:
        with self.assertRaises(TransitionError):
            record_in(P).transition(C, checkpoint_id=CKPT, last_identity=IDENT)
        with self.assertRaises(TransitionError):
            record_in(F).transition(R)
        with self.assertRaises(TransitionError):
            record_in(B).transition(R)

    def test_arrival_state_invariants_are_enforced_by_transition(self) -> None:
        with self.assertRaises(TransitionError):
            record_in(R).transition(C)                       # sem checkpoint
        with self.assertRaises(TransitionError):
            record_in(R).transition(F)                       # sem falha
        with self.assertRaises(TransitionError):
            record_in(P).transition(B)                       # sem causa


class Attempts(unittest.TestCase):
    def test_entering_running_counts_an_attempt(self) -> None:
        first = record_in(P).transition(R)
        self.assertEqual(first.attempts, 1)
        failed = first.transition(F, failure=failure(attempt=1))
        again = failed.transition(P).transition(R)
        self.assertEqual(again.attempts, 2)

    def test_recovery_can_refund_the_interrupted_attempt(self) -> None:
        running = record_in(P).transition(R)
        back = running.transition(P, recovery=True, refund_attempt=True)
        self.assertEqual((back.state, back.attempts), (P, 0))

    def test_refund_without_recovery_is_rejected(self) -> None:
        with self.assertRaises(TransitionError):
            record_in(P).transition(R).transition(F, failure=failure(), refund_attempt=True)

    def test_refund_never_goes_below_zero(self) -> None:
        self.assertEqual(StageRecord("s", R, 1).transition(P, recovery=True, refund_attempt=True).attempts, 0)

    def test_invalidation_resets_attempts(self) -> None:
        self.assertEqual(record_in(C, attempts=3).transition(I, cause=Cause(UPSTREAM_INVALIDATED, "a")).attempts, 0)

    def test_reset_attempts_on_demand(self) -> None:
        self.assertEqual(record_in(F, attempts=4).transition(P, reset_attempts=True).attempts, 0)
        self.assertEqual(record_in(F, attempts=4).transition(P).attempts, 4)

    def test_identity_is_kept_only_where_it_means_something(self) -> None:
        running = record_in(P).transition(R, last_identity=IDENT)
        self.assertEqual(running.last_identity, IDENT)
        self.assertEqual(running.transition(F, failure=failure()).last_identity, IDENT)
        done = running.transition(C, checkpoint_id=CKPT)
        self.assertEqual(done.last_identity, IDENT)
        self.assertIsNone(done.transition(I, cause=Cause(UPSTREAM_INVALIDATED, "a")).last_identity)
        self.assertIsNone(running.transition(P, recovery=True).last_identity)


class PortableFailure(unittest.TestCase):
    def test_exception_is_summarized_not_serialized(self) -> None:
        info = failure_from_exception("s", ValueError("valor ruim"), 2)
        self.assertEqual((info.stage_id, info.category, info.attempt, info.checkpoint_state), ("s", HANDLER_ERROR, 2, "ABSENT"))
        self.assertEqual(info.message, "ValueError: valor ruim")
        self.assertIn("ValueError", canonical_dumps(info.to_dict()))
        for value in info.to_dict().values():
            self.assertIn(type(value), (str, int))

    def test_local_paths_never_enter_the_message(self) -> None:
        for text in ("falha em C:\\Users\\alguem\\arq.txt", "falha em /home/alguem/arq.txt", "\\\\servidor\\pasta\\x", "file:///tmp/x"):
            with self.subTest(text):
                cleaned = sanitize_message(text)
                self.assertNotIn("alguem", cleaned)
                self.assertNotIn("servidor", cleaned)
                FailureInfo("s", HANDLER_ERROR, cleaned, 1)

    def test_message_is_bounded_and_controls_removed(self) -> None:
        cleaned = sanitize_message("x" * 1000)
        self.assertEqual(len(cleaned), 400)
        self.assertNotIn("\n", sanitize_message("a\nb\x00c"))
        self.assertTrue(sanitize_message("   "))

    def test_failure_validation(self) -> None:
        with self.assertRaises(TransitionError):
            FailureInfo("s", "MYSTERY", "x", 1)
        with self.assertRaises(TransitionError):
            FailureInfo("s", HANDLER_ERROR, "", 1)
        with self.assertRaises(TransitionError):
            FailureInfo("s", HANDLER_ERROR, "ok", -1)
        with self.assertRaises(TransitionError):
            FailureInfo("s", HANDLER_ERROR, "em C:\\Users\\x\\y", 1)
        with self.assertRaises(TransitionError):
            FailureInfo("s", HANDLER_ERROR, "ok", 1, "MAYBE")

    def test_categories_and_causes_are_closed_sets(self) -> None:
        self.assertIn(INTERRUPTED, FAILURE_CATEGORIES)
        self.assertIn(UPSTREAM_FAILED, CAUSE_CODES)
        with self.assertRaises(TransitionError):
            Cause("WHATEVER")
        with self.assertRaises(TransitionError):
            Cause(UPSTREAM_FAILED, "BAD")
        with self.assertRaises(TransitionError):
            Cause(UPSTREAM_FAILED, "a", "em C:\\Users\\x")

    def test_round_trip_for_every_state(self) -> None:
        for state in StageState:
            item = record_in(state)
            with self.subTest(state.value):
                self.assertEqual(StageRecord.from_dict(item.to_dict()), item)

    def test_malformed_records_are_rejected_on_load(self) -> None:
        for data in ({}, {"stage_id": "s"}, {"stage_id": "s", "state": "NOPE", "attempts": 0},
                     {"stage_id": "s", "state": "PENDING", "attempts": 0, "extra": 1}, "x"):
            with self.subTest(data), self.assertRaises(TransitionError):
                StageRecord.from_dict(data)


if __name__ == "__main__":
    unittest.main()
