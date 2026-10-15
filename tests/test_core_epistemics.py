"""Regras epistêmicas do núcleo."""
from __future__ import annotations

import unittest

from tests.helpers import REPO
from core import epistemics as E
from core.escalation import load_policy


class Bands(unittest.TestCase):
    def test_band_edges(self) -> None:
        self.assertEqual(E.band(0.0), "low")
        self.assertEqual(E.band(0.49), "low")
        self.assertEqual(E.band(0.5), "medium")
        self.assertEqual(E.band(0.79), "medium")
        self.assertEqual(E.band(0.8), "high")
        self.assertEqual(E.band(1.0), "high")

    def test_band_rejects_invalid_confidence(self) -> None:
        for bad in (-0.01, 1.01):
            with self.assertRaises(ValueError):
                E.band(bad)
        for bad in ("high", None, True):
            with self.assertRaises(TypeError):
                E.band(bad)

    def test_band_accepts_custom_limits(self) -> None:
        self.assertEqual(E.band(0.85, low_below=0.9, high_from=0.95), "low")

    def test_defaults_match_the_policy_file(self) -> None:
        thresholds = load_policy()["thresholds"]
        self.assertEqual(E.DEFAULT_LOW_BELOW, thresholds["low_below"])
        self.assertEqual(E.DEFAULT_HIGH_FROM, thresholds["high_from"])


class Naming(unittest.TestCase):
    def test_agent_identifiers_are_recognised(self) -> None:
        for name in ("ai:reader", "agent-1", "Tool", "LLM", "system", "bot_7", "AI reader"):
            self.assertTrue(E.looks_like_agent(name), name)

    def test_people_are_not_agents(self) -> None:
        for name in ("Maria Example", "Aida Silva", "Toolan", "Systemic Ana", "Botelho"):
            self.assertFalse(E.looks_like_agent(name), name)


class SemanticChecks(unittest.TestCase):
    def ep(self, **extra) -> dict:
        base = {"status": "INFERRED", "confidence": 0.7, "provenance": [{"source_id": "src:a"}], "basis": "b"}
        base.update(extra)
        return base

    def validated(self, **validation) -> dict:
        data = {"validated_by": "Maria Example", "validated_at": "2026-10-14T10:00:00-03:00", "method": "human_review"}
        data.update(validation)
        return {"status": "VALIDATED", "confidence": 0.9, "provenance": [{"source_id": "src:a"}], "validation": data}

    def test_clean_blocks_have_no_problems(self) -> None:
        self.assertEqual(E.check_epistemic(self.ep()), [])
        self.assertEqual(E.check_epistemic(self.validated()), [])

    def test_agent_cannot_validate(self) -> None:
        problems = E.check_epistemic(self.validated(validated_by="agent:reader"))
        self.assertTrue(any("agente" in p for p in problems), problems)

    def test_invalid_method_is_flagged(self) -> None:
        self.assertTrue(E.check_epistemic(self.validated(method="vibes")))

    def test_validated_in_quarantine_is_flagged(self) -> None:
        block = self.validated()
        block["quarantine"] = {"reason": "contradiction", "since": "2026-10-14T10:00:00-03:00"}
        self.assertTrue(E.check_epistemic(block))

    def test_missing_pieces_are_flagged(self) -> None:
        self.assertTrue(E.check_epistemic(self.ep(basis="")))
        self.assertTrue(E.check_epistemic(self.ep(provenance=[])))
        self.assertTrue(E.check_epistemic(self.ep(confidence=2)))
        self.assertTrue(E.check_epistemic(self.ep(status="MAYBE")))


class Constants(unittest.TestCase):
    def test_enumerations_match_the_schema(self) -> None:
        from tests.helpers import load_json

        common = load_json(REPO / "schemas" / "common.schema.json")["$defs"]
        self.assertEqual(tuple(common["status"]["enum"]), E.STATUSES)
        self.assertEqual(tuple(common["route"]["enum"]), E.ROUTES)
        self.assertEqual(
            tuple(common["validation"]["properties"]["method"]["enum"]), E.VALIDATION_METHODS
        )


if __name__ == "__main__":
    unittest.main()
