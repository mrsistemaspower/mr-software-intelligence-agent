"""Isolamento: o conhecimento de um software não vaza para o núcleo, para as camadas genéricas ou para outro pacote."""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.helpers import REPO, TempDirCase, copy_pack, copy_repo, load_json, write_json
from core import isolation

# Montado por concatenação para que este arquivo, que é varrido, não contenha a identidade antiga por extenso.
OLD_IDENTITY_TEXT = "mrs" + "istemas6"


def kinds(findings) -> set[str]:
    return {f.kind for f in findings}


class RepositoryIsClean(unittest.TestCase):
    def test_no_findings_at_all(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_all(REPO)], [])

    def test_leak_terms_cover_every_real_pack_and_skip_the_template(self) -> None:
        packs = isolation.collect_leak_terms(REPO)
        self.assertEqual(set(packs), {"syscom", "acme-ledger"})
        self.assertIn("syscom", packs["syscom"]["terms"])
        self.assertIn("cadastro de identificacao", packs["syscom"]["terms"])  # dobrado: sem acento, minúsculo
        self.assertIn("gerenciar nota fiscal", packs["syscom"]["terms"])
        self.assertIn("classificacao de produtos", packs["syscom"]["terms"])

    def test_neutral_zones_exist_and_are_not_empty(self) -> None:
        for zone in isolation.SOFTWARE_NEUTRAL_ZONES:
            self.assertTrue((REPO / zone).exists(), zone)
        for zone in isolation.PROVIDER_NEUTRAL_ZONES:
            self.assertTrue((REPO / zone).exists(), zone)

    def test_the_core_has_no_branch_on_software_id_and_no_software_constants(self) -> None:
        self.assertEqual([str(f) for f in isolation.check_core_independence(REPO)], [])

    def test_provider_names_live_only_in_the_provider_layer(self) -> None:
        self.assertEqual([str(f) for f in isolation.scan_provider_terms(REPO)], [])
        profile_text = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "adapters" / "llm").glob("*.profile.json")).casefold()
        for provider in ("anthropic", "openai", "kiro"):
            self.assertIn(provider, profile_text)

    def test_old_identity_is_absent_everywhere(self) -> None:
        self.assertEqual([str(f) for f in isolation.scan_old_identity(REPO)], [])

    def test_declared_organisation_identity_is_the_current_one(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/mrsistemaspower/mr-software-intelligence-agent", readme)


class ScannerDetectsViolations(TempDirCase):
    """O scanner precisa falhar quando deve. Cada teste planta uma violação numa cópia do repositório."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = copy_repo(self.tmp / "repo")
        self.assertEqual(isolation.check_all(self.repo), [], "a cópia deve começar limpa")

    def plant(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(old + "\n" + text + "\n", encoding="utf-8")

    def test_domain_term_in_a_skill_even_without_accents_or_case(self) -> None:
        self.plant("skills/ui-reverse-engineering/SKILL.md", "exemplo: CADASTRO DE IDENTIFICACAO")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))

    def test_identity_term_in_a_policy(self) -> None:
        self.plant("policies/evidence-discipline.md", "como no SysCom")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))

    def test_term_in_the_agent_contract(self) -> None:
        self.plant("AGENT.md", "Gerenciar Nota Fiscal")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))

    def test_term_in_a_schema_description(self) -> None:
        self.plant("schemas/README.md", "Classificação de Produtos")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))

    def test_term_in_the_pack_contract_readme(self) -> None:
        self.plant("software-packs/README.md", "syscom")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))

    def test_term_in_the_template(self) -> None:
        self.plant("software-packs/_template/README.md", "acme-ledger")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))

    def test_one_pack_citing_another(self) -> None:
        self.plant("software-packs/syscom/README.md", "veja o Posting Window do outro software")
        self.assertIn("CROSS_PACK_LEAK", kinds(isolation.check_all(self.repo)))
        self.plant("evaluations/fixtures/acme-ledger/software.json", "syscom")
        self.assertIn("CROSS_PACK_LEAK", kinds(isolation.check_all(self.repo)))

    def test_a_pack_may_name_itself(self) -> None:
        self.plant("software-packs/syscom/README.md", "SYSCOM, Cadastro de Identificação")
        self.assertEqual(isolation.check_all(self.repo), [])

    def test_provider_name_in_a_neutral_zone(self) -> None:
        for relative in ("AGENT.md", "skills/troubleshooting/SKILL.md", "policies/human-escalation.md", "workflows/ask.workflow.json"):
            with self.subTest(relative):
                repo = copy_repo(self.tmp / ("copy-" + relative.replace("/", "-")))
                path = repo / relative
                path.write_text(path.read_text(encoding="utf-8") + "\nfunciona melhor no Claude\n", encoding="utf-8")
                self.assertIn("PROVIDER_IN_NEUTRAL_ZONE", kinds(isolation.check_all(repo)))

    def test_provider_name_is_allowed_in_docs_readme_and_provider_layer(self) -> None:
        self.plant("docs/MODEL_PORTABILITY.md", "Claude e Kiro e OpenAI")
        self.plant("README.md", "Claude")
        self.plant("adapters/llm/claude/instructions.example.md", "Anthropic")
        self.assertEqual(isolation.check_all(self.repo), [])

    def test_software_terms_are_allowed_in_docs_readme_and_tests(self) -> None:
        self.plant("docs/ARCHITECTURE.md", "SYSCOM Cadastro de Identificação")
        self.plant("README.md", "syscom")
        self.plant("tests/zz_probe.py", "X = 'syscom'")
        self.assertEqual(isolation.check_all(self.repo), [])

    def test_software_id_branch_in_the_core(self) -> None:
        self.plant("core/zz_bad.py", 'def f(software_id):\n    if software_id == "anything":\n        return 1\n')
        self.assertIn("SOFTWARE_ID_BRANCH_IN_CORE", kinds(isolation.check_core_independence(self.repo)))

    def test_branch_through_a_manifest_lookup_is_also_caught(self) -> None:
        self.plant("core/zz_bad.py", 'def f(m):\n    return m["software_id"] == "anything"\n')
        self.assertIn("SOFTWARE_ID_BRANCH_IN_CORE", kinds(isolation.check_core_independence(self.repo)))

    def test_software_constant_in_the_core(self) -> None:
        self.plant("core/zz_bad.py", 'KNOWN = "syscom"\n')
        self.assertIn("SOFTWARE_CONSTANT_IN_CORE", kinds(isolation.check_core_independence(self.repo)))
        self.plant("core/zz_bad2.py", 'TITLE = "Cadastro de Identificação"\n')
        self.assertIn("SOFTWARE_CONSTANT_IN_CORE", kinds(isolation.check_core_independence(self.repo)))

    def test_comparing_other_names_in_the_core_is_fine(self) -> None:
        self.plant("core/zz_ok.py", 'def f(status):\n    return status == "OBSERVED"\n')
        self.assertEqual(isolation.check_core_independence(self.repo), [])

    def test_old_identity_anywhere(self) -> None:
        self.plant("docs/ARCHITECTURE.md", "git clone https://github.com/" + OLD_IDENTITY_TEXT + "/x")
        self.assertIn("OLD_IDENTITY", kinds(isolation.check_all(self.repo)))
        repo = copy_repo(self.tmp / "second")
        (repo / "software-packs" / "syscom" / "README.md").write_text(OLD_IDENTITY_TEXT, encoding="utf-8")
        self.assertIn("OLD_IDENTITY", kinds(isolation.check_all(repo)))

    def test_a_new_pack_extends_the_checks_without_code_changes(self) -> None:
        zeta = copy_pack(self.repo / "software-packs" / "_template", self.repo / "software-packs" / "zeta-erp")
        manifest = load_json(zeta / "manifest.json")
        manifest.update(software_id="zeta-erp", display_name="Zeta ERP", role="validation_case")
        manifest["isolation"]["leak_terms"] = {"identity": ["zeta-erp"], "domain": ["Painel Quântico"]}
        write_json(zeta / "manifest.json", manifest)
        software = load_json(zeta / "software.json")
        software.update(software_id="zeta-erp", display_name="Zeta ERP")
        write_json(zeta / "software.json", software)
        self.assertEqual(isolation.check_all(self.repo), [], "o novo pacote começa limpo")

        self.plant("skills/workflow-reconstruction/SKILL.md", "o painel quantico")
        self.assertIn("SOFTWARE_LEAK_IN_GENERIC", kinds(isolation.check_all(self.repo)))
        repo = copy_repo(self.tmp / "other")
        zeta2 = copy_pack(repo / "software-packs" / "_template", repo / "software-packs" / "zeta-erp")
        write_json(zeta2 / "manifest.json", {**manifest})
        write_json(zeta2 / "software.json", software)
        (repo / "software-packs" / "syscom" / "README.md").write_text("zeta-erp", encoding="utf-8")
        self.assertIn("CROSS_PACK_LEAK", kinds(isolation.check_all(repo)))
        (repo / "software-packs" / "syscom" / "README.md").write_text("ok", encoding="utf-8")
        (zeta2 / "README.md").write_text("syscom", encoding="utf-8")
        self.assertIn("CROSS_PACK_LEAK", kinds(isolation.check_all(repo)))


class FoldingHelper(unittest.TestCase):
    def test_fold(self) -> None:
        self.assertEqual(isolation.fold("Classificação DE Produtos"), "classificacao de produtos")
        self.assertEqual(isolation.fold("ÁÉÍÓÚ ãõ ç"), "aeiou ao c")


if __name__ == "__main__":
    unittest.main()
