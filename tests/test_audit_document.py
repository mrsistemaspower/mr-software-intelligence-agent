"""O documento de auditoria é consistente: vocabulário fechado, contagens recalculadas, cobertura das capacidades pedidas."""
from __future__ import annotations

import re
import unittest
from collections import Counter

from tests.helpers import REPO

AUDIT = REPO / "docs" / "AGENT_FOUNDATION_AUDIT.md"
CLASSES = ("GENERIC_AGENT_CAPABILITY", "SYSCOM_SPECIFIC_KNOWLEDGE", "PIPELINE_INFRASTRUCTURE", "EXPERIMENTAL_OR_LEGACY")
POTENTIAL = ("high", "medium", "low", "none")
COUPLING = ("low", "medium", "high")
ACTIONS = ("EXTRACT", "ADAPT", "KEEP_IN_SYSCOM", "REWRITE_GENERIC", "IGNORE_LEGACY", "NEEDS_REVIEW")
CAP_STATUS = ("IMPLEMENTED", "PARTIAL", "DOCUMENTED_ONLY", "NOT_FOUND")

# Capacidades que o pedido manda confirmar na implementação, em palavras-chave dos nomes.
REQUESTED = [
    "OBSERVED", "Provenance", "Confidence", "Human review", "AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION",
    "HUMAN_VALIDATION", "Contradiction", "Quarantine", "Screen grouping", "Multi-capture", "Lookup routes",
    "Interaction patterns", "Question dedup", "Knowledge snapshot", "Deterministic resume", "Stage checkpoints",
    "Hash protection", "Evidence integrity", "Documentation generation", "Scale selection", "Incremental knowledge reuse",
]


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse():
    text = AUDIT.read_text(encoding="utf-8")
    components = [cells(l) for l in text.splitlines() if re.match(r"^\| C\d{2} \|", l)]
    capabilities = [cells(l) for l in text.splitlines() if re.match(r"^\| CAP\d{2} \|", l)]
    return text, components, capabilities


TEXT, COMPONENTS, CAPABILITIES = parse()


class ComponentMap(unittest.TestCase):
    def test_table_shape(self) -> None:
        self.assertGreater(len(COMPONENTS), 40)
        for row in COMPONENTS:
            self.assertEqual(len(row), 8, row[0])

    def test_ids_are_sequential_and_unique(self) -> None:
        ids = [row[0] for row in COMPONENTS]
        self.assertEqual(ids, [f"C{i:02d}" for i in range(1, len(ids) + 1)])

    def test_closed_vocabularies(self) -> None:
        for row in COMPONENTS:
            _, name, location, classification, potential, coupling, action, notes = row
            self.assertTrue(name and location and notes, row[0])
            self.assertIn(classification, CLASSES, row[0])
            self.assertIn(potential, POTENTIAL, row[0])
            self.assertIn(coupling, COUPLING, row[0])
            self.assertIn(action, ACTIONS, row[0])

    def test_all_four_classes_and_all_actions_are_used(self) -> None:
        self.assertEqual({r[3] for r in COMPONENTS}, set(CLASSES))
        self.assertEqual({r[6] for r in COMPONENTS}, set(ACTIONS))

    def test_columns_requested_by_the_brief_are_present(self) -> None:
        header = next(l for l in TEXT.splitlines() if l.startswith("| ID | Existing component"))
        for column in ("Existing component", "Location in mr-syscom", "Classification", "Generic potential", "Coupling",
                       "Recommended action"):
            self.assertIn(column, header)

    def test_documented_counts_match_the_table(self) -> None:
        by_class = Counter(r[3] for r in COMPONENTS)
        by_action = Counter(r[6] for r in COMPONENTS)
        section = TEXT.split("## 5. Contagens")[1].split("## 6.")[0]
        documented = {m.group(1): int(m.group(2)) for m in re.finditer(r"^\| ([A-Z_]+) \| (\d+) \|$", section, re.M)}
        for name in CLASSES:
            self.assertEqual(documented[name], by_class[name], name)
        for name in ACTIONS:
            self.assertEqual(documented[name], by_action[name], name)
        by_status = Counter(r[2] for r in CAPABILITIES)
        for name in CAP_STATUS:
            self.assertEqual(documented[name], by_status[name], name)
        totals = [int(m.group(1)) for m in re.finditer(r"^\| Total \| (\d+) \|$", section, re.M)]
        self.assertEqual(totals, [len(COMPONENTS), len(COMPONENTS), len(CAPABILITIES)])
        self.assertIn(f"({len(COMPONENTS)} linhas)", section)

    def test_no_generic_component_is_left_without_an_extraction_path(self) -> None:
        for row in COMPONENTS:
            if row[3] == "GENERIC_AGENT_CAPABILITY":
                self.assertNotEqual(row[6], "KEEP_IN_SYSCOM", row[0])

    def test_specific_knowledge_is_never_marked_for_extraction(self) -> None:
        for row in COMPONENTS:
            if row[3] == "SYSCOM_SPECIFIC_KNOWLEDGE":
                self.assertNotEqual(row[6], "EXTRACT", row[0])

    def test_legacy_is_never_extracted_or_adapted(self) -> None:
        for row in COMPONENTS:
            if row[3] == "EXPERIMENTAL_OR_LEGACY":
                self.assertNotIn(row[6], {"EXTRACT", "ADAPT"}, row[0])

    def test_unread_items_are_not_presented_as_confirmed(self) -> None:
        for row in COMPONENTS:
            if row[6] == "NEEDS_REVIEW":
                self.assertRegex(row[7], r"(?i)não lid|nao lid|só .*lid|não foi|desconhecid|lido", row[0])


class Capabilities(unittest.TestCase):
    def test_shape_and_ids(self) -> None:
        self.assertEqual([r[0] for r in CAPABILITIES], [f"CAP{i:02d}" for i in range(1, len(CAPABILITIES) + 1)])
        for row in CAPABILITIES:
            self.assertEqual(len(row), 5, row[0])
            self.assertIn(row[2], CAP_STATUS, row[0])
            self.assertTrue(row[3] and row[4], row[0])

    def test_every_requested_capability_was_checked(self) -> None:
        names = " | ".join(r[1] for r in CAPABILITIES).casefold()
        for keyword in REQUESTED:
            self.assertIn(keyword.casefold(), names, keyword)

    def test_findings_that_matter_are_stated_honestly(self) -> None:
        status = {r[1]: r[2] for r in CAPABILITIES}
        self.assertEqual(status["HUMAN_VALIDATION"], "PARTIAL")
        self.assertEqual(status["Change intelligence / comparação de versões"], "NOT_FOUND")
        self.assertEqual(status["AI_RECHECK"], "PARTIAL")

    def test_every_implemented_item_cites_evidence(self) -> None:
        for row in CAPABILITIES:
            if row[2] in {"IMPLEMENTED", "PARTIAL"}:
                self.assertRegex(row[3], r"(\.py|\.json|\.md|`)", row[0])


class Consistency(unittest.TestCase):
    def test_readme_numbers_match_the_audit(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(CAPABILITIES)} capacidades confirmadas", readme)
        self.assertIn(f"{len(COMPONENTS)} componentes classificados", readme)

    def test_audit_states_that_the_workspace_was_not_modified(self) -> None:
        self.assertIn("Nenhum arquivo do `mr-syscom` foi criado, editado, movido, formatado ou apagado.", TEXT)
        self.assertIn("168 entradas", TEXT)
        self.assertIn("4c652a3", TEXT)

    def test_artifacts_named_in_section_7_exist_in_this_repository(self) -> None:
        """A seção 7 diz o que a Foundation fez com cada achado. Tudo que ela cita por caminho precisa existir aqui."""
        section = TEXT.split("## 7.")[1].split("## 8.")[0]
        paths = sorted(set(re.findall(r"`((?:schemas|core|policies|adapters|workflows|skills|evaluations|tests)/[A-Za-z0-9_./-]+)`", section)))
        self.assertGreater(len(paths), 8)
        for path in paths:
            self.assertTrue((REPO / path.rstrip("/")).exists(), f"seção 7 cita {path}, que não existe")


if __name__ == "__main__":
    unittest.main()
