"""Referências internas: arquivos exigidos, skills, workflows, adaptadores, perfis, políticas e links."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.helpers import REPO, load_json
from core.schema_lite import Registry, Validator

REGISTRY = Registry()
VALIDATOR = Validator(REGISTRY)

REQUIRED_FILES = [
    "AGENT.md", "README.md", ".gitignore", ".gitattributes",
    "docs/AGENT_FOUNDATION_AUDIT.md", "docs/ARCHITECTURE.md", "docs/MODEL_PORTABILITY.md",
    "docs/decisions/0001-agent-vs-software-knowledge.md",
    "skills/erp-reverse-engineering/SKILL.md",
    "software-packs/syscom/manifest.json", "software-packs/README.md",
    "policies/escalation.policy.json", "policies/evidence-discipline.md", "policies/human-escalation.md",
    "policies/safety-and-isolation.md", "evaluations/README.md", "adapters/README.md", "workflows/README.md",
    "schemas/README.md", "skills/README.md",
]
REQUIRED_DIRS = ["core", "skills", "schemas", "adapters", "workflows", "policies", "evaluations", "tests", "software-packs", "docs"]
SKILLS = [
    "erp-reverse-engineering", "ui-reverse-engineering", "documentation-mining", "workflow-reconstruction",
    "business-rule-discovery", "database-analysis", "runtime-exploration", "change-intelligence", "troubleshooting",
    "documentation-generation",
]
VERBS = ["learn", "explain", "ask", "find-gaps", "compare", "document"]
PHASES = [
    "UNKNOWN SOFTWARE", "SOURCE INVENTORY", "SURFACE / MODULE DISCOVERY", "SCREEN & STATE DISCOVERY",
    "INTERACTION ANALYSIS", "ENTITY / LOOKUP DISCOVERY", "WORKFLOW RECONSTRUCTION", "BUSINESS-RULE DISCOVERY",
    "EVIDENCE CLASSIFICATION", "CONTRADICTION DETECTION", "TARGETED HUMAN REVIEW", "KNOWLEDGE PACK", "DOCUMENTATION",
    "SUPPORT / TROUBLESHOOTING", "CONTINUOUS UPDATE",
]
SKIP_DIRS = {".git", "__pycache__"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, "sem cabeçalho"
    meta: dict = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value
    return meta


class RequiredStructure(unittest.TestCase):
    def test_required_files(self) -> None:
        for rel in REQUIRED_FILES:
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_required_directories(self) -> None:
        for rel in REQUIRED_DIRS:
            self.assertTrue((REPO / rel).is_dir(), rel)

    def test_no_directory_is_empty(self) -> None:
        for path in REPO.rglob("*"):
            if path.is_dir() and not (set(path.relative_to(REPO).parts) & SKIP_DIRS):
                self.assertTrue(any(p.is_file() for p in path.rglob("*")), f"diretório vazio: {path.relative_to(REPO)}")

    def test_no_stray_artifacts_in_the_repository(self) -> None:
        for path in REPO.rglob("*"):
            rel = path.relative_to(REPO)
            if set(rel.parts) & SKIP_DIRS:
                continue
            self.assertNotIn(path.suffix, {".pyc", ".tmp", ".log"}, str(rel))
            self.assertFalse(path.name.startswith("local.config.json"), str(rel))

    def test_repository_has_no_provider_configuration_at_the_root(self) -> None:
        self.assertFalse((REPO / ".kiro").exists())
        self.assertFalse((REPO / "CLAUDE.md").exists())


class Skills(unittest.TestCase):
    def test_exactly_the_ten_skills(self) -> None:
        found = sorted(p.name for p in (REPO / "skills").iterdir() if p.is_dir())
        self.assertEqual(found, sorted(SKILLS))

    def test_frontmatter(self) -> None:
        for name in SKILLS:
            meta = frontmatter(read(REPO / "skills" / name / "SKILL.md"))
            self.assertEqual(meta["name"], name)
            self.assertTrue(meta["description"], name)
            self.assertRegex(meta["version"], r"^\d+\.\d+\.\d+$")
            self.assertIn(meta["status"], {"specified", "contract"}, name)
            self.assertIsInstance(meta["requires"], list, name)
            self.assertTrue(meta["requires"], name)

    def test_requires_point_to_existing_files(self) -> None:
        for name in SKILLS:
            for rel in frontmatter(read(REPO / "skills" / name / "SKILL.md"))["requires"]:
                self.assertTrue((REPO / rel).exists(), f"{name} requer {rel}")

    def test_only_the_erp_skill_is_fully_specified(self) -> None:
        status = {n: frontmatter(read(REPO / "skills" / n / "SKILL.md"))["status"] for n in SKILLS}
        self.assertEqual(status.pop("erp-reverse-engineering"), "specified")
        self.assertEqual(set(status.values()), {"contract"})

    def test_contracts_have_the_structural_sections(self) -> None:
        for name in SKILLS[1:]:
            text = read(REPO / "skills" / name / "SKILL.md")
            for section in ("## Intenção", "## Fronteiras", "## Depende de", "## Perguntas em aberto"):
                self.assertIn(section, text, f"{name}: {section}")

    def test_index_lists_every_skill(self) -> None:
        index = read(REPO / "skills" / "README.md")
        for name in SKILLS:
            self.assertIn(f"`{name}`", index)


class ErpSkill(unittest.TestCase):
    text = read(REPO / "skills" / "erp-reverse-engineering" / "SKILL.md")

    def test_fifteen_phases_in_the_required_order(self) -> None:
        headings = re.findall(r"^### Fase (\d+): (.+)$", self.text, re.M)
        self.assertEqual([(int(n), name) for n, name in headings], list(enumerate(PHASES)))

    def test_each_phase_defines_objective_and_exit_criterion(self) -> None:
        blocks = re.split(r"^### Fase \d+: .+$", self.text, flags=re.M)[1:]
        self.assertEqual(len(blocks), len(PHASES))
        for phase, block in zip(PHASES, blocks):
            self.assertIn("**Objetivo.**", block, phase)
            self.assertIn("**Critério de saída.**", block, phase)
            self.assertIn("**Saídas", block, phase)

    def test_cross_cutting_sections(self) -> None:
        for section in ("## Princípios que governam", "## Modos por disponibilidade de fontes", "## Fluxo",
                        "## Operação em escala", "## Critérios de qualidade", "## O que esta skill não faz"):
            self.assertIn(section, self.text)

    def test_covers_every_source_availability_mode(self) -> None:
        for mode in ("Só capturas", "documentação", "execução", "esquema de banco", "código-fonte", "especialista"):
            self.assertIn(mode, self.text)

    def test_names_every_route_and_the_core_entry_points(self) -> None:
        for token in ("AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION", "core.escalation.decide",
                      "core.impact.plan_reanalysis", "py -m core validate-pack", "py -m core check-isolation"):
            self.assertIn(token, self.text)

    def test_related_skills_all_exist(self) -> None:
        related = re.findall(r"`([a-z]+(?:-[a-z]+)+)`", self.text.split("## Relação com outras skills")[1])
        self.assertTrue(set(related) <= set(SKILLS), set(related) - set(SKILLS))


class Workflows(unittest.TestCase):
    files = sorted((REPO / "workflows").glob("*.workflow.json"))

    def test_exactly_the_six_verbs(self) -> None:
        self.assertEqual(sorted(load_json(f)["verb"] for f in self.files), sorted(VERBS))

    def test_each_file_matches_its_verb_and_the_schema(self) -> None:
        for f in self.files:
            data = load_json(f)
            self.assertEqual(VALIDATOR.validate(data, "agent-workflow"), [], f.name)
            self.assertEqual(f.name, f"{data['verb']}.workflow.json")
            self.assertEqual(data["id"], data["verb"])

    def test_referenced_skills_and_policies_exist(self) -> None:
        for f in self.files:
            data = load_json(f)
            for skill in data["skills"]:
                self.assertIn(skill, SKILLS, f"{f.name}: {skill}")
            for step in data["steps"]:
                if "skill" in step:
                    self.assertIn(step["skill"], data["skills"], f"{f.name}: passo {step['id']}")
            for policy in data["policies"]:
                self.assertTrue((REPO / policy).is_file(), f"{f.name}: {policy}")
            ids = [s["id"] for s in data["steps"]]
            self.assertEqual(len(ids), len(set(ids)), f.name)

    def test_read_only_verbs_do_not_mutate_or_escalate(self) -> None:
        for f in self.files:
            data = load_json(f)
            if data["verb"] in {"explain", "ask", "document"}:
                self.assertFalse(data["mutates_knowledge"], f.name)
                self.assertFalse(data["escalates_to_human"], f.name)

    def test_mutating_verbs_validate_the_pack(self) -> None:
        for verb in ("learn",):
            data = load_json(REPO / "workflows" / f"{verb}.workflow.json")
            gates = [s.get("gate", "") for s in data["steps"]]
            self.assertTrue(any("validate-pack" in g for g in gates))

    def test_index_documents_every_verb(self) -> None:
        index = read(REPO / "workflows" / "README.md")
        for verb in VERBS:
            self.assertIn(f"`{verb}`", index)
        agent = read(REPO / "AGENT.md")
        for verb in VERBS:
            self.assertIn(f"`{verb}`", agent)


class SourceAdapters(unittest.TestCase):
    files = sorted((REPO / "adapters" / "sources").glob("*.adapter.json"))
    kinds = load_json(REPO / "schemas" / "common.schema.json")["$defs"]["sourceKind"]["enum"]

    def test_nine_adapters_cover_every_source_kind(self) -> None:
        self.assertEqual(len(self.files), 9)
        data = [load_json(f) for f in self.files]
        self.assertEqual(sorted(d["source_kind"] for d in data), sorted(self.kinds))
        self.assertEqual(len({d["id"] for d in data}), 9)

    def test_each_adapter_is_valid_and_named_after_its_file(self) -> None:
        for f in self.files:
            data = load_json(f)
            self.assertEqual(VALIDATOR.validate(data, "source-adapter"), [], f.name)
            self.assertEqual(f.name, f"{data['id']}.adapter.json")

    def test_contract_guarantees(self) -> None:
        for f in self.files:
            data = load_json(f)
            self.assertTrue(data["read_only"], f.name)
            self.assertTrue({"describe", "inventory", "extract", "coverage"} <= set(data["operations"]), f.name)
            self.assertEqual(data["status"], "contract", f.name)

    def test_invasive_sources_require_human_authorization(self) -> None:
        by_id = {load_json(f)["id"]: load_json(f) for f in self.files}
        for sid in ("runtime", "database-schema"):
            self.assertTrue(by_id[sid]["safety"]["requires_human_authorization"], sid)

    def test_contract_table_is_documented(self) -> None:
        readme = read(REPO / "adapters" / "README.md")
        for f in self.files:
            self.assertIn(f"`{load_json(f)['id']}`", readme)
        for op in ("describe", "inventory", "fingerprint", "extract", "coverage"):
            self.assertIn(f"`{op}(", readme)


class LlmProfiles(unittest.TestCase):
    files = sorted((REPO / "adapters" / "llm").glob("*.profile.json"))

    def test_four_profiles(self) -> None:
        self.assertEqual(sorted(load_json(f)["id"] for f in self.files), ["claude", "kiro", "local", "openai"])

    def test_profiles_are_valid_and_point_to_existing_files(self) -> None:
        for f in self.files:
            data = load_json(f)
            self.assertEqual(VALIDATOR.validate(data, "llm-profile"), [], f.name)
            self.assertEqual(f.name, f"{data['id']}.profile.json")
            self.assertEqual(data["instruction_entrypoint"], "AGENT.md")
            self.assertTrue((REPO / data["loading"]["example_file"]).is_file(), f.name)

    def test_profiles_never_copy_the_instructions(self) -> None:
        agent_lines = {line.strip() for line in read(REPO / "AGENT.md").splitlines() if len(line.strip()) > 60}
        for f in self.files:
            for example in f.parent.glob(f"{load_json(f)['id']}/*"):
                for line in read(example).splitlines():
                    self.assertNotIn(line.strip(), agent_lines, f"{example.name} repete AGENT.md")

    def test_known_providers_file(self) -> None:
        terms = load_json(REPO / "adapters" / "llm" / "known-providers.json")["terms"]
        self.assertTrue({"claude", "openai", "kiro", "anthropic"} <= set(terms))


class Policies(unittest.TestCase):
    def test_escalation_policy_matches_the_schema(self) -> None:
        self.assertEqual(VALIDATOR.validate(load_json(REPO / "policies" / "escalation.policy.json"), "escalation-policy"), [])

    def test_ladder_matches_the_documented_order(self) -> None:
        text = read(REPO / "policies" / "human-escalation.md")
        positions = [text.index(marker) for marker in (
            "conhecimento já validado", "reuso de evidências", "documentação existente", "relações entre registros",
            "nova leitura focada", "observação em execução")]
        self.assertEqual(positions, sorted(positions))

    def test_agent_contract_names_all_routes_and_policies(self) -> None:
        agent = read(REPO / "AGENT.md")
        for route in ("AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION"):
            self.assertIn(route, agent)
        for policy in ("evidence-discipline.md", "human-escalation.md", "safety-and-isolation.md", "escalation.policy.json"):
            self.assertIn(policy, agent)

    def test_agent_contract_sections(self) -> None:
        agent = read(REPO / "AGENT.md")
        for heading in ("Identidade", "Missão", "Princípios operacionais", "Disciplina de evidência", "Limites do raciocínio",
                        "Escalonamento humano", "Isolamento entre agente e software", "Ciclo de vida do conhecimento",
                        "Portabilidade de modelo", "Segurança e operação não destrutiva"):
            self.assertRegex(agent, rf"(?m)^## \d+\. {re.escape(heading)}", heading)

    def test_mission_statement_is_verbatim(self) -> None:
        mission = ("Um agente de inteligência de software capaz de aprender, investigar, reconstruir, documentar e "
                   "acompanhar sistemas complexos, especialmente ERPs e softwares legados, utilizando múltiplas "
                   "fontes de evidência e intervenção humana mínima.")
        self.assertIn(mission, read(REPO / "AGENT.md"))
        self.assertIn(mission, read(REPO / "README.md"))


class Documentation(unittest.TestCase):
    LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

    def test_relative_links_resolve(self) -> None:
        checked = 0
        for md in sorted(REPO.rglob("*.md")):
            if set(md.relative_to(REPO).parts) & SKIP_DIRS:
                continue
            for target in self.LINK.findall(read(md)):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path = (md.parent / target.split("#")[0]).resolve()
                self.assertTrue(path.exists(), f"{md.relative_to(REPO)} -> {target}")
                checked += 1
        self.assertGreater(checked, 15)

    def test_decision_record_has_the_required_parts(self) -> None:
        text = read(REPO / "docs" / "decisions" / "0001-agent-vs-software-knowledge.md")
        for part in ("- Status: aceita", "## Contexto", "## Decisão", "## Alternativas consideradas", "## Consequências"):
            self.assertIn(part, text)

    def test_architecture_shows_the_layer_diagram(self) -> None:
        text = read(REPO / "docs" / "ARCHITECTURE.md")
        for layer in ("LLM / Runtime", "Agent", "Skills + Tools", "Software Knowledge Pack", "Software"):
            self.assertIn(layer, text)
        order = [text.index(layer) for layer in ("LLM / Runtime", "|           Agent", "Skills + Tools", "Software Knowledge Pack")]
        self.assertEqual(order, sorted(order))

    def test_portability_doc_covers_every_target(self) -> None:
        text = read(REPO / "docs" / "MODEL_PORTABILITY.md")
        for target in ("Claude", "ChatGPT", "Kiro", "modelos locais"):
            self.assertIn(target, text)

    def test_readme_has_the_required_sections(self) -> None:
        text = read(REPO / "README.md")
        for heading in ("O problema que resolve", "Arquitetura", "Agente x conhecimento de software", "Principais skills",
                        "Primeiro caso: SYSCOM", "Visão de futuro", "Estado atual"):
            self.assertIn(heading, text, heading)

    def test_validation_case_one_principle_is_recorded(self) -> None:
        text = read(REPO / "README.md") + read(REPO / "docs" / "decisions" / "0001-agent-vs-software-knowledge.md")
        self.assertRegex(text, r"caso de treino e validação número 1")
        self.assertIn("sem reescrever o núcleo", text)


if __name__ == "__main__":
    unittest.main()
