"""Inteligência de mudança: quais registros uma mudança afeta."""
from __future__ import annotations

import unittest

from tests.helpers import FIXTURE
from core.impact import ACTION_RANK, dependents, plan_reanalysis, reference_graph
from core.pack import load_pack


def release(rid, action, subject=None, affected=()):
    return {
        "id": rid,
        "from_version": {"label": "1"}, "to_version": {"label": "2"},
        "kind": "modified",
        "subject": {"descriptor": "d", **({"ref": subject} if subject else {})},
        "detected_by": {"method": "human-report"},
        "impact": {"action": action, "affected_refs": list(affected)},
    }


def fact(rid, *refs, validated=False):
    record = {"id": rid, "statement": "s", "about": list(refs), "epistemic": {"status": "VALIDATED" if validated else "INFERRED"}}
    return record


class Graph(unittest.TestCase):
    def test_reference_graph_and_transitive_dependents(self) -> None:
        records = {
            "fct:a": fact("fct:a"),
            "fct:b": fact("fct:b", "fct:a"),
            "fct:c": fact("fct:c", "fct:b"),
            "fct:d": fact("fct:d"),
        }
        graph = reference_graph(records)
        self.assertEqual(graph["fct:b"], {"fct:a"})
        self.assertEqual(dependents(graph, {"fct:a"}), {"fct:b", "fct:c"})
        self.assertEqual(dependents(graph, {"fct:d"}), set())

    def test_cycles_terminate(self) -> None:
        records = {"fct:a": fact("fct:a", "fct:b"), "fct:b": fact("fct:b", "fct:a")}
        self.assertEqual(dependents(reference_graph(records), {"fct:a"}), {"fct:b"})

    def test_seeds_are_not_reported_as_their_own_dependents(self) -> None:
        records = {"fct:a": fact("fct:a", "fct:b"), "fct:b": fact("fct:b", "fct:a")}
        self.assertEqual(dependents(reference_graph(records), {"fct:a", "fct:b"}), set())


class Plan(unittest.TestCase):
    def records(self) -> dict:
        return {
            "fct:a": fact("fct:a"),
            "fct:b": fact("fct:b", "fct:a"),
            "fct:v": fact("fct:v", validated=True),
            "fct:alone": fact("fct:alone"),
        }

    def test_subject_gets_the_declared_action_and_dependents_at_most_revalidate(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "INVALIDATE", subject="fct:a")
        self.assertEqual(plan_reanalysis(records), {"fct:a": "INVALIDATE", "fct:b": "REVALIDATE"})

    def test_reanalyze_does_not_spread_beyond_revalidate(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "REANALYZE", subject="fct:a")
        self.assertEqual(plan_reanalysis(records)["fct:b"], "REVALIDATE")

    def test_none_action_changes_nothing(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "NONE", subject="fct:a")
        self.assertEqual(plan_reanalysis(records), {})

    def test_validated_records_are_never_below_revalidate(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "NONE", affected=["fct:v"])
        self.assertEqual(plan_reanalysis(records), {})  # NONE não toca em nada
        records["rel:x"] = release("rel:x", "REVALIDATE", affected=["fct:v"])
        self.assertEqual(plan_reanalysis(records), {"fct:v": "REVALIDATE"})

    def test_strongest_action_wins_across_changes(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "REVALIDATE", subject="fct:a")
        records["rel:y"] = release("rel:y", "INVALIDATE", subject="fct:a")
        self.assertEqual(plan_reanalysis(records)["fct:a"], "INVALIDATE")

    def test_unrelated_records_are_untouched_and_changes_are_excluded(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "REANALYZE", subject="fct:a")
        plan = plan_reanalysis(records)
        self.assertNotIn("fct:alone", plan)
        self.assertNotIn("rel:x", plan)

    def test_unknown_targets_are_ignored(self) -> None:
        records = self.records()
        records["rel:x"] = release("rel:x", "REANALYZE", affected=["fct:ghost"])
        self.assertEqual(plan_reanalysis(records), {})

    def test_action_ranking(self) -> None:
        order = sorted(ACTION_RANK, key=ACTION_RANK.get)
        self.assertEqual(order, ["NONE", "REVALIDATE", "REANALYZE", "INVALIDATE"])


class OnTheSyntheticPack(unittest.TestCase):
    def test_release_note_impact(self) -> None:
        pack = load_pack(FIXTURE)
        plan = plan_reanalysis(pack.records)
        # assunto e referência declarada recebem a ação da mudança
        self.assertEqual(plan["scr:posting-window"], "REVALIDATE")
        self.assertEqual(plan["int:save-persists"], "REVALIDATE")
        # a regra validada depende da tela afetada: perde a base
        self.assertEqual(plan["rul:balance"], "REVALIDATE")
        # fluxo e interações que citam a tela são reanalisados
        self.assertIn("wfl:post-entry", plan)
        self.assertIn("int:account-lookup-opens", plan)
        # fontes e evidências não dependem de registros: nunca entram
        self.assertFalse([rid for rid in plan if rid.startswith(("src:", "evd:", "rel:"))])
        # revisões e lookups que citam a tela entram
        self.assertIn("rev:decide-grouping", plan)
        self.assertIn("lku:account", plan)

    def test_plan_is_deterministic_and_sorted(self) -> None:
        pack = load_pack(FIXTURE)
        first, second = plan_reanalysis(pack.records), plan_reanalysis(pack.records)
        self.assertEqual(list(first), list(second))
        self.assertEqual(list(first), sorted(first))


if __name__ == "__main__":
    unittest.main()
