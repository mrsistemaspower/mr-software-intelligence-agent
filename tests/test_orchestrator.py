"""Orquestração determinística: fluxo sintético, ramos, propagação, falha, interrupção, corrupção, estado atômico e proteção de fontes."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import unittest
from unittest import mock

from tests.helpers import TempDirCase
from tests.wf_helpers import (
    BRANCHED_INPUTS,
    DIAMOND_INPUTS,
    LINEAR_INPUTS,
    SRC,
    WORK,
    Scenario,
    branched_specs,
    diamond_specs,
    linear_specs,
    retrying,
    spec,
)
from core.orchestrator import (
    InputMutatedError,
    Orchestrator,
    OutputContractError,
    RunMismatchError,
    StageFailure,
)
from core.protection import Protection
from core.resume import Decision
from core.run_config import ConfigError, InterruptionPolicy
from core.run_state import RunStateError, RunStatus, load_run_state, save_run_state
from core.snapshot import ArtifactEntry, build_snapshot
from core.stage_graph import RetryPolicy
from core.stage_state import StageRecord, StageState

ORDER = ["inventory", "discover", "analyze", "validate", "document"]


class Crash(BaseException):
    """Simula o desaparecimento do processo: não é Exception, então o orquestrador não a captura."""


def raising(exc: BaseException):
    def handler(ctx, default) -> None:
        raise exc

    return handler


def snapshot(label: str, software_id: str = "synthetic-one"):
    return build_snapshot([ArtifactEntry.from_bytes("k/a.txt", "doc", label.encode())], created_from_kind="declared-list",
                          created_from_label="teste", software_id=software_id)


def mutating(filename: str, text: str = "someone else wrote this"):
    """Tratador mal-comportado: faz o trabalho e depois escreve numa raiz de fonte protegida."""

    def handler(ctx, default) -> None:
        default(ctx)
        (ctx._runner.guard.local_root(SRC) / filename).write_text(text)

    return handler


def mtimes(paths) -> dict:
    return {str(p): p.stat().st_mtime_ns for p in paths}


def retry_all(specs, attempts: int):
    return [dataclasses.replace(item, retry=RetryPolicy(attempts)) for item in specs]


class SyntheticLinearWorkflow(TempDirCase):
    """inventory -> discover -> analyze -> validate -> document."""

    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)

    def test_first_run_executes_every_stage_in_dependency_order(self) -> None:
        result = self.s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.s.calls, ORDER)
        self.assertEqual((result.executed, result.skipped, result.failed), (tuple(ORDER), (), ()))
        for name in ORDER:
            self.assertTrue(self.s.output_file(name).is_file(), name)
            self.assertTrue(self.s.checkpoint_file(name).is_file(), name)
        self.assertEqual(set(self.s.states(result).values()), {"COMPLETED"})
        self.assertEqual(load_run_state(self.s.state_file), result.state)

    def test_second_run_unchanged_skips_every_valid_completed_stage(self) -> None:
        self.s.run()
        outputs = [self.s.output_file(n) for n in ORDER]
        checkpoints = [self.s.checkpoint_file(n) for n in ORDER]
        before = (mtimes(outputs), mtimes(checkpoints))
        second = self.s.run()
        self.assertEqual(self.s.calls, [], "nenhum tratador foi chamado")
        self.assertEqual((second.executed, second.skipped), ((), tuple(ORDER)))
        self.assertTrue(all(item.decision is Decision.SKIP for item in second.plan.stages))
        self.assertIs(second.status, RunStatus.COMPLETED)
        self.assertEqual((mtimes(outputs), mtimes(checkpoints)), before, "nem saída nem checkpoint foi regravado")

    def test_resume_count_grows_only_when_an_existing_run_is_reopened(self) -> None:
        self.assertEqual(self.s.run().state.resume_count, 0)
        self.assertEqual(self.s.run().state.resume_count, 1)
        self.assertEqual(self.s.run().state.resume_count, 2)

    def test_changing_the_analysis_input_reruns_analyze_validate_and_document_only(self) -> None:
        self.s.run()
        untouched = [self.s.output_file(n) for n in ("inventory", "discover")]
        before = mtimes(untouched)
        old_document = self.s.output_file("document").read_bytes()
        self.s.write_input("analysis-input", "rules v2\n")
        result = self.s.run()
        self.assertEqual(self.s.calls, ["analyze", "validate", "document"])
        self.assertEqual(result.skipped, ("inventory", "discover"))
        self.assertEqual(result.executed, ("analyze", "validate", "document"))
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(mtimes(untouched), before)
        self.assertNotEqual(self.s.output_file("document").read_bytes(), old_document)
        analyze = result.plan.of("analyze")
        self.assertEqual((analyze.decision, analyze.reason, analyze.detail), (Decision.RUN, "INPUT_CHANGED", "analysis-input"))
        for downstream in ("validate", "document"):
            item = result.plan.of(downstream)
            self.assertEqual((item.decision, item.reason), (Decision.RUN, "UPSTREAM_INVALIDATED"))

    def test_after_the_partial_rerun_everything_is_stable_again(self) -> None:
        self.s.run()
        self.s.write_input("analysis-input", "rules v2\n")
        self.s.run()
        again = self.s.run()
        self.assertEqual((self.s.calls, again.skipped), ([], tuple(ORDER)))

    def test_changing_the_first_input_reruns_everything(self) -> None:
        self.s.run()
        self.s.write_input("inventory-source", "item-1\nitem-2\nitem-3\n")
        self.s.run()
        self.assertEqual(self.s.calls, ORDER)

    def test_changing_back_to_the_original_input_is_a_change_too(self) -> None:
        self.s.run()
        self.s.write_input("analysis-input", "rules v2\n")
        self.s.run()
        self.s.write_input("analysis-input", LINEAR_INPUTS["analysis-input"])
        self.s.run()
        self.assertEqual(self.s.calls, ["analyze", "validate", "document"])

    def test_a_stage_with_only_a_changed_downstream_dependency_in_name_is_untouched(self) -> None:
        self.s.run()
        self.s.write_input("analysis-input", "rules v2\n")
        self.s.run()
        self.assertNotIn("inventory", self.s.calls)

    def test_outputs_are_deterministic_functions_of_inputs(self) -> None:
        self.s.run()
        first = {n: self.s.output_file(n).read_bytes() for n in ORDER}
        twin = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="twin")
        twin.run()
        self.assertEqual({n: twin.output_file(n).read_bytes() for n in ORDER}, first)


class BranchedWorkflow(TempDirCase):
    """inventory -> ui-analysis -> documentation ; inventory -> database-analysis."""

    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, branched_specs(), BRANCHED_INPUTS)
        self.s.run()

    def test_first_run_follows_the_deterministic_order(self) -> None:
        self.assertEqual(self.s.calls, ["inventory", "database-analysis", "ui-analysis", "documentation"])

    def test_changing_only_the_ui_input_leaves_the_database_branch_valid(self) -> None:
        self.s.write_input("ui-input", "ui v2\n")
        result = self.s.run()
        self.assertEqual(self.s.calls, ["ui-analysis", "documentation"])
        self.assertEqual(result.skipped, ("inventory", "database-analysis"))
        self.assertEqual(result.plan.of("ui-analysis").reason, "INPUT_CHANGED")
        self.assertEqual(result.plan.of("documentation").reason, "UPSTREAM_INVALIDATED")
        self.assertEqual(result.plan.of("database-analysis").decision, Decision.SKIP)

    def test_changing_only_the_database_input_invalidates_nothing_else(self) -> None:
        self.s.write_input("db-input", "db v2\n")
        result = self.s.run()
        self.assertEqual(self.s.calls, ["database-analysis"])
        self.assertEqual(result.skipped, ("inventory", "ui-analysis", "documentation"))

    def test_changing_the_shared_root_invalidates_both_branches(self) -> None:
        self.s.write_input("inventory-source", "base v2\n")
        self.s.run()
        self.assertEqual(self.s.calls, ["inventory", "database-analysis", "ui-analysis", "documentation"])

    def test_the_valid_branch_checkpoint_and_output_are_not_even_rewritten(self) -> None:
        keep = [self.s.output_file("database-analysis"), self.s.checkpoint_file("database-analysis")]
        before = mtimes(keep)
        self.s.write_input("ui-input", "ui v2\n")
        self.s.run()
        self.assertEqual(mtimes(keep), before)


class ChangePropagation(TempDirCase):
    """A -> B -> C e A -> D (seção de propagação da especificação)."""

    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, diamond_specs(), DIAMOND_INPUTS)
        self.s.run()

    def reasons(self, result) -> dict:
        return {item.stage_id: (item.decision.value, item.reason) for item in result.plan.stages}

    def test_when_a_changes_b_c_and_d_are_invalidated(self) -> None:
        self.s.output_file("a").write_bytes(b"A's relevant output changed")
        result = self.s.run()
        self.assertEqual(self.s.calls, ["a", "b", "c", "d"])
        self.assertEqual(self.reasons(result), {
            "a": ("RUN", "OUTPUT_MUTATED"), "b": ("RUN", "UPSTREAM_INVALIDATED"),
            "c": ("RUN", "UPSTREAM_INVALIDATED"), "d": ("RUN", "UPSTREAM_INVALIDATED"),
        })

    def test_when_a_input_changes_everything_downstream_follows(self) -> None:
        self.s.write_input("seed", "seed v2\n")
        self.s.run()
        self.assertEqual(self.s.calls, ["a", "b", "c", "d"])

    def test_when_only_b_changes_c_is_invalidated_and_d_stays_valid(self) -> None:
        self.s.output_file("b").write_bytes(b"B changed")
        result = self.s.run()
        self.assertEqual(self.s.calls, ["b", "c"])
        self.assertEqual(self.reasons(result), {
            "a": ("SKIP", "CHECKPOINT_VALID"), "b": ("RUN", "OUTPUT_MUTATED"),
            "c": ("RUN", "UPSTREAM_INVALIDATED"), "d": ("SKIP", "CHECKPOINT_VALID"),
        })

    def test_when_only_c_changes_nothing_else_is_touched(self) -> None:
        self.s.output_file("c").unlink()
        self.s.run()
        self.assertEqual(self.s.calls, ["c"])

    def test_when_only_d_changes_nothing_else_is_touched(self) -> None:
        self.s.output_file("d").write_bytes(b"D changed")
        self.s.run()
        self.assertEqual(self.s.calls, ["d"])

    def test_the_origin_of_a_propagated_invalidation_is_the_stage_that_really_changed(self) -> None:
        self.s.output_file("b").write_bytes(b"B changed")
        result = self.s.run()
        c = result.plan.of("c")
        self.assertEqual(c.detail, "b")

    def test_unrelated_branch_state_is_byte_identical_after_the_rerun(self) -> None:
        before = {n: self.s.checkpoint_file(n).read_bytes() for n in ("a", "d")}
        outputs = {n: self.s.output_file(n).read_bytes() for n in ("a", "d")}
        self.s.output_file("b").write_bytes(b"B changed")
        self.s.run()
        self.assertEqual({n: self.s.checkpoint_file(n).read_bytes() for n in ("a", "d")}, before)
        self.assertEqual({n: self.s.output_file(n).read_bytes() for n in ("a", "d")}, outputs)

    def test_input_of_a_middle_stage_invalidates_its_descendants_only(self) -> None:
        specs = [spec("a", inputs=["seed"]), spec("b", deps=["a"], inputs=["b-input"]), spec("c", deps=["b"]), spec("d", deps=["a"])]
        s = Scenario(self.tmp, specs, {"seed": "s\n", "b-input": "b1\n"}, tag="middle")
        s.run()
        s.write_input("b-input", "b2\n")
        result = s.run()
        self.assertEqual(s.calls, ["b", "c"])
        self.assertEqual(result.skipped, ("a", "d"))

    def test_a_skipped_ancestor_never_hides_a_changed_descendant(self) -> None:
        self.s.output_file("c").write_bytes(b"C changed")
        result = self.s.run()
        self.assertEqual(result.skipped, ("a", "b", "d"))
        self.assertEqual(self.s.calls, ["c"])


class FailureRecording(TempDirCase):
    def test_a_failing_handler_is_recorded_as_portable_data_and_stops_the_run(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": raising(ValueError("boom"))})
        result = s.run()
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertEqual(s.calls, ["inventory", "discover"], "nenhum estágio roda depois de uma falha crítica")
        self.assertEqual(result.failed, ("discover",))
        record = result.state.stage("discover")
        self.assertIs(record.state, StageState.FAILED)
        failure = record.failure
        self.assertEqual((failure.stage_id, failure.category, failure.message, failure.attempt, failure.checkpoint_state),
                         ("discover", "HANDLER_ERROR", "ValueError: boom", 1, "ABSENT"))
        self.assertEqual({n: s.states(result)[n] for n in ORDER}, {
            "inventory": "COMPLETED", "discover": "FAILED", "analyze": "PENDING", "validate": "PENDING", "document": "PENDING"})
        self.assertFalse(s.checkpoint_file("discover").exists())
        self.assertTrue(s.checkpoint_file("inventory").exists())
        self.assertEqual(load_run_state(s.state_file), result.state)

    def test_no_python_exception_leaks_into_the_portable_contract(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": raising(RuntimeError("falhou"))})
        s.run()
        text = s.state_file.read_text(encoding="utf-8")
        for fragment in ("Traceback", "File \"", "<class", "0x", "object at"):
            self.assertNotIn(fragment, text)
        data = json.loads(text)
        failure = next(item["failure"] for item in data["stages"] if item["stage_id"] == "discover")
        self.assertEqual(set(failure), {"stage_id", "category", "message", "attempt", "checkpoint_state"})

    def test_a_message_with_a_local_path_is_withheld(self) -> None:
        secret = str(self.tmp / "private-folder" / "data.bin")
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": raising(OSError(f"cannot open {secret}"))})
        result = s.run()
        message = result.state.stage("discover").failure.message
        self.assertNotIn("private-folder", message)
        self.assertNotIn("private-folder", s.state_file.read_text(encoding="utf-8"))

    def test_a_handler_can_pick_the_failure_category(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": raising(StageFailure("sem entrada", "INPUT_UNAVAILABLE"))})
        self.assertEqual(s.run().state.stage("discover").failure.category, "INPUT_UNAVAILABLE")

    def test_empty_exception_message_still_produces_a_record(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": raising(KeyError())})
        self.assertEqual(s.run().state.stage("discover").failure.message, "KeyError")

    def test_a_failure_on_a_branch_stops_independent_branches_too(self) -> None:
        s = Scenario(self.tmp, branched_specs(), BRANCHED_INPUTS, handlers={"database-analysis": raising(ValueError("x"))})
        result = s.run()
        self.assertEqual(s.calls, ["inventory", "database-analysis"])
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertEqual(s.states(result)["ui-analysis"], "PENDING")

    def test_retry_runs_only_the_failed_stage_and_counts_attempts(self) -> None:
        attempts: list[int] = []

        def flaky(ctx, default) -> None:
            attempts.append(ctx.attempt)
            if len(attempts) == 1:
                raise ValueError("primeira vez")
            default(ctx)

        specs = [retrying("discover", 3, deps=["inventory"]) if item.stage_id == "discover" else item for item in linear_specs()]
        s = Scenario(self.tmp, specs, LINEAR_INPUTS, handlers={"discover": flaky})
        self.assertIs(s.run().status, RunStatus.FAILED)
        result = s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(s.calls, ["discover", "analyze", "validate", "document"])
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(result.plan.of("discover").decision, Decision.RETRY)
        self.assertEqual(result.state.stage("discover").attempts, 2)

    def test_attempts_are_exhausted_and_the_stage_stays_failed_without_running(self) -> None:
        calls = {"n": 0}

        def always(ctx, default) -> None:
            calls["n"] += 1
            raise ValueError("sempre")

        specs = [retrying("discover", 2, deps=["inventory"]) if item.stage_id == "discover" else item for item in linear_specs()]
        s = Scenario(self.tmp, specs, LINEAR_INPUTS, handlers={"discover": always})
        s.run()
        s.run()
        third = s.run()
        self.assertEqual(calls["n"], 2)
        self.assertEqual(third.plan.of("discover").decision, Decision.EXHAUSTED)
        self.assertIs(third.status, RunStatus.FAILED)
        self.assertEqual(third.state.stage("discover").attempts, 2)
        self.assertEqual(s.calls, [])

    def test_changed_inputs_give_an_exhausted_stage_a_fresh_life(self) -> None:
        mode = {"fail": True}

        def sometimes(ctx, default) -> None:
            if mode["fail"]:
                raise ValueError("ruim")
            default(ctx)

        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"analyze": sometimes})
        s.run()
        self.assertEqual(s.run().plan.of("analyze").decision, Decision.EXHAUSTED)
        mode["fail"] = False
        s.write_input("analysis-input", "rules v2\n")
        result = s.run()
        self.assertEqual((result.plan.of("analyze").decision, result.plan.of("analyze").reason), (Decision.RUN, "INPUTS_CHANGED_SINCE_FAILURE"))
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state.stage("analyze").attempts, 1)

    def test_a_failing_stage_whose_upstream_is_rebuilt_gets_its_attempts_back(self) -> None:
        mode = {"fail": True}

        def sometimes(ctx, default) -> None:
            if mode["fail"]:
                raise ValueError("ruim")
            default(ctx)

        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": sometimes})
        s.run()
        mode["fail"] = False
        s.write_input("inventory-source", "new content\n")
        result = s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(s.calls, ORDER)
        self.assertEqual(result.state.stage("discover").attempts, 1)

    def test_optional_failure_blocks_only_its_dependents_and_the_run_completes(self) -> None:
        specs = [spec("inventory", inputs=["inventory-source"]), spec("docs", deps=["inventory"]),
                 spec("report", deps=["inventory"], optional=True), spec("extra", deps=["report"], optional=True)]
        s = Scenario(self.tmp, specs, {"inventory-source": "x\n"}, handlers={"report": raising(ValueError("opcional"))})
        result = s.run()
        self.assertEqual(s.calls, ["inventory", "docs", "report"])
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(s.states(result), {"inventory": "COMPLETED", "docs": "COMPLETED", "report": "FAILED", "extra": "BLOCKED"})
        self.assertEqual(result.state.stage("extra").cause.stage_id, "report")
        self.assertEqual(load_run_state(s.state_file), result.state)

    def test_a_recovered_optional_stage_unblocks_its_dependents(self) -> None:
        mode = {"fail": True}

        def sometimes(ctx, default) -> None:
            if mode["fail"]:
                raise ValueError("opcional")
            default(ctx)

        specs = [spec("inventory", inputs=["inventory-source"]), retrying("report", 3, deps=["inventory"], optional=True),
                 spec("extra", deps=["report"], optional=True)]
        s = Scenario(self.tmp, specs, {"inventory-source": "x\n"}, handlers={"report": sometimes})
        s.run()
        mode["fail"] = False
        result = s.run()
        self.assertEqual(s.calls, ["report", "extra"])
        self.assertEqual(s.states(result), {"inventory": "COMPLETED", "report": "COMPLETED", "extra": "COMPLETED"})
        self.assertEqual(result.plan.of("extra").reason, "UNBLOCKED")

    def test_an_exhausted_optional_stage_keeps_blocking_across_resumes(self) -> None:
        specs = [spec("inventory", inputs=["inventory-source"]), spec("report", deps=["inventory"], optional=True),
                 spec("extra", deps=["report"], optional=True)]
        s = Scenario(self.tmp, specs, {"inventory-source": "x\n"}, handlers={"report": raising(ValueError("opcional"))})
        s.run()
        result = s.run()
        self.assertEqual(s.calls, [])
        self.assertEqual(result.plan.of("report").decision, Decision.EXHAUSTED)
        self.assertEqual(result.plan.of("extra").decision, Decision.BLOCKED)
        self.assertIs(result.status, RunStatus.COMPLETED)


class OutputAndInputContracts(TempDirCase):
    def test_a_declared_output_that_was_not_produced_fails_the_stage(self) -> None:
        def lazy(ctx, default) -> None:
            return None

        s = Scenario(self.tmp, [spec("only", outputs=("one", "two"))], handlers={"only": lazy})
        result = s.run()
        failure = result.state.stage("only").failure
        self.assertEqual(failure.category, "OUTPUT_CONTRACT")
        self.assertIn("one", failure.message)
        self.assertFalse(s.checkpoint_file("only").exists())

    def test_a_partially_produced_output_set_is_not_completion(self) -> None:
        def half(ctx, default) -> None:
            ctx.write_output("one", b"1")

        s = Scenario(self.tmp, [spec("only", outputs=("one", "two"))], handlers={"only": half})
        result = s.run()
        self.assertIs(result.state.stage("only").state, StageState.FAILED)
        self.assertIn("two", result.state.stage("only").failure.message)

    def test_writing_an_undeclared_output_is_refused(self) -> None:
        def sneaky(ctx, default) -> None:
            ctx.write_output("secret", b"x")

        s = Scenario(self.tmp, [spec("only")], handlers={"only": sneaky})
        self.assertEqual(s.run().state.stage("only").failure.category, "OUTPUT_CONTRACT")
        self.assertFalse((s.work / "outputs" / "only" / "secret").exists())

    def test_non_bytes_output_is_refused(self) -> None:
        s = Scenario(self.tmp, [spec("only")], handlers={"only": lambda ctx, default: ctx.write_output("result", "texto")})
        self.assertEqual(s.run().state.stage("only").failure.category, "OUTPUT_CONTRACT")

    def test_reading_an_undeclared_input_is_refused(self) -> None:
        s = Scenario(self.tmp, [spec("a", inputs=["one"]), spec("b", deps=["a"])], {"one": "1"},
                     handlers={"b": lambda ctx, default: ctx.read_input("one")})
        self.assertEqual(s.run().state.stage("b").failure.category, "HANDLER_ERROR")

    def test_reading_the_output_of_a_non_dependency_is_refused(self) -> None:
        s = Scenario(self.tmp, [spec("a"), spec("b")], handlers={"b": lambda ctx, default: ctx.read_upstream("a", "result")})
        self.assertEqual(s.run().state.stage("b").failure.category, "HANDLER_ERROR")

    def test_reading_an_undeclared_output_of_a_dependency_is_refused(self) -> None:
        s = Scenario(self.tmp, [spec("a"), spec("b", deps=["a"])], handlers={"b": lambda ctx, default: ctx.read_upstream("a", "ghost")})
        self.assertEqual(s.run().state.stage("b").failure.category, "HANDLER_ERROR")

    def test_an_upstream_output_changed_behind_the_checkpoint_is_detected_when_read(self) -> None:
        def reader(ctx, default) -> None:
            ctx._runner.guard.output_path(WORK, "outputs/a/result").write_bytes(b"changed behind the back")
            ctx.read_upstream("a", "result")

        s = Scenario(self.tmp, [spec("a"), spec("b", deps=["a"])], handlers={"b": reader})
        self.assertEqual(s.run().state.stage("b").failure.category, "INPUT_MUTATED")

    def test_a_missing_input_fails_before_the_handler_runs(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        (s.src / "analysis-input.txt").unlink()
        result = s.run()
        self.assertEqual(s.calls, ["inventory", "discover"], "o tratador de analyze não chegou a rodar")
        failure = result.state.stage("analyze").failure
        self.assertEqual((failure.category, result.status), ("INPUT_UNAVAILABLE", RunStatus.FAILED))
        self.assertIn("analysis-input", failure.message)

    def test_the_missing_input_appears_later_and_the_stage_gets_a_fresh_life(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        (s.src / "analysis-input.txt").unlink()
        s.run()
        s.write_input("analysis-input", "back again\n")
        result = s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(s.calls, ["analyze", "validate", "document"])

    def test_an_input_that_changes_during_the_stage_is_never_checkpointed(self) -> None:
        def mutating(ctx, default) -> None:
            default(ctx)
            (ctx._runner.guard.local_root(SRC) / "analysis-input.txt").write_text("changed while running")

        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, src_verify="NONE", handlers={"analyze": mutating})
        result = s.run()
        self.assertEqual(result.state.stage("analyze").failure.category, "INPUT_MUTATED")
        self.assertFalse(s.checkpoint_file("analyze").exists())

    def test_reading_an_input_that_changed_since_the_stage_started_fails(self) -> None:
        def mutating(ctx, default) -> None:
            (ctx._runner.guard.local_root(SRC) / "analysis-input.txt").write_text("changed before reading")
            ctx.read_input("analysis-input")

        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, src_verify="NONE", handlers={"analyze": mutating})
        self.assertEqual(s.run().state.stage("analyze").failure.category, "INPUT_MUTATED")

    def test_exceptions_exported_for_handlers(self) -> None:
        self.assertEqual(InputMutatedError("x").category, "INPUT_MUTATED")
        self.assertEqual(OutputContractError("x").category, "OUTPUT_CONTRACT")
        self.assertEqual(StageFailure("x").category, "HANDLER_ERROR")


class InterruptedRuns(TempDirCase):
    def crash_at_validate(self, **kw) -> Scenario:
        def crashing(ctx, default) -> None:
            ctx.write_output("result", b"partial output that must never be trusted")
            raise Crash()

        s = Scenario(self.tmp, kw.pop("specs", linear_specs()), LINEAR_INPUTS, handlers={"validate": crashing}, **kw)
        with self.assertRaises(Crash):
            s.run()
        return s

    def test_a_vanished_process_leaves_a_running_stage_on_disk(self) -> None:
        s = self.crash_at_validate()
        state = load_run_state(s.state_file)
        self.assertIs(state.status, RunStatus.RUNNING)
        self.assertEqual(state.current_stage, "validate")
        self.assertIs(state.stage("validate").state, StageState.RUNNING)
        self.assertIs(state.stage("document").state, StageState.PENDING)
        self.assertFalse(s.checkpoint_file("validate").exists(), "o estágio interrompido não ganhou checkpoint")

    def test_resume_requeues_the_running_stage_and_refunds_the_attempt(self) -> None:
        s = self.crash_at_validate()
        s.custom.clear()
        result = s.run()
        self.assertEqual(result.plan.of("validate").decision, Decision.REQUEUE)
        self.assertEqual(s.calls, ["validate", "document"])
        self.assertEqual(result.skipped, ("inventory", "discover", "analyze"))
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state.stage("validate").attempts, 1)

    def test_partial_output_is_replaced_and_never_trusted(self) -> None:
        s = self.crash_at_validate()
        self.assertEqual(s.output_file("validate").read_bytes(), b"partial output that must never be trusted")
        s.custom.clear()
        s.run()
        self.assertNotEqual(s.output_file("validate").read_bytes(), b"partial output that must never be trusted")

    def test_fail_policy_turns_the_interruption_into_a_counted_failure(self) -> None:
        s = self.crash_at_validate(interruption=InterruptionPolicy.FAIL)
        s.custom.clear()
        result = s.run()
        failure = result.state.stage("validate").failure
        self.assertEqual((failure.category, failure.attempt), ("INTERRUPTED", 1))
        self.assertIs(result.status, RunStatus.FAILED)
        self.assertEqual(s.calls, [])
        self.assertEqual(result.plan.of("validate").decision, Decision.EXHAUSTED)

    def test_fail_policy_with_attempts_left_retries(self) -> None:
        specs = [retrying("validate", 2, deps=["analyze"]) if item.stage_id == "validate" else item for item in linear_specs()]
        s = self.crash_at_validate(interruption=InterruptionPolicy.FAIL, specs=specs)
        s.custom.clear()
        result = s.run()
        self.assertEqual(result.plan.of("validate").decision, Decision.RETRY)
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state.stage("validate").attempts, 2)

    def test_a_stage_marked_running_on_disk_is_never_assumed_complete_even_with_a_valid_checkpoint(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        s.run()
        state = load_run_state(s.state_file)
        stages = tuple(StageRecord("validate", StageState.RUNNING, 1) if r.stage_id == "validate" else r for r in state.stages)
        save_run_state(s.state_file, state.with_changes(stages=stages, current_stage="validate", status=RunStatus.RUNNING))
        self.assertTrue(s.checkpoint_file("validate").exists(), "o checkpoint de uma tentativa anterior ainda está lá")
        result = s.run()
        self.assertEqual(s.calls, ["validate", "document"], "validate refeito; document depende dele")
        self.assertEqual(result.plan.of("validate").decision, Decision.REQUEUE)
        self.assertEqual(result.plan.of("document").reason, "UPSTREAM_INVALIDATED")

    def test_a_run_left_in_running_status_with_no_running_stage_resumes_cleanly(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        s.run()
        state = load_run_state(s.state_file)
        save_run_state(s.state_file, state.with_changes(status=RunStatus.RUNNING))
        result = s.run()
        self.assertEqual((s.calls, result.skipped), ([], tuple(ORDER)))
        self.assertIs(result.status, RunStatus.COMPLETED)

    def test_two_interruptions_in_a_row_still_converge(self) -> None:
        s = self.crash_at_validate()
        with self.assertRaises(Crash):
            s.run()
        s.custom.clear()
        self.assertIs(s.run().status, RunStatus.COMPLETED)
        self.assertEqual(s.run().state.stage("validate").attempts, 1)

    def test_keyboard_interrupt_is_not_swallowed_either(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"validate": raising(KeyboardInterrupt())})
        with self.assertRaises(KeyboardInterrupt):
            s.run()
        self.assertIs(load_run_state(s.state_file).stage("validate").state, StageState.RUNNING)


class CheckpointIntegrity(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        self.s.run()

    def rerun_reasons(self) -> dict:
        result = self.s.run()
        return {item.stage_id: item.reason for item in result.plan.stages if item.decision is Decision.RUN}

    def test_a_truncated_checkpoint_is_corrupt_and_the_stage_and_dependents_rerun(self) -> None:
        path = self.s.checkpoint_file("analyze")
        path.write_bytes(path.read_bytes()[:40])
        reasons = self.rerun_reasons()
        self.assertEqual(self.s.calls, ["analyze", "validate", "document"])
        self.assertEqual(reasons["analyze"], "CHECKPOINT_CORRUPT")

    def test_a_missing_checkpoint_does_not_allow_skipping(self) -> None:
        self.s.checkpoint_file("discover").unlink()
        reasons = self.rerun_reasons()
        self.assertEqual(self.s.calls, ["discover", "analyze", "validate", "document"])
        self.assertEqual(reasons["discover"], "CHECKPOINT_MISSING")

    def test_a_tampered_checkpoint_is_rejected(self) -> None:
        path = self.s.checkpoint_file("analyze")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["outputs"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        reasons = self.rerun_reasons()
        self.assertEqual(reasons["analyze"], "CHECKPOINT_CORRUPT")

    def test_a_checkpoint_copied_from_another_stage_is_rejected(self) -> None:
        shutil.copyfile(self.s.checkpoint_file("discover"), self.s.checkpoint_file("analyze"))
        self.assertEqual(self.rerun_reasons()["analyze"], "CHECKPOINT_CORRUPT")

    def test_an_old_but_valid_looking_checkpoint_restored_over_a_newer_one_is_not_the_recorded_one(self) -> None:
        old = self.s.checkpoint_file("analyze").read_bytes()
        self.s.write_input("analysis-input", "rules v2\n")
        self.s.run()
        self.s.checkpoint_file("analyze").write_bytes(old)
        reasons = self.rerun_reasons()
        self.assertEqual(reasons["analyze"], "CHECKPOINT_MISSING")

    def test_a_mutated_output_invalidates_the_stage(self) -> None:
        self.s.output_file("discover").write_bytes(b"tampered")
        reasons = self.rerun_reasons()
        self.assertEqual(reasons["discover"], "OUTPUT_MUTATED")
        self.assertEqual(self.s.calls, ["discover", "analyze", "validate", "document"])

    def test_a_deleted_output_invalidates_the_stage(self) -> None:
        self.s.output_file("analyze").unlink()
        self.assertEqual(self.rerun_reasons()["analyze"], "OUTPUT_MISSING")

    def test_an_emptied_output_is_not_accepted_just_because_the_file_exists(self) -> None:
        self.s.output_file("analyze").write_bytes(b"")
        self.assertEqual(self.rerun_reasons()["analyze"], "OUTPUT_MUTATED")

    def test_everything_is_stable_again_after_the_repair(self) -> None:
        self.s.output_file("analyze").write_bytes(b"x")
        self.s.run()
        again = self.s.run()
        self.assertEqual((self.s.calls, again.skipped), ([], tuple(ORDER)))

    def test_checkpoints_hold_no_local_path(self) -> None:
        for name in ORDER:
            self.assertNotIn(str(self.s.base), self.s.checkpoint_file(name).read_text(encoding="utf-8"))
        self.assertNotIn(str(self.s.base), self.s.state_file.read_text(encoding="utf-8"))


class KnowledgeSnapshotBinding(TempDirCase):
    def test_a_different_snapshot_invalidates_everything(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, snapshot=snapshot("knowledge v1"))
        s.run()
        s.snapshot = snapshot("knowledge v2")
        result = s.run()
        self.assertEqual(s.calls, ORDER)
        self.assertEqual(result.plan.of("inventory").reason, "SNAPSHOT_CHANGED")
        self.assertEqual(result.state.knowledge_snapshot_id, s.snapshot.snapshot_id)

    def test_the_same_snapshot_content_with_different_metadata_changes_nothing(self) -> None:
        a = snapshot("knowledge v1")
        b = build_snapshot([ArtifactEntry.from_bytes("k/a.txt", "doc", b"knowledge v1")], created_from_kind="derived", created_from_label="outra",
                           software_id="synthetic-one", created_at="2031-05-06T07:08:09Z", notes="outra geração")
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, snapshot=a)
        s.run()
        s.snapshot = b
        self.assertEqual(s.run().skipped, tuple(ORDER))

    def test_adding_or_removing_the_snapshot_invalidates(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        s.run()
        s.snapshot = snapshot("knowledge v1")
        self.assertEqual(s.run().plan.of("inventory").reason, "SNAPSHOT_CHANGED")
        s.snapshot = None
        self.assertEqual(s.run().plan.of("inventory").reason, "SNAPSHOT_CHANGED")

    def test_a_snapshot_of_another_software_is_refused_before_anything_runs(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, snapshot=snapshot("x", software_id="another-erp"))
        with self.assertRaises(ConfigError):
            s.orchestrator()
        self.assertEqual(list(s.work.iterdir()), [])

    def test_the_snapshot_id_is_recorded_in_state_and_checkpoints(self) -> None:
        snap = snapshot("knowledge v1")
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, snapshot=snap)
        s.run()
        self.assertEqual(json.loads(s.checkpoint_file("inventory").read_text(encoding="utf-8"))["knowledge_snapshot_id"], snap.snapshot_id)

    def test_a_non_snapshot_is_refused(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        with self.assertRaises(ConfigError):
            s.orchestrator(snapshot="snap:sha256:" + "1" * 64)


class DefinitionDrift(TempDirCase):
    def test_bumping_one_stage_version_invalidates_it_and_its_descendants_only(self) -> None:
        Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws").run()
        specs = [dataclasses.replace(item, version="2") if item.stage_id == "validate" else item for item in linear_specs()]
        s = Scenario(self.tmp, specs, LINEAR_INPUTS, tag="ws")
        result = s.run()
        self.assertEqual(s.calls, ["validate", "document"])
        self.assertEqual(result.plan.of("validate").reason, "DEFINITION_CHANGED")

    def test_a_stage_added_to_the_graph_runs_alone(self) -> None:
        Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws").run()
        s = Scenario(self.tmp, linear_specs() + [spec("publish", deps=["document"])], LINEAR_INPUTS, tag="ws")
        result = s.run()
        self.assertEqual(s.calls, ["publish"])
        self.assertEqual(result.plan.of("publish").reason, "NOT_RUN_YET")
        self.assertEqual(result.skipped, tuple(ORDER))

    def test_a_stage_removed_from_the_graph_loses_its_checkpoint_and_nothing_else_reruns(self) -> None:
        first = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws")
        first.run()
        shorter = Scenario(self.tmp, linear_specs()[:-1], LINEAR_INPUTS, tag="ws")
        result = shorter.run()
        self.assertEqual(shorter.calls, [])
        self.assertFalse(shorter.checkpoint_file("document").exists())
        self.assertEqual(tuple(item.stage_id for item in result.state.stages), tuple(ORDER[:-1]))

    def test_changed_stage_parameters_invalidate_that_stage_and_downstream(self) -> None:
        Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws", params={"analyze": {"depth": 2}}).run()
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws", params={"analyze": {"depth": 3}})
        result = s.run()
        self.assertEqual(s.calls, ["analyze", "validate", "document"])
        self.assertEqual(result.plan.of("analyze").reason, "CONFIG_CHANGED")

    def test_parameters_of_another_stage_do_not_invalidate(self) -> None:
        Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws", params={"validate": {"strict": True}}).run()
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws", params={"validate": {"strict": True}, "inventory": {}})
        self.assertEqual(s.run().skipped, tuple(ORDER))
        self.assertEqual(s.calls, [])

    def test_changing_only_the_retry_policy_does_not_invalidate_finished_work(self) -> None:
        Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="ws").run()
        s = Scenario(self.tmp, retry_all(linear_specs(), 5), LINEAR_INPUTS, tag="ws")
        self.assertEqual(s.run().skipped, tuple(ORDER))

    def test_changing_the_graph_shape_invalidates_whoever_depends_differently(self) -> None:
        Scenario(self.tmp, diamond_specs(), DIAMOND_INPUTS, tag="ws").run()
        rewired = [spec("a", inputs=["seed"]), spec("b", deps=["a"]), spec("c", deps=["a"]), spec("d", deps=["a"])]
        s = Scenario(self.tmp, rewired, DIAMOND_INPUTS, tag="ws")
        s.run()
        self.assertEqual(s.calls, ["c"], "c mudou de dependência; os demais conservam o trabalho feito")


class StateFileHandling(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)

    def test_a_corrupt_state_is_refused_and_left_untouched(self) -> None:
        self.s.run()
        self.s.state_file.write_bytes(self.s.state_file.read_bytes()[:30])
        damaged = self.s.state_file.read_bytes()
        with self.assertRaises(RunStateError):
            self.s.run()
        self.assertEqual(self.s.state_file.read_bytes(), damaged, "nada é reiniciado em silêncio")
        self.assertEqual(self.s.calls, [])

    def test_start_refuses_when_state_exists_and_resume_refuses_when_it_does_not(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.s.orchestrator().resume()
        self.s.orchestrator().start()
        with self.assertRaises(RunMismatchError):
            self.s.orchestrator().start()
        self.assertIs(self.s.orchestrator().resume().status, RunStatus.COMPLETED)

    def test_state_of_another_workflow_version_is_not_adopted(self) -> None:
        self.s.run()
        other = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, workflow_version="2")
        before = self.s.state_file.read_bytes()
        with self.assertRaises(RunMismatchError):
            other.run()
        self.assertEqual(self.s.state_file.read_bytes(), before)

    def test_different_run_keys_are_different_runs(self) -> None:
        self.s.run()
        other = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, run_key="second-batch")
        with self.assertRaises(RunMismatchError):
            other.run()

    def test_handlers_must_match_the_graph_before_anything_is_written(self) -> None:
        handlers = self.s.handlers()
        for broken in ({k: v for k, v in handlers.items() if k != "analyze"}, {**handlers, "ghost": lambda ctx: None}):
            with self.assertRaises(ConfigError):
                Orchestrator(self.s.config(), broken, {SRC: self.s.src, WORK: self.s.work})
        with self.assertRaises(ConfigError):
            Orchestrator(self.s.config(), {**handlers, "analyze": "not callable"}, {SRC: self.s.src, WORK: self.s.work})  # type: ignore[dict-item]
        self.assertEqual(list(self.s.work.iterdir()), [])

    def test_root_bindings_must_match_the_configuration(self) -> None:
        with self.assertRaises(ConfigError):
            Orchestrator(self.s.config(), self.s.handlers(), {SRC: self.s.src})
        with self.assertRaises(ConfigError):
            Orchestrator(self.s.config(), self.s.handlers(), {SRC: self.s.src, WORK: self.s.work, "extra": self.s.work})

    def test_overlapping_root_bindings_are_refused(self) -> None:
        with self.assertRaises(ConfigError):
            Orchestrator(self.s.config(), self.s.handlers(), {SRC: self.s.src, WORK: self.s.src / "nested"})

    def test_now_is_injected_and_never_part_of_any_identity(self) -> None:
        stamps = iter(["2032-01-01T00:00:00Z", "2033-02-02T00:00:00Z"])
        first = self.s.run(now=lambda: next(stamps))
        self.assertEqual(first.state.started_at, "2032-01-01T00:00:00Z")
        twin = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="twin")
        twin.run()
        for name in ORDER:
            self.assertEqual(self.s.checkpoint_file(name).read_bytes(), twin.checkpoint_file(name).read_bytes())
        again = self.s.run(now=lambda: next(stamps))
        self.assertEqual(again.state.started_at, "2032-01-01T00:00:00Z", "o início original é preservado na retomada")

    def test_a_malformed_timestamp_from_now_is_rejected(self) -> None:
        with self.assertRaises(RunStateError):
            self.s.run(now=lambda: "ontem")
        with self.assertRaises(ConfigError):
            self.s.orchestrator(now="not callable")


class AtomicStatePersistence(TempDirCase):
    """Injeta uma falha em CADA gravação atômica de uma execução e confere que o estado sempre se recupera."""

    def reference(self) -> dict:
        ref = Scenario(self.tmp, retry_all(linear_specs(), 5), LINEAR_INPUTS, tag="reference")
        ref.run()
        return {name: ref.output_file(name).read_bytes() for name in ORDER}

    def test_a_failure_at_every_possible_write_leaves_a_recoverable_state(self) -> None:
        expected = self.reference()
        real_replace = os.replace
        completed_without_fault = False
        for k in range(1, 60):
            s = Scenario(self.tmp, retry_all(linear_specs(), 5), LINEAR_INPUTS, tag=f"fault-{k}")
            counter = {"n": 0}

            def flaky(source, target, _k=k, _counter=counter):
                _counter["n"] += 1
                if _counter["n"] == _k:
                    raise OSError("queda simulada durante a gravação")
                return real_replace(source, target)

            faulted = False
            with mock.patch("core.atomic.os.replace", flaky):
                try:
                    result = s.run()
                except OSError:
                    faulted = True
                else:
                    faulted = result.status is not RunStatus.COMPLETED
            with self.subTest(write=k):
                if s.state_file.exists():
                    load_run_state(s.state_file)  # nunca fica pela metade
                leftovers = [p.name for p in s.work.rglob("*") if p.is_file() and p.name.endswith(".tmp")]
                self.assertEqual(leftovers, [], "nenhum temporário abandonado")
                for _ in range(4):
                    result = s.run()
                    if result.status is RunStatus.COMPLETED:
                        break
                self.assertIs(result.status, RunStatus.COMPLETED)
                self.assertEqual({name: s.output_file(name).read_bytes() for name in ORDER}, expected)
                self.assertEqual(s.run().skipped, tuple(ORDER))
            if not faulted:
                completed_without_fault = True
                break
        self.assertTrue(completed_without_fault, "a varredura chegou além da última gravação da execução")
        self.assertGreater(k, 15, "a execução tem muitas gravações; a varredura precisa cobri-las")

    def test_a_crash_between_checkpoint_and_state_never_counts_the_stage_as_done(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        real_save = save_run_state
        calls = {"n": 0}

        def die_on_completed_validate(path, state):
            calls["n"] += 1
            record = state.stage("validate")
            if record.state is StageState.COMPLETED:
                raise Crash()
            return real_save(path, state)

        with mock.patch("core.orchestrator.save_run_state", die_on_completed_validate):
            with self.assertRaises(Crash):
                s.run()
        self.assertTrue(s.checkpoint_file("validate").exists(), "o checkpoint chegou a ser gravado")
        self.assertIs(load_run_state(s.state_file).stage("validate").state, StageState.RUNNING)
        result = s.run()
        self.assertIn("validate", s.calls)
        self.assertEqual(result.plan.of("validate").decision, Decision.REQUEUE)

    def test_a_truncated_state_file_from_a_non_atomic_writer_is_detected_not_adopted(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        s.run()
        text = s.state_file.read_text(encoding="utf-8")
        s.state_file.write_text(text[: len(text) // 2], encoding="utf-8")
        with self.assertRaises(RunStateError):
            s.run()


class SourceProtection(TempDirCase):
    def test_a_handler_that_mutates_a_read_only_source_makes_the_run_unsafe(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": mutating("analysis-input.txt")})
        result = s.run()
        self.assertIs(result.status, RunStatus.UNSAFE)
        self.assertEqual([(v.kind, v.path, v.root) for v in result.violations], [("MUTATED", "analysis-input.txt", "src")])
        self.assertEqual(s.calls, ["inventory", "discover"], "nada roda depois da violação")
        failure = result.state.stage("discover").failure
        self.assertEqual(failure.category, "SOURCE_MUTATION")
        self.assertFalse(s.checkpoint_file("discover").exists(), "resultado obtido sobre fonte mutada nunca é selado")
        self.assertEqual(load_run_state(s.state_file), result.state)
        self.assertEqual(result.state.violation_total, 1)

    def test_the_agent_never_reverts_what_it_did_not_write(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": mutating("analysis-input.txt", "changed by someone")})
        s.run()
        self.assertEqual(s.read_input("analysis-input"), "changed by someone")

    def test_source_mutation_takes_precedence_over_input_mutation(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"analyze": mutating("analysis-input.txt")})
        result = s.run()
        self.assertEqual(result.state.stage("analyze").failure.category, "SOURCE_MUTATION")
        self.assertIs(result.status, RunStatus.UNSAFE)

    def test_a_deleted_source_file_is_a_violation_too(self) -> None:
        def deleting(ctx, default) -> None:
            default(ctx)
            (ctx._runner.guard.local_root(SRC) / "inventory-source.txt").unlink()

        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": deleting})
        result = s.run()
        self.assertEqual([v.kind for v in result.violations], ["MISSING"])

    def test_a_new_file_appearing_in_a_read_only_source_is_a_violation(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": mutating("stray.txt")})
        result = s.run()
        self.assertEqual([(v.kind, v.path) for v in result.violations], [("UNDECLARED", "stray.txt")])

    def test_after_the_run_is_unsafe_a_new_run_takes_the_current_sources_as_the_new_truth(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, handlers={"discover": mutating("analysis-input.txt", "rules v9\n")})
        s.run()
        s.custom.clear()
        result = s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertIn("rules v9", s.output_file("analyze").read_text(encoding="utf-8"))
        again = result.plan.of("discover")
        self.assertEqual((again.decision, again.reason), (Decision.RETRY, "SOURCE_MUTATION_NOT_COUNTED"))
        self.assertEqual(result.state.stage("discover").attempts, 1, "a violação não gastou a única tentativa do estágio")

    def test_end_of_run_verification_flags_a_late_mutation_without_failing_stages(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, src_verify="RUN_END", handlers={"validate": mutating("inventory-source.txt")})
        result = s.run()
        self.assertIs(result.status, RunStatus.UNSAFE)
        self.assertEqual(s.calls, ORDER, "com RUN_END os estágios seguem até o fim")
        self.assertEqual({r.state for r in result.state.stages}, {StageState.COMPLETED})

    def test_verification_can_be_turned_off_for_read_only_roots(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, src_verify="NONE", handlers={"validate": mutating("stray.txt")})
        self.assertIs(s.run().status, RunStatus.COMPLETED)

    def test_agent_writes_only_inside_the_mutable_root(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        before = {p.name: p.read_bytes() for p in s.src.iterdir()}
        s.run()
        s.write_input("analysis-input", "rules v2\n")
        before["analysis-input.txt"] = (s.src / "analysis-input.txt").read_bytes()
        s.run()
        self.assertEqual({p.name: p.read_bytes() for p in s.src.iterdir()}, before)
        self.assertEqual(sorted(p.name for p in s.base.iterdir()), [SRC, WORK])


class FrozenSources(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, src_protection=Protection.FROZEN)

    def test_first_run_records_the_baseline_and_completes(self) -> None:
        result = self.s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertTrue((self.s.work / "state" / "protection" / "src.snapshot.json").is_file())
        (record,) = [r for r in result.state.protection if r.root == "src"]
        self.assertEqual((record.protection, record.entries), ("FROZEN", 2))

    def test_a_change_between_runs_stops_the_run_before_anything_executes(self) -> None:
        self.s.run()
        self.s.write_input("analysis-input", "evidence was edited\n")
        before = self.s.state_file.read_bytes()
        result = self.s.run()
        self.assertIs(result.status, RunStatus.UNSAFE)
        self.assertEqual(self.s.calls, [])
        self.assertEqual([(v.kind, v.path) for v in result.violations], [("MUTATED", "analysis-input.txt")])
        self.assertEqual(self.s.read_input("analysis-input"), "evidence was edited\n", "nada é revertido")
        self.assertEqual({r.state for r in result.state.stages}, {StageState.COMPLETED}, "nada foi invalidado por causa de uma fonte que pode ser restaurada")
        self.assertNotEqual(self.s.state_file.read_bytes(), before)

    def test_restoring_the_evidence_lets_the_run_continue_and_skip_everything(self) -> None:
        self.s.run()
        self.s.write_input("analysis-input", "evidence was edited\n")
        self.s.run()
        self.s.write_input("analysis-input", LINEAR_INPUTS["analysis-input"])
        result = self.s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual((self.s.calls, result.skipped), ([], tuple(ORDER)))
        self.assertEqual(result.state.violation_total, 0)

    def test_a_frozen_source_is_checked_after_every_stage(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="inflight", src_protection=Protection.FROZEN,
                     handlers={"analyze": mutating("inventory-source.txt")})
        result = s.run()
        self.assertIs(result.status, RunStatus.UNSAFE)
        self.assertEqual(s.calls, ["inventory", "discover", "analyze"])

    def test_a_new_file_in_a_frozen_source_is_a_violation_at_start(self) -> None:
        self.s.run()
        (self.s.src / "extra.txt").write_text("new evidence?")
        result = self.s.run()
        self.assertEqual([(v.kind, v.path) for v in result.violations], [("UNDECLARED", "extra.txt")])

    def test_a_leftover_running_stage_survives_an_unsafe_start_and_recovers_after_the_repair(self) -> None:
        def crashing(ctx, default) -> None:
            raise Crash()

        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="mix", src_protection=Protection.FROZEN, handlers={"validate": crashing})
        with self.assertRaises(Crash):
            s.run()
        s.write_input("analysis-input", "edited while the process was dead\n")
        s.custom.clear()
        unsafe = s.run()
        self.assertIs(unsafe.status, RunStatus.UNSAFE)
        self.assertEqual(s.calls, [])
        self.assertIs(load_run_state(s.state_file).stage("validate").state, StageState.RUNNING)
        s.write_input("analysis-input", LINEAR_INPUTS["analysis-input"])
        result = s.run()
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(s.calls, ["validate", "document"])

    def test_a_tampered_baseline_stops_everything(self) -> None:
        self.s.run()
        path = self.s.work / "state" / "protection" / "src.snapshot.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entries"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        from core.protection import ProtectionError

        with self.assertRaises(ProtectionError):
            self.s.run()
        self.assertEqual(self.s.calls, [])


class DeterminismAndPortability(TempDirCase):
    def test_two_workspaces_in_different_places_produce_byte_identical_state(self) -> None:
        a = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="one")
        b = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="deeper/and/elsewhere/two")
        a.run()
        b.run()
        self.assertEqual(a.state_file.read_bytes(), b.state_file.read_bytes())
        for name in ORDER:
            self.assertEqual(a.checkpoint_file(name).read_bytes(), b.checkpoint_file(name).read_bytes())
            self.assertEqual(a.output_file(name).read_bytes(), b.output_file(name).read_bytes())

    def test_a_moved_workspace_resumes_without_redoing_anything(self) -> None:
        a = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="here")
        a.run()
        moved = self.tmp / "moved" / "to" / "somewhere"
        shutil.copytree(a.base, moved)
        shutil.rmtree(a.base)
        calls: list[str] = []
        handlers = {name: (lambda ctx, _n=name: calls.append(_n)) for name in ORDER}
        result = Orchestrator(a.config(), handlers, {SRC: moved / SRC, WORK: moved / WORK}).run()
        self.assertEqual(calls, [])
        self.assertEqual(result.skipped, tuple(ORDER))

    def test_declaration_order_never_changes_execution_order_or_state(self) -> None:
        import itertools

        outcomes = set()
        for index, permutation in enumerate(itertools.permutations(branched_specs())):
            s = Scenario(self.tmp, list(permutation), BRANCHED_INPUTS, tag=f"perm-{index}")
            s.run()
            outcomes.add((tuple(s.calls), s.state_file.read_bytes()))
        self.assertEqual(len(outcomes), 1)

    def test_run_id_depends_only_on_the_workflow_identity(self) -> None:
        a = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="one")
        b = Scenario(self.tmp, linear_specs(), {"inventory-source": "other", "analysis-input": "other"}, tag="two")
        self.assertEqual(a.orchestrator().run_id, b.orchestrator().run_id)
        self.assertNotEqual(a.orchestrator().run_id, Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="three", workflow_version="2").orchestrator().run_id)

    def test_input_identity_follows_the_inputs_while_the_run_id_does_not(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)
        first = s.run().state
        s.write_input("analysis-input", "rules v2\n")
        second = s.run().state
        self.assertEqual(first.run_id, second.run_id)
        self.assertNotEqual(first.input_identity, second.input_identity)


class SyntheticSecondSoftware(TempDirCase):
    """O mesmo código serve a um software sem relação com o primeiro: nomes, entradas e grafo diferentes."""

    SPECS = [
        spec("ingest.catalog", inputs=["catalog"], outputs=("rows.json",)),
        spec("reconcile", deps=["ingest.catalog"], inputs=["ledger"], outputs=("diff.json", "notes.txt")),
        spec("publish-bundle", deps=["reconcile"], outputs=("bundle",)),
        spec("lint", deps=["ingest.catalog"], optional=True, outputs=("lint.txt",)),
    ]
    INPUTS = {"catalog": "SKU-1;SKU-2\n", "ledger": "L-1\n"}

    def default(self, scenario: Scenario):
        def handler(ctx) -> None:
            for out in ctx.stage.output_contracts:
                ctx.write_output(out, ("|".join(ctx.stage.depends_on) + out).encode())

        return handler

    def test_second_software_runs_resumes_and_invalidates_with_the_same_code(self) -> None:
        s = Scenario(self.tmp, self.SPECS, self.INPUTS, software_id="omega-crm", workflow_id="omega-sync", tag="omega")
        first = s.run()
        self.assertIs(first.status, RunStatus.COMPLETED)
        self.assertEqual(s.calls, ["ingest.catalog", "lint", "reconcile", "publish-bundle"])
        self.assertEqual(s.run().skipped, tuple(first.state.stage(n).stage_id for n in ("ingest.catalog", "lint", "reconcile", "publish-bundle")))
        s.write_input("ledger", "L-2\n")
        s.run()
        self.assertEqual(s.calls, ["reconcile", "publish-bundle"])

    def test_two_softwares_never_share_identity_or_state(self) -> None:
        a = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="alpha")
        b = Scenario(self.tmp, self.SPECS, self.INPUTS, software_id="omega-crm", workflow_id="omega-sync", tag="omega")
        ra, rb = a.run(), b.run()
        self.assertNotEqual(ra.state.run_id, rb.state.run_id)
        self.assertEqual({r.stage_id for r in rb.state.stages}, {"ingest.catalog", "reconcile", "publish-bundle", "lint"})
        self.assertNotIn("omega", a.state_file.read_text(encoding="utf-8"))

    def test_same_workflow_content_in_two_softwares_gets_two_identities(self) -> None:
        a = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="a", software_id="software-one")
        b = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="b", software_id="software-two")
        a.run()
        b.run()
        self.assertNotEqual(a.checkpoint_file("inventory").read_bytes(), b.checkpoint_file("inventory").read_bytes())
        self.assertNotEqual(a.orchestrator().run_id, b.orchestrator().run_id)

    def test_software_id_is_optional(self) -> None:
        s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, software_id=None)
        self.assertIs(s.run().status, RunStatus.COMPLETED)
        self.assertEqual(s.run().skipped, tuple(ORDER))


class FoundationWorkflowBridge(TempDirCase):
    """Os passos de um `agent-workflow` da Foundation executam como estágios, pelo mesmo motor."""

    def test_the_learn_workflow_runs_resumes_and_invalidates(self) -> None:
        from core.stage_graph import graph_from_agent_workflow
        from tests.helpers import REPO, load_json

        graph = graph_from_agent_workflow(load_json(REPO / "workflows" / "learn.workflow.json"))
        specs = [spec(item.stage_id, deps=item.depends_on, inputs=(["corpus"] if not item.depends_on else [])) for item in graph.stages]
        s = Scenario(self.tmp, specs, {"corpus": "documents v1\n"}, workflow_id="learn")
        first = s.run()
        self.assertIs(first.status, RunStatus.COMPLETED)
        self.assertEqual(s.calls, list(graph.order))
        self.assertEqual(len(s.calls), 11)
        self.assertEqual(s.run().skipped, graph.order)
        s.write_input("corpus", "documents v2\n")
        s.run()
        self.assertEqual(s.calls, list(graph.order))


class EnvironmentCapabilities(TempDirCase):
    """Capacidades opcionais do ambiente (Git etc.): informação, nunca requisito."""

    def setUp(self) -> None:
        super().setUp()
        self.s = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS)

    def test_without_any_probe_the_run_knows_nothing_about_the_environment(self) -> None:
        result = self.s.run()
        self.assertIsNone(result.state.environment)
        self.assertNotIn("environment", json.loads(self.s.state_file.read_text(encoding="utf-8")))

    def test_a_probe_is_captured_at_start_and_end_with_its_drift(self) -> None:
        class Probe:
            def __init__(self) -> None:
                self.n = 0

            def capture(self) -> dict:
                self.n += 1
                return {"revision": self.n}

            def compare(self, before: dict, after: dict) -> list:
                return ["REVISION_CHANGED"] if before != after else []

        result = self.s.run(probes={"vcs": Probe()})
        self.assertEqual(result.state.environment, {"vcs": {"at_start": {"revision": 1}, "at_end": {"revision": 2}, "drift": ["REVISION_CHANGED"]}})
        self.assertIs(result.status, RunStatus.COMPLETED, "deriva é informação, não veredito")

    def test_a_failing_probe_never_breaks_the_run(self) -> None:
        class Broken:
            def capture(self) -> dict:
                raise RuntimeError("sem ambiente")

            def compare(self, before: dict, after: dict) -> list:
                return []

        result = self.s.run(probes={"vcs": Broken()})
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state.environment, {"vcs": {"probe_error": "RuntimeError"}})

    def test_the_git_probe_with_git_missing_is_just_an_unavailable_capability(self) -> None:
        from core.gitstate import NOT_INSTALLED, GitStateProbe, GitUnavailable

        def no_git(args, cwd):
            raise GitUnavailable(NOT_INSTALLED)

        result = self.s.run(probes={"git": GitStateProbe(self.s.base, runner=no_git)})
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state.environment["git"]["at_start"], {"available": False, "reason": "NOT_INSTALLED"})
        self.assertEqual(result.state.environment["git"]["drift"], [])

    def test_the_real_git_probe_on_a_plain_directory_does_not_matter_to_the_run(self) -> None:
        from core.gitstate import GitStateProbe

        result = self.s.run(probes={"git": GitStateProbe(self.s.base)})
        self.assertIs(result.status, RunStatus.COMPLETED)
        self.assertIn(result.state.environment["git"]["at_start"]["available"], (True, False))

    def test_probes_do_not_change_what_gets_skipped_or_any_checkpoint(self) -> None:
        plain = Scenario(self.tmp, linear_specs(), LINEAR_INPUTS, tag="plain")
        plain.run()

        class Probe:
            def capture(self) -> dict:
                return {"x": 1}

            def compare(self, before: dict, after: dict) -> list:
                return []

        self.s.run(probes={"vcs": Probe()})
        for name in ORDER:
            self.assertEqual(self.s.checkpoint_file(name).read_bytes(), plain.checkpoint_file(name).read_bytes())
        again = self.s.run()
        self.assertEqual(again.skipped, tuple(ORDER), "retomar sem sonda depois de usar uma sonda não invalida nada")

    def test_probe_outputs_must_be_portable(self) -> None:
        class Leaky:
            def capture(self) -> dict:
                return {"where": "C:\\Users\\someone\\repo"}

            def compare(self, before: dict, after: dict) -> list:
                return []

        with self.assertRaises(RunStateError):
            self.s.run(probes={"vcs": Leaky()})


class ContextAccess(TempDirCase):
    def test_context_exposes_stage_attempt_and_a_private_copy_of_the_parameters(self) -> None:
        seen: dict = {}

        def inspect(ctx, default) -> None:
            seen["stage"] = ctx.stage.stage_id
            seen["attempt"] = ctx.attempt
            params = ctx.parameters
            params["depth"] = 99
            seen["again"] = ctx.parameters
            seen["path"] = ctx.input_path("one")
            default(ctx)

        s = Scenario(self.tmp, [spec("only", inputs=["one"])], {"one": "1"}, params={"only": {"depth": 2}}, handlers={"only": inspect})
        s.run()
        self.assertEqual((seen["stage"], seen["attempt"], seen["again"]), ("only", 1, {"depth": 2}))
        self.assertEqual(seen["path"], (s.src / "one.txt").resolve())
        self.assertEqual(s.config().parameters_of("only"), {"depth": 2})

    def test_written_outputs_land_only_under_the_canonical_stage_folder(self) -> None:
        s = Scenario(self.tmp, [spec("only", outputs=("a.json", "b"))])
        s.run()
        written = sorted(p.relative_to(s.work).as_posix() for p in (s.work / "outputs").rglob("*") if p.is_file())
        self.assertEqual(written, ["outputs/only/a.json", "outputs/only/b"])

    def test_json_outputs_use_the_canonical_pretty_form(self) -> None:
        def writer(ctx, default) -> None:
            ctx.write_output_json("result", {"b": 1, "a": [1, 2]})

        s = Scenario(self.tmp, [spec("only")], handlers={"only": writer})
        s.run()
        self.assertEqual(s.output_file("only").read_bytes(), b'{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1\n}\n')
        self.assertEqual(hashlib.sha256(s.output_file("only").read_bytes()).hexdigest(), json.loads(s.checkpoint_file("only").read_text())["outputs"][0]["sha256"])


if __name__ == "__main__":
    unittest.main()
