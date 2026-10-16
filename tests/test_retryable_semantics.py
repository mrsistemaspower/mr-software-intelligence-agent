"""Semântica de `retryable` no motor de estágios: falha determinística não gasta tentativa; falha temporária segue a RetryPolicy.

Antes da Extração 4 o motor repetia TODA falha técnica até `max_attempts`, ignorando `result.retryable` do AI_RECHECK. Aqui está a
regressão. O que NÃO muda: o que é repetível continua repetindo, qualquer exceção comum de tratador continua repetível, e a repetição
epistêmica (um recheck válido, mesmo `INCONCLUSIVE`, nunca é repetido) segue como na Extração 3.
"""
from __future__ import annotations

import json
import unittest

from adapters.models.scripted import ScriptedModelAdapter
from core.model_adapter import ModelFailure
from core.orchestrator import StageFailure
from core.resume import Decision
from core.schema_lite import Registry, Validator
from core.stage_state import FailureInfo, StageState, TransitionError
from core.ai_recheck import AIRecheckRequest, RecheckClaim
from core.model_task import EvidenceRef
from core.recheck_stage import input_locator
from tests.helpers import TempDirCase
from tests.recheck_helpers import DOC, DOC_REF, inconclusive, resolved
from tests.test_recheck_stage import ALL_DONE, Flow
from tests.wf_helpers import Scenario, retrying, spec

VALIDATOR = Validator(Registry())
MANY = 5  # tentativas de sobra: o que importa é quantas foram USADAS


def failed_twice_in_a_row(flow: Flow):  # noqa: ANN201
    """Duas execuções seguidas. Devolve (primeira, segunda): a segunda mostra o que a retomada decide sobre a falha."""
    first = flow.run()
    second = flow.run()
    return first, second


class RetryableFailureRetries(TempDirCase):
    def test_a_temporary_adapter_timeout_is_retried_and_may_succeed(self) -> None:
        adapter = ScriptedModelAdapter([TimeoutError("lento"), resolved()])
        flow = Flow(self.tmp, adapter, attempts=MANY)
        first, second = failed_twice_in_a_row(flow)
        self.assertEqual(flow.stage_record(first, "recheck").attempts, 1)
        self.assertEqual(second.plan.of("recheck").decision, Decision.RETRY)
        self.assertEqual(flow.states(second), ALL_DONE)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(flow.output("recheck", "result")["provenance"]["attempt"], 2)

    def test_a_failure_the_adapter_calls_retryable_is_retried_up_to_the_limit_and_no_further(self) -> None:
        adapter = ScriptedModelAdapter([ModelFailure("UNAVAILABLE", "fora do ar", True)] * 4)
        flow = Flow(self.tmp, adapter, attempts=3)
        for expected in (1, 2, 3):
            result = flow.run()
            self.assertEqual(flow.stage_record(result, "recheck").attempts, expected)
        fourth = flow.run()
        self.assertEqual(fourth.plan.of("recheck").decision, Decision.EXHAUSTED)
        self.assertEqual(fourth.plan.of("recheck").reason, "ATTEMPTS_EXHAUSTED")
        self.assertEqual(adapter.calls, 3, "o limite da RetryPolicy continua valendo para o que é repetível")

    def test_missing_evidence_may_come_back_so_it_is_retryable(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, attempts=2, declare_evidence=False)
        first, second = failed_twice_in_a_row(flow)
        failure = flow.stage_record(first, "recheck").failure
        self.assertEqual(failure.category, "INPUT_UNAVAILABLE")
        self.assertTrue(failure.retryable)
        self.assertEqual(second.plan.of("recheck").decision, Decision.RETRY)

    def test_an_ordinary_handler_exception_stays_retryable(self) -> None:
        scenario = Scenario(
            self.tmp, [retrying("a", MANY)], {}, handlers={"a": self.boom},
        )
        results = [scenario.run() for _ in range(MANY)]
        self.assertEqual([r.state.stage("a").attempts for r in results], list(range(1, MANY + 1)))
        self.assertTrue(all(r.state.stage("a").failure.retryable for r in results))
        self.assertEqual(scenario.run().plan.of("a").decision, Decision.EXHAUSTED)

    @staticmethod
    def boom(ctx, default) -> None:  # noqa: ANN001
        raise ValueError("falha comum")

    def test_a_stage_failure_is_retryable_by_default(self) -> None:
        self.assertTrue(StageFailure("x").retryable)
        def fail_again(ctx, default) -> None:  # noqa: ANN001
            raise StageFailure("de novo")

        scenario = Scenario(self.tmp, [retrying("a", 3)], {}, handlers={"a": fail_again})
        scenario.run()
        self.assertEqual(scenario.run().plan.of("a").decision, Decision.RETRY)


class NonRetryableFailureStopsAtOnce(TempDirCase):
    def assert_one_attempt(self, flow: Flow, adapter: ScriptedModelAdapter, expected_kind: str, *, calls: int = 0) -> None:
        first, second = failed_twice_in_a_row(flow)
        failure = flow.stage_record(first, "recheck").failure
        self.assertIn(expected_kind, failure.message)
        self.assertFalse(failure.retryable)
        self.assertEqual(flow.stage_record(first, "recheck").attempts, 1)
        plan = second.plan.of("recheck")
        self.assertEqual((plan.decision, plan.reason), (Decision.EXHAUSTED, "NON_RETRYABLE_FAILURE"))
        self.assertEqual(flow.stage_record(second, "recheck").attempts, 1, "nenhuma tentativa foi gasta depois da primeira")
        self.assertIs(flow.stage_record(second, "recheck").state, StageState.FAILED)
        self.assertEqual(adapter.calls, calls)
        self.assertEqual(second.status.value, "FAILED")
        self.assertEqual(flow.states(second)["document"], "BLOCKED", "quem depende do estágio falho fica bloqueado, não repetido")

    def test_a_non_retryable_adapter_failure_stops_at_once_even_with_attempts_to_spare(self) -> None:
        adapter = ScriptedModelAdapter([ModelFailure("REFUSED", "recusou", False), resolved()])
        self.assert_one_attempt(Flow(self.tmp, adapter, attempts=MANY), adapter, "ADAPTER_FAILURE", calls=1)

    def test_provider_policy_rejection_consumes_only_one_attempt(self) -> None:
        for sensitive in (True, None):
            adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
            flow = Flow(self.tmp, adapter, attempts=MANY, tag=f"ws-{sensitive}", max_boundary="EXTERNAL", sensitive=sensitive)
            with self.subTest(sensitive=sensitive):
                self.assert_one_attempt(flow, adapter, "SENSITIVE_EVIDENCE_EXTERNAL")

    def test_an_adapter_beyond_the_data_boundary_consumes_only_one_attempt(self) -> None:
        adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        self.assert_one_attempt(Flow(self.tmp, adapter, attempts=MANY), adapter, "BOUNDARY_EXCEEDED")

    def test_an_adapter_missing_a_capability_consumes_only_one_attempt(self) -> None:
        adapter = ScriptedModelAdapter([resolved()], capabilities=("long-context",))
        self.assert_one_attempt(Flow(self.tmp, adapter, attempts=MANY), adapter, "MISSING_CAPABILITY")

    def test_the_ineligible_adapter_is_never_called_and_its_failure_is_the_executors(self) -> None:
        adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        flow = Flow(self.tmp, adapter, attempts=MANY)
        failure = flow.stage_record(flow.run(), "recheck").failure
        self.assertEqual(failure.category, "HANDLER_ERROR")
        self.assertIn("ADAPTER_INELIGIBLE", failure.message)
        self.assertEqual(adapter.calls, 0)

    def test_malformed_static_configuration_consumes_only_one_attempt(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, attempts=MANY, declare_parameters=False)
        first, second = failed_twice_in_a_row(flow)
        self.assertIn("stage_parameters", flow.stage_record(first, "recheck").failure.message)
        self.assertEqual(second.plan.of("recheck").reason, "NON_RETRYABLE_FAILURE")
        self.assertEqual(flow.stage_record(second, "recheck").attempts, 1)
        self.assertEqual(adapter.calls, 0)

    def test_a_request_that_cannot_be_read_consumes_only_one_attempt(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, attempts=MANY)

        def analyze_with_a_broken_request(ctx, default) -> None:  # noqa: ANN001
            ctx.write_output_json("analysis", {})
            ctx.write_output_json("request", {"kind": "ai-recheck-request"})  # fora do contrato do pedido

        flow.scenario.custom["analyze"] = analyze_with_a_broken_request
        first, second = failed_twice_in_a_row(flow)
        self.assertIn("pedido de AI_RECHECK inválido", flow.stage_record(first, "recheck").failure.message)
        self.assertEqual(second.plan.of("recheck").reason, "NON_RETRYABLE_FAILURE")
        self.assertEqual(flow.stage_record(second, "recheck").attempts, 1)
        self.assertEqual(adapter.calls, 0)

    def test_exceeding_the_round_limit_is_deterministic_and_stops_at_once(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])

        def second_round(content: bytes) -> AIRecheckRequest:
            item = EvidenceRef.of(DOC_REF, content, "documentation", contains_sensitive_data=False, locator=input_locator("evidence-doc"))
            return AIRecheckRequest(
                subject_ref="job:nightly-retention", subject_kind="documentation", ambiguity="Prazo ambíguo.", reason="Confiança baixa.",
                claims=(RecheckClaim("c1", "Os registros são mantidos por 90 dias."),), evidence=(item,), max_boundary="LOCAL", round=2,
            )

        self.assert_one_attempt(Flow(self.tmp, adapter, attempts=MANY, make_request=second_round), adapter, "ROUND_LIMIT")

    def test_a_handler_can_declare_its_own_failure_non_retryable(self) -> None:
        def fail_for_good(ctx, default) -> None:  # noqa: ANN001
            raise StageFailure("configuração inválida", retryable=False)

        scenario = Scenario(self.tmp, [retrying("a", MANY)], {}, handlers={"a": fail_for_good})
        first = scenario.run()
        self.assertFalse(first.state.stage("a").failure.retryable)
        second = scenario.run()
        self.assertEqual((second.plan.of("a").decision, second.plan.of("a").reason), (Decision.EXHAUSTED, "NON_RETRYABLE_FAILURE"))
        self.assertEqual(second.state.stage("a").attempts, 1)
        self.assertEqual(scenario.calls, [], "o tratador nem foi chamado de novo")

    def test_retryable_must_be_a_boolean(self) -> None:
        for bad in (0, 1, "false", None):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                StageFailure("x", retryable=bad)  # type: ignore[arg-type]


class ANonRetryableFailureIsNotForever(TempDirCase):
    """O mesmo erro com entradas novas não é o mesmo erro: a falha antiga não vale mais e a vida recomeça."""

    def test_changed_evidence_gives_a_new_life_after_a_non_retryable_failure(self) -> None:
        refused = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        flow = Flow(self.tmp, refused, attempts=MANY)
        flow.run()
        self.assertEqual(flow.run().plan.of("recheck").reason, "NON_RETRYABLE_FAILURE")
        flow.scenario.write_input("evidence-doc", DOC.decode("utf-8") + "Linha nova.\n")
        again = flow.run()
        # A análise que alimenta o recheck foi refeita; a falha antiga não vale mais e o recheck roda de novo, com contagem nova.
        self.assertEqual((again.plan.of("recheck").decision, again.plan.of("recheck").reason), (Decision.RUN, "UPSTREAM_REBUILDING"))
        self.assertIn("recheck", flow.calls)
        self.assertEqual(flow.stage_record(again, "recheck").attempts, 1, "a contagem recomeçou")

    def test_replacing_the_adapter_after_an_ineligible_one_runs_again(self) -> None:
        refused = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        flow = Flow(self.tmp, refused, attempts=MANY)
        flow.run()
        self.assertEqual(flow.run().plan.of("recheck").reason, "NON_RETRYABLE_FAILURE")
        eligible = ScriptedModelAdapter([resolved()], adapter_id="scripted-local")
        replaced = Flow(self.tmp, eligible)
        result = replaced.run()
        self.assertEqual(replaced.states(result), ALL_DONE)
        self.assertEqual(eligible.calls, 1)


class EpistemicRetryIsUntouched(TempDirCase):
    """Repetir por falha técnica e repetir a análise por dúvida são coisas diferentes. Esta correção só mexe na primeira."""

    def test_a_valid_inconclusive_is_still_not_repeated(self) -> None:
        adapter = ScriptedModelAdapter([inconclusive(), resolved()])
        flow = Flow(self.tmp, adapter, attempts=MANY)
        flow.run()
        again = flow.run()
        self.assertEqual(flow.calls, [])
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(again.plan.with_decision(Decision.RETRY, Decision.EXHAUSTED), ())

    def test_a_non_retryable_failure_is_not_turned_into_a_verdict(self) -> None:
        adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        flow = Flow(self.tmp, adapter, attempts=MANY)
        result = flow.run()
        self.assertEqual(flow.states(result)["recheck"], "FAILED")
        self.assertFalse(flow.scenario.output_file("recheck", "result").exists(), "falha técnica não grava resultado")

    def test_the_executor_still_marks_what_is_deterministic(self) -> None:
        from tests.test_recheck_engine import run

        for script, kind, retryable in (
            ([TimeoutError("x")], "ADAPTER_FAILURE", True),
            ([ModelFailure("REFUSED", "x", False)], "ADAPTER_FAILURE", False),
        ):
            result = run(script)[0]
            with self.subTest(kind=kind, retryable=retryable):
                self.assertEqual((result.failure["kind"], result.failure["retryable"]), (kind, retryable))


class FailureRecordFormat(unittest.TestCase):
    def info(self, **kwargs: object) -> FailureInfo:
        return FailureInfo("s", "HANDLER_ERROR", "falhou", 1, **kwargs)  # type: ignore[arg-type]

    def test_the_default_is_retryable_and_the_stored_form_is_unchanged(self) -> None:
        info = self.info()
        self.assertTrue(info.retryable)
        self.assertEqual(set(info.to_dict()), {"stage_id", "category", "message", "attempt", "checkpoint_state"})

    def test_only_false_is_stored(self) -> None:
        data = self.info(retryable=False).to_dict()
        self.assertIs(data["retryable"], False)
        self.assertEqual(FailureInfo.from_dict(data), self.info(retryable=False))

    def test_round_trip_of_both_forms(self) -> None:
        for info in (self.info(), self.info(retryable=False)):
            self.assertEqual(FailureInfo.from_dict(info.to_dict()), info)

    def test_a_record_written_before_this_field_still_loads_as_retryable(self) -> None:
        old = {"stage_id": "s", "category": "HANDLER_ERROR", "message": "x", "attempt": 2, "checkpoint_state": "ABSENT"}
        self.assertTrue(FailureInfo.from_dict(old).retryable)

    def test_a_redundant_or_wrong_retryable_is_refused(self) -> None:
        base = self.info().to_dict()
        for bad in (True, 0, "false", None):
            with self.subTest(bad=bad), self.assertRaises(TransitionError):
                FailureInfo.from_dict({**base, "retryable": bad})
        with self.assertRaises(TransitionError):
            FailureInfo.from_dict({**base, "surprise": 1})
        with self.assertRaises(TransitionError):
            self.info(retryable=1)  # type: ignore[arg-type]

    def test_the_run_state_schema_accepts_both_forms_and_refuses_the_redundant_one(self) -> None:
        schema = {"$ref": "urn:mr-software-intelligence:schema:run-state:1#/$defs/failure"}
        base = self.info().to_dict()
        self.assertEqual(VALIDATOR.validate(base, schema), [])
        self.assertEqual(VALIDATOR.validate({**base, "retryable": False}, schema), [])
        self.assertTrue(VALIDATOR.validate({**base, "retryable": True}, schema))


class StoredStateCarriesTheFlag(TempDirCase):
    def test_the_state_file_records_retryable_false_and_validates(self) -> None:
        adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        flow = Flow(self.tmp, adapter, attempts=MANY)
        flow.run()
        data = json.loads(flow.scenario.state_file.read_text(encoding="utf-8"))
        failure = next(item["failure"] for item in data["stages"] if item["stage_id"] == "recheck")
        self.assertIs(failure["retryable"], False)
        self.assertEqual(VALIDATOR.validate(data, "run-state"), [])

    def test_a_retryable_failure_leaves_the_stored_form_exactly_as_before(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([TimeoutError("lento")]), attempts=MANY)
        flow.run()
        data = json.loads(flow.scenario.state_file.read_text(encoding="utf-8"))
        failure = next(item["failure"] for item in data["stages"] if item["stage_id"] == "recheck")
        self.assertEqual(set(failure), {"stage_id", "category", "message", "attempt", "checkpoint_state"})


if __name__ == "__main__":
    unittest.main()
