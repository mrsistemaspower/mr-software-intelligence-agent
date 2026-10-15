"""A estrutura recebe um segundo (e um terceiro) software sem tocar no núcleo."""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from tests.helpers import REPO, TempDirCase, codes, copy_pack, copy_repo, load_json, write_json
from core import isolation
from core.impact import plan_reanalysis
from core.pack import discover_packs, load_pack, validate_pack
from core.schema_lite import Registry

REGISTRY = Registry()
SHA = "9f" * 32


def tree(root: Path, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and "__pycache__" not in rel and not rel.startswith(exclude):
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def instantiate(repo: Path, software_id: str, display: str, terms: list[str], domain: list[str]) -> Path:
    pack = copy_pack(repo / "software-packs" / "_template", repo / "software-packs" / software_id)
    manifest = load_json(pack / "manifest.json")
    manifest.update(software_id=software_id, display_name=display, role="validation_case")
    manifest["isolation"]["leak_terms"] = {"identity": terms, "domain": domain}
    write_json(pack / "manifest.json", manifest)
    software = load_json(pack / "software.json")
    software.update(software_id=software_id, display_name=display)
    write_json(pack / "software.json", software)
    return pack


def add_records(pack: Path) -> None:
    """Dá ao pacote um conhecimento mínimo embutido: uma fonte, uma evidência e um fato observado."""
    manifest = load_json(pack / "manifest.json")
    manifest["collections"] = ["sources", "evidence", "facts"]
    write_json(pack / "manifest.json", manifest)
    write_json(pack / "sources.json", {"collection": "sources", "records": [
        {"id": "src:doc-01", "kind": "documentation", "locator": {"workspace": "pack", "path": "doc/manual.txt"},
         "sha256": SHA, "contains_sensitive_data": False}]})
    write_json(pack / "evidence.json", {"collection": "evidence", "records": [
        {"id": "evd:doc-01", "source_id": "src:doc-01", "kind": "text", "content": "O manual descreve o painel.",
         "extracted_by": {"actor": "tool", "name": "extractor"}, "confidence": 0.9}]})
    write_json(pack / "facts.json", {"collection": "facts", "records": [
        {"id": "fct:panel", "statement": "O manual descreve o painel.", "supported_by": ["evd:doc-01"],
         "epistemic": {"status": "OBSERVED", "confidence": 0.9,
                       "provenance": [{"source_id": "src:doc-01", "evidence_id": "evd:doc-01", "source_sha256": SHA}]}}]})


class RepositoryAlreadyHasTwoSoftwares(unittest.TestCase):
    def test_two_real_packs_validate_through_the_same_code_path(self) -> None:
        packs = [p for p in discover_packs(REPO / "software-packs", REPO / "evaluations" / "fixtures")
                 if load_json(p / "manifest.json")["role"] != "template"]
        self.assertGreaterEqual(len(packs), 2)
        self.assertEqual(len({load_json(p / "manifest.json")["software_id"] for p in packs}), len(packs))
        for pack in packs:
            self.assertEqual(codes(validate_pack(pack, REGISTRY)), set(), pack.name)

    def test_the_two_packs_use_the_two_ownership_modes(self) -> None:
        modes = {
            load_json(p / "manifest.json")["knowledge_ownership"]["canonical_location"]
            for p in discover_packs(REPO / "software-packs", REPO / "evaluations" / "fixtures")
            if load_json(p / "manifest.json")["role"] != "template"
        }
        self.assertEqual(modes, {"external-workspace", "this-pack"})

    def test_generic_capabilities_run_on_the_second_software(self) -> None:
        pack = load_pack(REPO / "evaluations" / "fixtures" / "acme-ledger")
        self.assertTrue(plan_reanalysis(pack.records))


class AddingPacksTouchesOnlyPacks(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = copy_repo(self.tmp / "repo")
        self.before = tree(self.repo)

    def test_adding_two_embedded_packs_changes_nothing_outside_software_packs(self) -> None:
        for sid, display, term, domain in (
            ("zeta-erp", "Zeta ERP", "zeta-erp", "Painel Quântico"),
            ("omega-crm", "Omega CRM", "omega-crm", "Funil Radial"),
        ):
            pack = instantiate(self.repo, sid, display, [term], [domain])
            add_records(pack)
            self.assertEqual([str(i) for i in validate_pack(pack, REGISTRY)], [], sid)

        after = tree(self.repo)
        added = set(after) - set(self.before)
        self.assertTrue(added)
        self.assertTrue(all(path.startswith("software-packs/") for path in added), sorted(added))
        changed = {path for path in self.before if after.get(path) != self.before[path]}
        self.assertEqual(changed, set())

    def test_all_packs_validate_together_and_isolation_still_holds(self) -> None:
        for sid, display, term in (("zeta-erp", "Zeta ERP", "zeta-erp"), ("omega-crm", "Omega CRM", "omega-crm")):
            add_records(instantiate(self.repo, sid, display, [term], []))
        packs = discover_packs(self.repo / "software-packs", self.repo / "evaluations" / "fixtures")
        self.assertEqual({p.name for p in packs}, {"_template", "syscom", "zeta-erp", "omega-crm", "acme-ledger"})
        for pack in packs:
            self.assertEqual(codes(validate_pack(pack, REGISTRY)), set(), pack.name)
        self.assertEqual([str(f) for f in isolation.check_all(self.repo)], [])
        self.assertEqual(set(isolation.collect_leak_terms(self.repo)), {"syscom", "acme-ledger", "zeta-erp", "omega-crm"})

    def test_a_new_pack_with_a_copied_term_is_rejected(self) -> None:
        zeta = instantiate(self.repo, "zeta-erp", "Zeta ERP", ["zeta-erp"], [])
        (zeta / "README.md").write_text("baseado no syscom", encoding="utf-8")
        self.assertIn("CROSS_PACK_LEAK", {f.kind for f in isolation.check_all(self.repo)})

    def test_an_embedded_pack_with_a_wrong_workspace_is_rejected(self) -> None:
        pack = instantiate(self.repo, "zeta-erp", "Zeta ERP", ["zeta-erp"], [])
        add_records(pack)
        sources = load_json(pack / "sources.json")
        sources["records"][0]["locator"]["workspace"] = "elsewhere"
        write_json(pack / "sources.json", sources)
        self.assertIn("P013", codes(validate_pack(pack, REGISTRY)))

    def test_a_stale_hash_in_a_new_pack_is_rejected(self) -> None:
        pack = instantiate(self.repo, "zeta-erp", "Zeta ERP", ["zeta-erp"], [])
        add_records(pack)
        sources = load_json(pack / "sources.json")
        sources["records"][0]["sha256"] = "1" * 64
        write_json(pack / "sources.json", sources)
        self.assertIn("P022", codes(validate_pack(pack, REGISTRY)))

    def test_external_mode_pack_cannot_carry_knowledge(self) -> None:
        pack = instantiate(self.repo, "zeta-erp", "Zeta ERP", ["zeta-erp"], [])
        manifest = load_json(pack / "manifest.json")
        manifest["external_workspace"] = {
            "mode": "external", "workspace_id": "zeta-workspace", "access": "read-only",
            "locator": {"resolution": "env", "name": "MR_ZETA_WORKSPACE"},
        }
        manifest["knowledge_ownership"]["canonical_location"] = "external-workspace"
        manifest["integration"] = {"status": "reference-only", "reference_file": "integration.reference.json"}
        manifest["collections"] = ["facts"]
        write_json(pack / "manifest.json", manifest)
        self.assertIn("P002", codes(validate_pack(pack, REGISTRY)))


if __name__ == "__main__":
    unittest.main()
