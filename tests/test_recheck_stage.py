"""AI_RECHECK como estágio do orquestrador da Extração 2: inventário -> análise -> AI_RECHECK -> rota, com retomada e repetição."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.helpers import TempDirCase
from tests.recheck_helpers import DDL, DDL_REF, DOC, DOC_REF, finding, human_decision, inconclusive, resolved, source_gap
from tests.wf_helpers import Scenario, spec
from adapters.models.scripted import RuleBasedModelAdapter, ScriptedModelAdapter
from core.ai_recheck import AIRecheckRequest, AIRecheckResult, PriorInterpretation, RecheckClaim, RecheckPolicy, human_review_candidate, source_gap_entry
from core.model_task import EVIDENCE_KEY, EvidenceRef
from core.recheck_stage import PARAMETER_KEY, AIRecheckStage, input_locator, upstream_locator
from core.resume import Decision
from core.serialization import loads_strict
from core.stage_graph import RetryPolicy
from core.stage_state import StageState
from core.schema_lite import Registry, Validator

VALIDATOR = Validator(Registry())
EVIDENCE_INPUT = "evidence-doc"


class Names:
    """Nomes dos estágios de um fluxo: o executor não conhece nenhum deles."""

    def __init__(self, inventory="inventory", analyze="analyze", recheck="recheck", route="route", final="document") -> None:
        self.inventory, self.analyze, self.recheck, self.route, self.final = inventory, analyze, recheck, route, final


class Flow:
    """inventory -> analyze -> recheck -> route -> document, com um adaptador injetado. Sintético e sem software real."""

    def __init__(
        self, tmp: Path, adapter, *, attempts: int = 1, policy: "RecheckPolicy | None" = None, doc: bytes = DOC, stale_doc: "bytes | None" = None,
        declare_evidence: bool = True, tag: str = "ws", names: "Names | None" = None, evidence_input: str = EVIDENCE_INPUT, ref: str = DOC_REF,
        software_id: str = "synthetic-one", workflow_id: str = "synthetic-flow", make_request=None, max_boundary: str = "LOCAL",
        sensitive: "bool | None" = False, declare_parameters: bool = True, final_outputs: "tuple[str, ...]" = ("result",),
    ) -> None:
        self.n = names or Names()
        self.adapter = adapter
        self.stage = AIRecheckStage(adapter, request_upstream=(self.n.analyze, "request"), policy=policy)
        self.ref, self.evidence_input, self.stale_doc, self.max_boundary, self.sensitive = ref, evidence_input, stale_doc, max_boundary, sensitive
        self.make_request = make_request or self.default_request
        evidence_inputs = [evidence_input] if declare_evidence else []
        n = self.n
        specs = [
            spec(n.inventory, inputs=[evidence_input]),
            spec(n.analyze, deps=[n.inventory], inputs=[evidence_input], outputs=["analysis", "request"]),
            spec(n.recheck, deps=[n.analyze], inputs=evidence_inputs, outputs=["result"], retry=RetryPolicy(attempts)),
            spec(n.route, deps=[n.analyze, n.recheck], outputs=["routed"]),
            spec(n.final, deps=[n.route], outputs=list(final_outputs)),
        ]
        params = {n.recheck: self.stage.stage_parameters()} if declare_parameters else {}
        self.scenario = Scenario(
            tmp, specs, {evidence_input: doc.decode("utf-8")}, tag=tag, params=params, software_id=software_id, workflow_id=workflow_id,
            handlers={
                n.analyze: self.analyze_handler,
                n.recheck: lambda ctx, default: self.stage(ctx),
                n.route: self.route_handler,
                n.final: self.final_handler,
            },
        )

    # ---- estágios sintéticos (o que um fluxo real escreveria) ----
    def default_request(self, content: bytes) -> AIRecheckRequest:
        item = EvidenceRef.of(self.ref, content, "documentation", contains_sensitive_data=self.sensitive, locator=input_locator(self.evidence_input))
        return AIRecheckRequest(
            subject_ref="job:nightly-retention", subject_kind="documentation", ambiguity="Prazo ambíguo.", reason="Confiança baixa.",
            claims=(RecheckClaim("c1", "Os registros são mantidos por 90 dias.", 0.3),), evidence=(item,), max_boundary=self.max_boundary,
            prior_interpretation=PriorInterpretation("Provavelmente 30 dias.", "INFERRED", 0.3), software_id="synthetic-one",
        )

    def analyze_handler(self, ctx, default) -> None:  # noqa: ANN001
        current = ctx.read_input(self.evidence_input)
        pinned = self.stale_doc if self.stale_doc is not None else current  # um pedido criado antes de a evidência mudar
        ctx.write_output_json("analysis", {"ambiguous": True, "claims": ["c1"]})
        ctx.write_output_json("request", self.make_request(pinned).to_dict())

    def route_handler(self, ctx, default) -> None:  # noqa: ANN001
        request = AIRecheckRequest.from_dict(loads_strict(ctx.read_upstream(self.n.analyze, "request")))
        result = AIRecheckResult.from_dict(loads_strict(ctx.read_upstream(self.n.recheck, "result")))
        routed: dict = {"route": result.route, "proceeds": result.proceeds, "status": result.status}
        if result.proceeds:
            routed["facts"] = list(result.reusable_findings)
        elif result.route == "SOURCE_GAP":
            routed["source_backlog"] = [source_gap_entry(request, result)]
        elif result.route in ("HUMAN_DECISION", "HUMAN_VALIDATION"):
            routed["human_review_candidates"] = [human_review_candidate(request, result)]
        ctx.write_output_json("routed", routed)

    def final_handler(self, ctx, default) -> None:  # noqa: ANN001
        routed = loads_strict(ctx.read_upstream(self.n.route, "routed"))
        if routed["proceeds"]:
            outcome = {"document": "built", "facts": [item["statement"] for item in routed["facts"]]}
        else:
            outcome = {"document": "held", "reason": routed["route"]}
        ctx.write_output_json("result", outcome)

    # ---- acesso ----
    def run(self):  # noqa: ANN201
        return self.scenario.run()

    def stage_record(self, result, stage: str):  # noqa: ANN001, ANN201
        return result.state.stage(stage)

    def states(self, result) -> dict:  # noqa: ANN001
        return self.scenario.states(result)

    def output(self, stage: str, name: str) -> dict:
        return json.loads(self.scenario.output_file(stage, name).read_text(encoding="utf-8"))

    @property
    def calls(self) -> list:
        return list(self.scenario.calls)


ALL_DONE = {"inventory": "COMPLETED", "analyze": "COMPLETED", "recheck": "COMPLETED", "route": "COMPLETED", "document": "COMPLETED"}


class CaseAResolved(TempDirCase):
    """A análise inicial é ambígua; AI_RECHECK resolve; o fluxo continua."""

    def test_the_workflow_continues_with_the_reusable_ai_result(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter)
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        self.assertEqual(result.status.value, "COMPLETED")
        self.assertEqual(flow.calls, ["inventory", "analyze", "recheck", "route", "document"])
        routed = flow.output("route", "routed")
        self.assertEqual((routed["route"], routed["proceeds"]), ("REUSE", True))
        self.assertEqual(flow.output("document", "result")["document"], "built")
        self.assertEqual(adapter.calls, 1)

    def test_the_stored_result_is_the_validated_contract_artifact(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]))
        flow.run()
        data = flow.output("recheck", "result")
        self.assertEqual(VALIDATOR.validate(data, "ai-recheck-result"), [])
        self.assertEqual(AIRecheckResult.from_dict(data).status, "RESOLVED")
        self.assertEqual({item["status"] for item in data["findings"]}, {"INFERRED"})
        self.assertEqual(data["provenance"]["attempt"], 1)
        self.assertTrue(data["provenance"]["evidence_verified"])

    def test_the_stage_is_a_plain_handler_of_the_existing_engine(self) -> None:
        stage = AIRecheckStage(ScriptedModelAdapter([]), request_input="request")
        self.assertTrue(callable(stage))
        from core.orchestrator import Orchestrator

        self.assertEqual(Orchestrator.__module__, "core.orchestrator")
        self.assertNotIn("Orchestrator", AIRecheckStage.__dict__)

    def test_the_checkpoint_seals_the_recheck_output(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]))
        flow.run()
        checkpoint = json.loads(flow.scenario.checkpoint_file("recheck").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in checkpoint["outputs"]], ["result"])
        self.assertIn(EVIDENCE_INPUT, checkpoint["inputs"], "a evidência autorizada é entrada do checkpoint")


class CaseBSourceGap(TempDirCase):
    """AI_RECHECK diz SOURCE_GAP: o fluxo roteia para o backlog e não segue como resolvido."""

    def test_the_gap_is_routed_to_the_backlog_with_what_is_missing(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([source_gap(recommended="runtime")]))
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE, "um veredito de recheck não é falha de estágio")
        routed = flow.output("route", "routed")
        self.assertEqual((routed["route"], routed["proceeds"]), ("SOURCE_GAP", False))
        entry = routed["source_backlog"][0]
        self.assertEqual(entry["missing"], ["O valor configurado em produção."])
        self.assertEqual(entry["insufficient_because"], "O documento só descreve o padrão.")
        self.assertEqual(entry["needed_source"], "runtime")
        self.assertNotIn("facts", routed)
        self.assertNotIn("human_review_candidates", routed)

    def test_the_workflow_does_not_continue_as_if_it_were_resolved(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([source_gap()]))
        flow.run()
        self.assertEqual(flow.output("document", "result"), {"document": "held", "reason": "SOURCE_GAP"})


class CaseCHumanDecision(TempDirCase):
    """AI_RECHECK diz HUMAN_DECISION: vira candidato a escalonamento; ninguém é perguntado aqui."""

    def test_the_decision_becomes_a_human_review_candidate(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([human_decision()]))
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        routed = flow.output("route", "routed")
        self.assertEqual((routed["route"], routed["proceeds"]), ("HUMAN_DECISION", False))
        candidate = routed["human_review_candidates"][0]
        self.assertEqual((candidate["route"], candidate["state"]), ("HUMAN_DECISION", "OPEN"))
        self.assertEqual(candidate["options"], ["30 dias", "90 dias"])
        self.assertNotIn("answer", candidate)
        self.assertEqual(flow.output("document", "result")["document"], "held")

    def test_an_inconclusive_verdict_follows_the_policy_fallback_to_a_validation_candidate(self) -> None:
        adapter = ScriptedModelAdapter([inconclusive()])
        flow = Flow(self.tmp, adapter, attempts=3)
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        routed = flow.output("route", "routed")
        self.assertEqual((routed["status"], routed["route"]), ("INCONCLUSIVE", "HUMAN_VALIDATION"))
        self.assertEqual(routed["human_review_candidates"][0]["hypothesis"], "Provavelmente 30 dias.")
        self.assertEqual(adapter.calls, 1, "INCONCLUSIVE válido não é repetido, mesmo havendo tentativas sobrando")


class TechnicalFailureAndRetry(TempDirCase):
    def test_a_technical_failure_fails_the_stage_and_stops_the_workflow(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([TimeoutError("lento")]))
        result = flow.run()
        self.assertEqual(result.status.value, "FAILED")
        self.assertEqual(flow.states(result)["recheck"], "FAILED")
        self.assertEqual(flow.states(result)["document"], "PENDING")
        failure = flow.stage_record(result, "recheck").failure
        self.assertEqual(failure.category, "HANDLER_ERROR")
        self.assertIn("ADAPTER_FAILURE", failure.message)
        self.assertNotIn("route", flow.calls)

    def test_the_stage_engine_retries_in_a_new_run_and_the_attempt_is_recorded(self) -> None:
        adapter = ScriptedModelAdapter([TimeoutError("lento"), resolved()])
        flow = Flow(self.tmp, adapter, attempts=2)
        first = flow.run()
        self.assertEqual(flow.states(first)["recheck"], "FAILED")
        self.assertEqual(flow.stage_record(first, "recheck").attempts, 1)
        second = flow.run()
        self.assertEqual(flow.states(second), ALL_DONE)
        self.assertEqual(second.plan.with_decision(Decision.RETRY), ("recheck",))
        self.assertEqual(flow.output("recheck", "result")["provenance"]["attempt"], 2)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual([task.attempt for task in adapter.tasks], [1, 2])
        self.assertEqual(adapter.tasks[0].task_id, adapter.tasks[1].task_id, "a repetição técnica é a MESMA tarefa")
        self.assertEqual(flow.output("document", "result")["document"], "built")

    def test_retries_are_bounded_and_never_loop(self) -> None:
        adapter = ScriptedModelAdapter([TimeoutError("a"), TimeoutError("b"), resolved()])
        flow = Flow(self.tmp, adapter, attempts=2)
        flow.run()
        second = flow.run()
        self.assertEqual(flow.states(second)["recheck"], "FAILED")
        third = flow.run()
        self.assertEqual(third.plan.with_decision(Decision.EXHAUSTED), ("recheck",))
        self.assertEqual(adapter.calls, 2, "esgotadas as tentativas, o adaptador não é chamado de novo")
        self.assertEqual(third.status.value, "FAILED")

    def test_a_valid_inconclusive_is_not_retried_by_the_stage_engine(self) -> None:
        adapter = ScriptedModelAdapter([inconclusive(), resolved()])
        flow = Flow(self.tmp, adapter, attempts=5)
        flow.run()
        again = flow.run()
        self.assertEqual(flow.calls, [], "tudo pulado pelo checkpoint")
        self.assertEqual(again.plan.with_decision(Decision.RETRY, Decision.RUN, Decision.REQUEUE), ())
        self.assertEqual(adapter.calls, 1)

    def test_an_adapter_that_is_not_eligible_fails_without_being_called(self) -> None:
        adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
        flow = Flow(self.tmp, adapter)
        result = flow.run()
        failure = flow.stage_record(result, "recheck").failure
        self.assertEqual(flow.states(result)["recheck"], "FAILED")
        self.assertIn("ADAPTER_INELIGIBLE", failure.message)
        self.assertEqual(adapter.calls, 0)

    def test_sensitive_evidence_is_never_sent_to_an_external_provider_through_the_stage(self) -> None:
        for sensitive in (True, None):
            adapter = ScriptedModelAdapter([resolved()], data_boundary="EXTERNAL")
            flow = Flow(self.tmp, adapter, tag=f"ws-{sensitive}", max_boundary="EXTERNAL", sensitive=sensitive)
            result = flow.run()
            with self.subTest(sensitive):
                self.assertIn("SENSITIVE_EVIDENCE_EXTERNAL", flow.stage_record(result, "recheck").failure.message)
                self.assertEqual(adapter.calls, 0)

    def test_a_response_with_an_unknown_evidence_reference_fails_the_stage(self) -> None:
        output = resolved()
        output["findings"][0]["evidence_refs"] = ["somewhere-else"]
        flow = Flow(self.tmp, ScriptedModelAdapter([output]))
        result = flow.run()
        self.assertIn("UNAUTHORIZED_EVIDENCE", flow.stage_record(result, "recheck").failure.message)
        self.assertIs(result.state.stage("recheck").state, StageState.FAILED)
        self.assertIsNone(result.state.stage("recheck").checkpoint_id, "nada é selado por checkpoint")

    def test_a_malformed_response_never_becomes_knowledge(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([{"status": "RESOLVED", "validated_by": "Fulano"}]))
        result = flow.run()
        self.assertIn("MALFORMED_RESPONSE", flow.stage_record(result, "recheck").failure.message)
        self.assertEqual(flow.states(result)["route"], "PENDING")


class EvidenceDisciplineThroughTheStage(TempDirCase):
    def test_a_request_created_before_the_evidence_changed_is_a_changed_input(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, doc=DOC + b"EDITED AFTER THE REQUEST\n", stale_doc=DOC)
        result = flow.run()
        failure = flow.stage_record(result, "recheck").failure
        self.assertEqual(failure.category, "INPUT_MUTATED")
        self.assertIn("EVIDENCE_CHANGED", failure.message)
        self.assertEqual(adapter.calls, 0)

    def test_evidence_that_the_stage_did_not_declare_cannot_be_read(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, declare_evidence=False)
        result = flow.run()
        failure = flow.stage_record(result, "recheck").failure
        self.assertEqual(failure.category, "INPUT_UNAVAILABLE")
        self.assertIn("EVIDENCE_UNAVAILABLE", failure.message)
        self.assertEqual(adapter.calls, 0)

    def test_the_model_receives_the_current_verified_content_and_nothing_else(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter)
        flow.run()
        task = adapter.tasks[0]
        self.assertEqual(task.allowed_context[EVIDENCE_KEY][DOC_REF]["content"], DOC.decode("utf-8"))
        self.assertEqual(sorted(task.allowed_context[EVIDENCE_KEY]), [DOC_REF])

    def test_a_locator_that_the_stage_does_not_support_is_unavailable_not_a_crash(self) -> None:
        def odd_request(content: bytes) -> AIRecheckRequest:
            item = EvidenceRef.of(DOC_REF, content, "documentation", contains_sensitive_data=False, locator="elsewhere:thing")
            return AIRecheckRequest(
                subject_ref="job:x", subject_kind="documentation", ambiguity="a", reason="b", claims=(RecheckClaim("c1", "x"),),
                evidence=(item,), max_boundary="LOCAL",
            )

        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, make_request=odd_request)
        self.assertEqual(flow.stage_record(flow.run(), "recheck").failure.category, "INPUT_UNAVAILABLE")
        self.assertEqual(adapter.calls, 0)

    def test_the_stage_reads_upstream_outputs_as_evidence_too(self) -> None:
        captured: dict = {}

        def request_from_upstream(content: bytes) -> AIRecheckRequest:
            data = captured["inventory"]
            item = EvidenceRef.of("inv-1", data, "filesystem", contains_sensitive_data=False, locator=upstream_locator("inventory", "result"))
            return AIRecheckRequest(
                subject_ref="inventory:one", subject_kind="configuration", ambiguity="a", reason="b", claims=(RecheckClaim("c1", "x"),),
                evidence=(item,), max_boundary="LOCAL",
            )

        flow = Flow(self.tmp, ScriptedModelAdapter([resolved(refs=("inv-1",))]), make_request=request_from_upstream)
        # O estágio de recheck precisa declarar a dependência direta que fornece a evidência.
        flow.scenario.graph = type(flow.scenario.graph)([
            spec("inventory", inputs=[EVIDENCE_INPUT]),
            spec("analyze", deps=["inventory"], inputs=[EVIDENCE_INPUT], outputs=["analysis", "request"]),
            spec("recheck", deps=["analyze", "inventory"], outputs=["result"]),
            spec("route", deps=["analyze", "recheck"], outputs=["routed"]),
            spec("document", deps=["route"], outputs=["result"]),
        ])
        original = flow.analyze_handler

        def analyze(ctx, default):  # noqa: ANN001, ANN202
            captured["inventory"] = ctx.read_upstream("inventory", "result")
            original(ctx, default)

        flow.scenario.custom["analyze"] = analyze
        result = flow.run()
        self.assertEqual(flow.states(result)["recheck"], "COMPLETED")
        self.assertEqual(flow.output("recheck", "result")["provenance"]["evidence_snapshot"][0]["ref"], "inv-1")


class CheckpointResumeAndInvalidation(TempDirCase):
    def test_a_second_run_skips_everything_and_does_not_call_the_model_again(self) -> None:
        adapter = ScriptedModelAdapter([resolved(), resolved()])
        flow = Flow(self.tmp, adapter)
        flow.run()
        again = flow.run()
        self.assertEqual(flow.calls, [])
        self.assertEqual(set(again.plan.skipped), set(ALL_DONE))
        self.assertEqual(adapter.calls, 1)

    def test_changing_the_evidence_redoes_the_analysis_the_recheck_and_what_depends_on_it(self) -> None:
        adapter = ScriptedModelAdapter([resolved(), resolved()])
        flow = Flow(self.tmp, adapter)
        flow.run()
        flow.scenario.write_input(EVIDENCE_INPUT, DOC.decode("utf-8") + "Nova linha.\n")
        flow.stale_doc = None
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        self.assertEqual(flow.calls, ["inventory", "analyze", "recheck", "route", "document"])
        self.assertEqual(adapter.calls, 2)

    def test_changing_the_adapter_identity_invalidates_only_the_recheck_and_its_dependents(self) -> None:
        first = ScriptedModelAdapter([resolved()], adapter_id="synthetic-alpha", model_id="model-1")
        flow = Flow(self.tmp, first)
        flow.run()
        second = ScriptedModelAdapter([resolved()], adapter_id="synthetic-alpha", model_id="model-2")
        replaced = Flow(self.tmp, second)
        result = replaced.run()
        self.assertEqual(replaced.calls, ["recheck", "route", "document"])
        self.assertEqual(second.calls, 1)
        self.assertEqual(replaced.states(result), ALL_DONE)
        self.assertEqual(replaced.output("recheck", "result")["provenance"]["adapter"]["model_id"], "model-2")
        self.assertEqual(set(result.plan.skipped), {"inventory", "analyze"})

    def test_changing_the_policy_invalidates_the_recheck(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]))
        flow.run()
        stricter = ScriptedModelAdapter([resolved(confidence=0.7)])
        replaced = Flow(self.tmp, stricter, policy=RecheckPolicy(min_resolved_confidence=0.8))
        result = replaced.run()
        self.assertEqual(replaced.calls, ["recheck", "route", "document"])
        self.assertEqual(replaced.output("route", "routed")["status"], "INCONCLUSIVE")
        self.assertEqual(replaced.states(result)["document"], "COMPLETED")

    def test_parameters_that_do_not_declare_the_adapter_are_refused(self) -> None:
        adapter = ScriptedModelAdapter([resolved()])
        flow = Flow(self.tmp, adapter, declare_parameters=False)
        result = flow.run()
        failure = flow.stage_record(result, "recheck").failure
        self.assertEqual(failure.category, "HANDLER_ERROR")
        self.assertIn("stage_parameters", failure.message)
        self.assertEqual(adapter.calls, 0)

    def test_parameters_that_declare_another_adapter_are_refused(self) -> None:
        declared = AIRecheckStage(ScriptedModelAdapter([], model_id="declared-model"), request_upstream=("analyze", "request"))
        adapter = ScriptedModelAdapter([resolved()], model_id="actual-model")
        flow = Flow(self.tmp, adapter)
        flow.scenario.params["recheck"] = declared.stage_parameters()
        result = flow.run()
        self.assertIn("stage_parameters", flow.stage_record(result, "recheck").failure.message)
        self.assertEqual(adapter.calls, 0)

    def test_an_interrupted_recheck_is_requeued_and_completes_without_a_second_epistemic_pass(self) -> None:
        adapter = ScriptedModelAdapter([KeyboardInterrupt(), resolved()])
        flow = Flow(self.tmp, adapter)
        with self.assertRaises(KeyboardInterrupt):
            flow.run()
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        self.assertEqual(result.plan.with_decision(Decision.REQUEUE), ("recheck",))
        self.assertEqual(flow.output("recheck", "result")["provenance"]["attempt"], 1, "a interrupção devolve a tentativa")
        self.assertEqual(adapter.calls, 2)

    def test_a_tampered_result_is_not_trusted_on_resume(self) -> None:
        adapter = ScriptedModelAdapter([resolved(), resolved()])
        flow = Flow(self.tmp, adapter)
        flow.run()
        path = flow.scenario.output_file("recheck", "result")
        path.write_bytes(path.read_bytes().replace(b"INFERRED", b"VALIDATED", 1))
        result = flow.run()
        self.assertEqual(flow.states(result), ALL_DONE)
        self.assertIn("recheck", flow.calls)
        self.assertEqual(adapter.calls, 2)
        self.assertNotIn(b"VALIDATED", path.read_bytes())

    def test_the_consumer_refuses_a_tampered_result_even_if_the_stage_were_skipped(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]))
        flow.run()
        data = flow.output("recheck", "result")
        data["findings"][0]["status"] = "VALIDATED"
        with self.assertRaises(Exception):
            AIRecheckResult.from_dict(data)

    def test_two_fresh_workspaces_produce_the_same_bytes_for_a_deterministic_adapter(self) -> None:
        one = Flow(self.tmp, ScriptedModelAdapter([resolved()]), tag="ws-one")
        two = Flow(self.tmp / "elsewhere", ScriptedModelAdapter([resolved()]), tag="ws-two")
        one.run()
        two.run()
        for stage, name in (("recheck", "result"), ("route", "routed"), ("analyze", "request")):
            self.assertEqual(
                one.scenario.output_file(stage, name).read_bytes(), two.scenario.output_file(stage, name).read_bytes(), f"{stage}/{name}"
            )

    def test_no_clock_and_no_local_path_in_the_stored_result(self) -> None:
        flow = Flow(self.tmp, ScriptedModelAdapter([resolved()]))
        flow.run()
        text = flow.scenario.output_file("recheck", "result").read_text(encoding="utf-8")
        from core.paths import find_local_paths

        self.assertEqual(find_local_paths(json.loads(text)), [])
        self.assertNotIn(str(self.tmp), text)
        for forbidden in ("created_at", "timestamp", "started", "finished"):
            self.assertNotIn(forbidden, text)


class ProviderIndependenceThroughTheStage(TempDirCase):
    def test_two_synthetic_adapters_complete_the_same_workflow_with_the_same_content(self) -> None:
        alpha = ScriptedModelAdapter([resolved()], adapter_id="synthetic-alpha", model_id="alpha-1")
        beta = RuleBasedModelAdapter(
            lambda task: resolved() if "90 days" in task.allowed_context[EVIDENCE_KEY][DOC_REF]["content"] else source_gap(),
            adapter_id="synthetic-beta", adapter_version="4", model_id="beta-2", capabilities=("structured-output", "long-context"), deterministic=False,
        )
        flows = [Flow(self.tmp, alpha, tag="ws-alpha"), Flow(self.tmp, beta, tag="ws-beta")]
        results = [flow.run() for flow in flows]
        for flow, result in zip(flows, results):
            self.assertEqual(flow.states(result), ALL_DONE)
        alpha_result, beta_result = (flow.output("recheck", "result") for flow in flows)
        self.assertEqual(alpha_result["findings"], beta_result["findings"])
        self.assertEqual(alpha_result["result_id"], beta_result["result_id"])
        self.assertNotEqual(alpha_result["provenance"]["adapter"], beta_result["provenance"]["adapter"])
        self.assertEqual(flows[0].output("document", "result"), flows[1].output("document", "result"))

    def test_parameters_differ_per_adapter_so_the_checkpoints_never_alias(self) -> None:
        one = AIRecheckStage(ScriptedModelAdapter([], adapter_id="synthetic-alpha"), request_input="request").stage_parameters()
        two = AIRecheckStage(ScriptedModelAdapter([], adapter_id="synthetic-beta"), request_input="request").stage_parameters()
        self.assertNotEqual(one, two)
        self.assertEqual(set(one), {PARAMETER_KEY})


class ASecondSyntheticWorkflow(TempDirCase):
    """Outro software, outros nomes de estágio, outro tipo de fonte e outro assunto: o executor e o estágio não mudam."""

    def database_request(self, content: bytes) -> AIRecheckRequest:
        item = EvidenceRef.of(DDL_REF, content, "database-schema", contains_sensitive_data=False, locator=input_locator("schema-ddl"))
        return AIRecheckRequest(
            subject_ref="table:stock_item", subject_kind="database-schema", ambiguity="reorder_level aceita nulo?", reason="Sem restrição explícita.",
            claims=(RecheckClaim("n1", "A coluna reorder_level aceita nulo.", 0.4),), evidence=(item,), max_boundary="LOCAL", software_id="synthetic-two",
        )

    def flow(self, adapter, **kwargs: object) -> Flow:
        return Flow(
            self.tmp, adapter, doc=DDL, names=Names("discover-schema", "assess", "second-look", "settle", "publish"), evidence_input="schema-ddl",
            ref=DDL_REF, software_id="synthetic-two", workflow_id="synthetic-flow-two", make_request=self.database_request, **kwargs,  # type: ignore[arg-type]
        )

    def test_a_database_schema_recheck_resolves_and_continues(self) -> None:
        flow = self.flow(ScriptedModelAdapter([resolved(("n1",), (DDL_REF,))]))
        result = flow.run()
        self.assertEqual(set(flow.states(result).values()), {"COMPLETED"})
        self.assertEqual(flow.calls, ["discover-schema", "assess", "second-look", "settle", "publish"])
        self.assertEqual(flow.output("settle", "routed")["route"], "REUSE")

    def test_the_other_routes_work_for_the_second_software_too(self) -> None:
        gap = self.flow(ScriptedModelAdapter([source_gap(refs=(DDL_REF,), recommended="database-schema")]))
        gap.run()
        self.assertEqual(gap.output("settle", "routed")["source_backlog"][0]["needed_source"], "database-schema")
        decision = Flow(
            self.tmp, ScriptedModelAdapter([human_decision(refs=(DDL_REF,))]), doc=DDL, names=Names("a", "b", "c", "d", "e"), evidence_input="schema-ddl",
            ref=DDL_REF, software_id="synthetic-two", workflow_id="synthetic-flow-two", make_request=self.database_request, tag="other",
        )
        decision.run()
        self.assertEqual(decision.output("d", "routed")["route"], "HUMAN_DECISION")

    def test_the_unrelated_workflows_never_share_identities(self) -> None:
        first = Flow(self.tmp, ScriptedModelAdapter([resolved()]), tag="one")
        second = self.flow(ScriptedModelAdapter([resolved(("n1",), (DDL_REF,))]))
        first.run()
        second.run()
        self.assertNotEqual(first.output("recheck", "result")["request_id"], second.output("settle", "routed")["facts"][0]["claim_ref"])
        self.assertNotEqual(first.output("analyze", "request")["request_id"], second.output("assess", "request")["request_id"])


class StageConstruction(unittest.TestCase):
    def test_exactly_one_request_source(self) -> None:
        adapter = ScriptedModelAdapter([])
        with self.assertRaises(ValueError):
            AIRecheckStage(adapter)
        with self.assertRaises(ValueError):
            AIRecheckStage(adapter, request_input="a", request_upstream=("b", "c"))
        with self.assertRaises(ValueError):
            AIRecheckStage(adapter, request_input="Not Valid")
        with self.assertRaises(ValueError):
            AIRecheckStage(adapter, request_upstream=("only-one",))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            AIRecheckStage(adapter, request_input="a", output="Not Valid")

    def test_locators_are_logical_names(self) -> None:
        self.assertEqual(input_locator("manual"), "input:manual")
        self.assertEqual(upstream_locator("analyze", "request"), "upstream:analyze/request")

    def test_stage_parameters_are_canonical_json_data(self) -> None:
        from core.serialization import canonical_bytes

        stage = AIRecheckStage(ScriptedModelAdapter([], deterministic=True), request_input="req", policy=RecheckPolicy(inconclusive_route="SOURCE_GAP"))
        parameters = stage.stage_parameters()
        self.assertEqual(loads_strict(canonical_bytes(parameters)), parameters)
        self.assertEqual(parameters[PARAMETER_KEY]["policy"]["inconclusive_route"], "SOURCE_GAP")
        self.assertEqual(parameters[PARAMETER_KEY]["request"], {"via": "input", "name": "req"})
        self.assertEqual(parameters[PARAMETER_KEY]["adapter"]["adapter_id"], "scripted")


if __name__ == "__main__":
    unittest.main()
