"""Grafo de estágios: validação antes da execução, ordem determinística, propagação e ponte com o contrato de workflow."""
from __future__ import annotations

import copy
import itertools
import unittest

from tests.helpers import REPO, load_json
from core.schema_lite import Registry, Validator
from core.stage_graph import (
    CYCLE,
    DUPLICATE_STAGE,
    EMPTY_GRAPH,
    INVALID_GRAPH,
    INVALID_STAGE,
    REQUIRED_DEPENDS_ON_OPTIONAL,
    SELF_DEPENDENCY,
    UNKNOWN_DEPENDENCY,
    GraphError,
    RetryPolicy,
    StageGraph,
    StageSpec,
    graph_from_agent_workflow,
    is_valid_name,
)


def s(stage_id: str, *deps: str, **extra: object) -> StageSpec:
    return StageSpec(stage_id, depends_on=deps, **extra)


def code_of(callable_, *args, **kwargs) -> str:
    with unittest.TestCase().assertRaises(GraphError) as caught:
        callable_(*args, **kwargs)
    return caught.exception.code


class Validation(unittest.TestCase):
    def test_a_valid_graph_is_built_and_ordered(self) -> None:
        graph = StageGraph([s("c", "b"), s("b", "a"), s("a")])
        self.assertEqual(graph.order, ("a", "b", "c"))
        self.assertEqual(len(graph), 3)
        self.assertIn("b", graph)
        self.assertNotIn("zz", graph)

    def test_empty_graph_is_rejected(self) -> None:
        self.assertEqual(code_of(StageGraph, []), EMPTY_GRAPH)

    def test_duplicate_stage_id_is_rejected(self) -> None:
        self.assertEqual(code_of(StageGraph, [s("a"), s("b"), s("a")]), DUPLICATE_STAGE)

    def test_self_dependency_is_rejected(self) -> None:
        self.assertEqual(code_of(StageGraph, [s("a", "a")]), SELF_DEPENDENCY)

    def test_unknown_dependency_is_rejected_and_named(self) -> None:
        with self.assertRaises(GraphError) as caught:
            StageGraph([s("a", "ghost"), s("b")])
        self.assertEqual(caught.exception.code, UNKNOWN_DEPENDENCY)
        self.assertIn("ghost", str(caught.exception))

    def test_two_node_cycle_is_rejected(self) -> None:
        self.assertEqual(code_of(StageGraph, [s("a", "b"), s("b", "a")]), CYCLE)

    def test_long_cycle_with_innocent_tail_is_rejected_and_reported_concretely(self) -> None:
        stages = [s("root"), s("a", "root", "c"), s("b", "a"), s("c", "b"), s("tail", "c")]
        with self.assertRaises(GraphError) as caught:
            StageGraph(stages)
        self.assertEqual(caught.exception.code, CYCLE)
        message = str(caught.exception)
        for member in ("a", "b", "c"):
            self.assertIn(member, message)
        self.assertNotIn("tail", message, "o rabo que apenas depende do ciclo não faz parte dele")
        self.assertNotIn("root", message)

    def test_cycle_report_is_deterministic_for_any_declaration_order(self) -> None:
        stages = [s("a", "c"), s("b", "a"), s("c", "b"), s("x")]
        messages = {str(self._cycle_error(list(order))) for order in itertools.permutations(stages)}
        self.assertEqual(len(messages), 1)

    @staticmethod
    def _cycle_error(stages: list) -> GraphError:
        try:
            StageGraph(stages)
        except GraphError as exc:
            return exc
        raise AssertionError("o ciclo deveria ser rejeitado")

    def test_required_stage_may_not_depend_on_an_optional_one(self) -> None:
        self.assertEqual(code_of(StageGraph, [s("a", optional=True), s("b", "a")]), REQUIRED_DEPENDS_ON_OPTIONAL)

    def test_optional_may_depend_on_required_and_on_optional(self) -> None:
        graph = StageGraph([s("a"), s("b", "a", optional=True), s("c", "b", optional=True)])
        self.assertEqual(graph.order, ("a", "b", "c"))

    def test_failures_happen_before_any_execution_is_possible(self) -> None:
        # Construir já valida: não existe grafo inválido em memória.
        with self.assertRaises(GraphError):
            StageGraph([s("a", "b"), s("b", "a")])

    def test_non_stage_items_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            StageGraph([{"stage_id": "a"}])  # type: ignore[list-item]


class Names(unittest.TestCase):
    def test_accepted_names_are_arbitrary_but_well_formed(self) -> None:
        for name in ("a", "zeta", "ui-analysis", "step_1", "report.json", "a.b-c_d", "x" * 64):
            with self.subTest(name):
                self.assertTrue(is_valid_name(name))
                StageSpec(name)

    def test_rejected_names(self) -> None:
        for name in ("", "A", "Upper", "1abc", "a b", "a/b", "a\\b", "a..b", "a--b", "-a", "a-", "a.", "x" * 65, "ação", "a:b", "con", "nul", "aux", "com1", "lpt1"):
            with self.subTest(name):
                self.assertFalse(is_valid_name(name))
                with self.assertRaises(GraphError):
                    StageSpec(name)

    def test_non_string_names_are_rejected(self) -> None:
        for value in (None, 3, b"a", ("a",)):
            self.assertFalse(is_valid_name(value))

    def test_invalid_contract_names_and_duplicates_are_rejected(self) -> None:
        with self.assertRaises(GraphError):
            StageSpec("a", input_contracts=("Bad",))
        with self.assertRaises(GraphError):
            StageSpec("a", output_contracts=("out", "out"))
        with self.assertRaises(GraphError):
            StageSpec("a", depends_on=("b", "b"))

    def test_version_and_flags_are_validated(self) -> None:
        for version in ("", " 1", "-1", "a b", "x" * 65, 1):
            with self.subTest(version):
                with self.assertRaises(GraphError):
                    StageSpec("a", version=version)  # type: ignore[arg-type]
        with self.assertRaises(GraphError):
            StageSpec("a", optional="yes")  # type: ignore[arg-type]
        with self.assertRaises(GraphError):
            StageSpec("a", retry=3)  # type: ignore[arg-type]

    def test_retry_policy_bounds(self) -> None:
        for bad in (0, -1, 21, True, 1.5, "2"):
            with self.subTest(bad):
                with self.assertRaises(GraphError):
                    RetryPolicy(bad)  # type: ignore[arg-type]
        self.assertEqual(RetryPolicy(20).max_attempts, 20)


class DeterministicOrder(unittest.TestCase):
    STAGES = [s("inventory"), s("ui", "inventory"), s("db", "inventory"), s("docs", "ui", "db"), s("audit", "inventory")]

    def test_order_does_not_depend_on_declaration_order(self) -> None:
        orders = {StageGraph(list(p)).order for p in itertools.permutations(self.STAGES)}
        self.assertEqual(orders, {("inventory", "audit", "db", "ui", "docs")})

    def test_ties_are_broken_by_the_smallest_stage_id(self) -> None:
        graph = StageGraph([s("zeta"), s("alpha"), s("mid"), s("beta", "zeta")])
        self.assertEqual(graph.order, ("alpha", "mid", "zeta", "beta"))

    def test_digest_and_serialization_do_not_depend_on_declaration_order(self) -> None:
        digests = {StageGraph(list(p)).digest() for p in itertools.permutations(self.STAGES)}
        dumps = {str(StageGraph(list(p)).to_dict()) for p in itertools.permutations(self.STAGES)}
        self.assertEqual((len(digests), len(dumps)), (1, 1))

    def test_dependency_order_inside_a_stage_is_irrelevant(self) -> None:
        self.assertEqual(
            StageGraph([s("a"), s("b"), s("c", "a", "b")]).digest(), StageGraph([s("b"), s("a"), s("c", "b", "a")]).digest()
        )

    def test_dependencies_always_precede_dependents(self) -> None:
        graph = StageGraph(self.STAGES)
        position = {name: index for index, name in enumerate(graph.order)}
        for item in graph.stages:
            for dep in item.depends_on:
                self.assertLess(position[dep], position[item.stage_id])


class Propagation(unittest.TestCase):
    """A -> B -> C e A -> D, o exemplo da especificação."""

    def setUp(self) -> None:
        self.graph = StageGraph([s("a"), s("b", "a"), s("c", "b"), s("d", "a")])

    def test_descendants(self) -> None:
        self.assertEqual(self.graph.descendants("a"), ("b", "c", "d"))
        self.assertEqual(self.graph.descendants("b"), ("c",))
        self.assertEqual(self.graph.descendants("c"), ())
        self.assertEqual(self.graph.descendants("d"), ())

    def test_ancestors(self) -> None:
        self.assertEqual(self.graph.ancestors("c"), ("a", "b"))
        self.assertEqual(self.graph.ancestors("d"), ("a",))
        self.assertEqual(self.graph.ancestors("a"), ())

    def test_direct_dependents_are_sorted(self) -> None:
        self.assertEqual(self.graph.dependents("a"), ("b", "d"))

    def test_descendants_of_a_diamond_are_not_repeated(self) -> None:
        graph = StageGraph([s("a"), s("b", "a"), s("c", "a"), s("d", "b", "c")])
        self.assertEqual(graph.descendants("a"), ("b", "c", "d"))

    def test_unknown_stage_queries_are_rejected(self) -> None:
        for call in (self.graph.descendants, self.graph.ancestors, self.graph.dependents, self.graph.get):
            with self.assertRaises(GraphError):
                call("ghost")


class Contract(unittest.TestCase):
    def sample(self) -> StageGraph:
        return StageGraph([
            s("inventory", input_contracts=("source",), output_contracts=("listing",)),
            s("analysis", "inventory", version="2", optional=True, retry=RetryPolicy(3), output_contracts=("report",)),
        ])

    def test_round_trip(self) -> None:
        graph = self.sample()
        again = StageGraph.from_dict(copy.deepcopy(graph.to_dict()))
        self.assertEqual(again, graph)
        self.assertEqual(again.digest(), graph.digest())

    def test_serialized_form_validates_against_the_schema(self) -> None:
        self.assertEqual(Validator(Registry()).validate(self.sample().to_dict(), "stage-graph"), [])

    def test_unknown_property_is_rejected(self) -> None:
        data = self.sample().to_dict()
        data["stages"][0]["surprise"] = 1
        self.assertEqual(code_of(StageGraph.from_dict, data), INVALID_GRAPH)

    def test_a_cycle_inside_a_document_is_rejected(self) -> None:
        data = {"schema": "urn:mr-software-intelligence:schema:stage-graph:1",
                "stages": [{"stage_id": "a", "depends_on": ["b"]}, {"stage_id": "b", "depends_on": ["a"]}]}
        self.assertEqual(code_of(StageGraph.from_dict, data), CYCLE)

    def test_wrong_schema_marker_and_empty_stage_list_are_rejected(self) -> None:
        self.assertEqual(code_of(StageGraph.from_dict, {"schema": "x", "stages": [{"stage_id": "a"}]}), INVALID_GRAPH)
        self.assertEqual(code_of(StageGraph.from_dict, {"schema": "urn:mr-software-intelligence:schema:stage-graph:1", "stages": []}), INVALID_GRAPH)
        self.assertEqual(code_of(StageGraph.from_dict, "nope"), INVALID_GRAPH)

    def test_digest_changes_with_any_structural_change(self) -> None:
        base = self.sample()
        changed = [
            StageGraph([s("inventory", input_contracts=("source",), output_contracts=("listing",)), s("analysis", "inventory", version="3", optional=True, retry=RetryPolicy(3), output_contracts=("report",))]),
            StageGraph([s("inventory", input_contracts=("source2",), output_contracts=("listing",)), s("analysis", "inventory", version="2", optional=True, retry=RetryPolicy(3), output_contracts=("report",))]),
            StageGraph([s("inventory", input_contracts=("source",), output_contracts=("listing",)), s("analysis", "inventory", version="2", retry=RetryPolicy(3), output_contracts=("report",))]),
        ]
        for other in changed:
            self.assertNotEqual(other.digest(), base.digest())

    def test_definition_digest_ignores_retry_but_not_version_or_contracts(self) -> None:
        a = StageSpec("x", output_contracts=("o",))
        self.assertEqual(a.definition_digest(), StageSpec("x", output_contracts=("o",), retry=RetryPolicy(5)).definition_digest())
        self.assertNotEqual(a.definition_digest(), StageSpec("x", version="2", output_contracts=("o",)).definition_digest())
        self.assertNotEqual(a.definition_digest(), StageSpec("x", output_contracts=("p",)).definition_digest())
        self.assertNotEqual(a.definition_digest(), StageSpec("x", output_contracts=("o",), optional=True).definition_digest())

    def test_graph_carries_no_software_knowledge(self) -> None:
        graph = StageGraph([s("zeta-ingest"), s("omega-merge", "zeta-ingest")])
        self.assertEqual(graph.order, ("zeta-ingest", "omega-merge"))


class AgentWorkflowBridge(unittest.TestCase):
    """Um único conceito de fluxo: os passos de um `agent-workflow` viram estágios."""

    def doc(self, steps: list) -> dict:
        return {
            "id": "sample", "verb": "learn", "summary": "x",
            "inputs": [{"name": "software", "required": True, "description": "x"}],
            "skills": ["erp-reverse-engineering"],
            "steps": steps,
            "outputs": [{"name": "records", "description": "x"}],
            "mutates_knowledge": False, "escalates_to_human": False,
            "policies": ["policies/evidence-discipline.md"],
        }

    def test_every_existing_workflow_becomes_a_linear_graph(self) -> None:
        files = sorted((REPO / "workflows").glob("*.workflow.json"))
        self.assertEqual(len(files), 6)
        for path in files:
            data = load_json(path)
            with self.subTest(path.name):
                graph = graph_from_agent_workflow(data)
                self.assertEqual(list(graph.order), [step["id"] for step in data["steps"]])
                for previous, current in zip(data["steps"], data["steps"][1:]):
                    self.assertEqual(graph.get(current["id"]).depends_on, (previous["id"],))
                self.assertEqual(graph.get(data["steps"][0]["id"]).depends_on, ())

    def test_explicit_depends_on_builds_branches(self) -> None:
        doc = self.doc([
            {"id": "inventory", "description": "x"},
            {"id": "ui", "description": "x", "depends_on": ["inventory"]},
            {"id": "db", "description": "x", "depends_on": ["inventory"]},
            {"id": "docs", "description": "x", "depends_on": ["ui"]},
        ])
        graph = graph_from_agent_workflow(doc)
        self.assertEqual(graph.descendants("ui"), ("docs",))
        self.assertEqual(graph.descendants("db"), ())

    def test_empty_depends_on_makes_a_second_root(self) -> None:
        doc = self.doc([{"id": "a", "description": "x"}, {"id": "b", "description": "x", "depends_on": []}])
        self.assertEqual(graph_from_agent_workflow(doc).get("b").depends_on, ())

    def test_cycle_unknown_step_and_self_dependency_are_rejected(self) -> None:
        cyc = self.doc([{"id": "a", "description": "x", "depends_on": ["b"]}, {"id": "b", "description": "x", "depends_on": ["a"]}])
        self.assertEqual(code_of(graph_from_agent_workflow, cyc), CYCLE)
        ghost = self.doc([{"id": "a", "description": "x"}, {"id": "b", "description": "x", "depends_on": ["zzz"]}])
        self.assertEqual(code_of(graph_from_agent_workflow, ghost), UNKNOWN_DEPENDENCY)
        selfdep = self.doc([{"id": "a", "description": "x"}, {"id": "b", "description": "x", "depends_on": ["b"]}])
        self.assertEqual(code_of(graph_from_agent_workflow, selfdep), SELF_DEPENDENCY)

    def test_documents_outside_the_workflow_contract_are_rejected(self) -> None:
        self.assertEqual(code_of(graph_from_agent_workflow, {"id": "x"}), INVALID_GRAPH)

    def test_schema_extension_is_optional_and_typed(self) -> None:
        validator = Validator(Registry())
        base = self.doc([{"id": "a", "description": "x"}, {"id": "b", "description": "x"}])
        self.assertEqual(validator.validate(base, "agent-workflow"), [])
        bad = self.doc([{"id": "a", "description": "x"}, {"id": "b", "description": "x", "depends_on": "a"}])
        self.assertNotEqual(validator.validate(bad, "agent-workflow"), [])
        repeated = self.doc([{"id": "a", "description": "x"}, {"id": "b", "description": "x", "depends_on": ["a", "a"]}])
        self.assertNotEqual(validator.validate(repeated, "agent-workflow"), [])

    def test_invalid_stage_error_code_is_exposed(self) -> None:
        self.assertEqual(code_of(StageSpec, "BAD"), INVALID_STAGE)


if __name__ == "__main__":
    unittest.main()
