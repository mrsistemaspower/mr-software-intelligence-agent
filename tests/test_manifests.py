"""Manifestos válidos e o contrato do pacote do primeiro caso de validação."""
from __future__ import annotations

import re
import unittest

from tests.helpers import FIXTURE, REPO, SYSCOM, TEMPLATE, codes, load_json
from core.pack import discover_packs, iter_strings, validate_pack
from core.schema_lite import Registry, Validator

REGISTRY = Registry()
ALL_PACKS = discover_packs(REPO / "software-packs", REPO / "evaluations" / "fixtures")
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(\\\\|\\|/)")


class EveryManifest(unittest.TestCase):
    def test_packs_are_discovered(self) -> None:
        names = {p.name for p in ALL_PACKS}
        self.assertEqual(names, {"_template", "syscom", "acme-ledger"})

    def test_every_pack_validates_without_issues(self) -> None:
        for pack in ALL_PACKS:
            self.assertEqual([str(i) for i in validate_pack(pack, REGISTRY)], [], pack.name)

    def test_software_id_matches_directory_except_template(self) -> None:
        for pack in ALL_PACKS:
            manifest = load_json(pack / "manifest.json")
            if manifest["role"] == "template":
                self.assertEqual(pack.name, "_template")
            else:
                self.assertEqual(manifest["software_id"], pack.name)

    def test_software_ids_are_unique(self) -> None:
        ids = [load_json(p / "manifest.json")["software_id"] for p in ALL_PACKS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_workspace_access_is_read_only(self) -> None:
        for pack in ALL_PACKS:
            self.assertEqual(load_json(pack / "manifest.json")["external_workspace"]["access"], "read-only", pack.name)

    def test_public_pack_files_have_no_absolute_paths(self) -> None:
        for pack in ALL_PACKS:
            manifest = load_json(pack / "manifest.json")
            public = ["manifest.json", "software.json"]
            if manifest["integration"].get("reference_file"):
                public.append(manifest["integration"]["reference_file"])
            for name in public:
                raw = (pack / name).read_text(encoding="utf-8")
                self.assertIsNone(DRIVE_PATH.search(raw), f"{pack.name}/{name}")
                for path, text in iter_strings(load_json(pack / name)):
                    self.assertNotRegex(text, r"^(/home/|/Users/|\\\\)", f"{pack.name}/{name} {path}")

    def test_leak_terms_are_distinct_between_packs(self) -> None:
        seen: dict[str, str] = {}
        for pack in ALL_PACKS:
            manifest = load_json(pack / "manifest.json")
            if manifest["role"] == "template":
                continue
            terms = manifest["isolation"]["leak_terms"]
            for term in terms["identity"] + terms["domain"]:
                key = term.casefold()
                self.assertNotIn(key, seen, f"termo '{term}' repetido entre {pack.name} e {seen.get(key)}")
                seen[key] = pack.name

    def test_template_is_a_valid_empty_pack(self) -> None:
        manifest = load_json(TEMPLATE / "manifest.json")
        self.assertEqual(manifest["role"], "template")
        self.assertEqual(manifest["collections"], [])


class SyscomPack(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_json(SYSCOM / "manifest.json")

    def test_manifest_states_the_contract(self) -> None:
        m = self.manifest
        self.assertEqual(m["software_id"], "syscom")
        self.assertEqual(m["display_name"], "SYSCOM")
        self.assertEqual(m["role"], "first_validation_case")
        self.assertEqual(m["validation_case_index"], 1)
        self.assertEqual(m["external_workspace"]["mode"], "external")
        self.assertEqual(m["external_workspace"]["access"], "read-only")
        self.assertEqual(m["knowledge_ownership"]["canonical_location"], "external-workspace")
        self.assertEqual(m["knowledge_ownership"]["duplication_policy"], "forbidden")
        self.assertEqual(m["integration"]["status"], "reference-only")
        self.assertEqual(m["collections"], [])

    def test_workspace_is_resolved_by_configuration_not_by_path(self) -> None:
        locator = self.manifest["external_workspace"]["locator"]
        self.assertEqual(locator["resolution"], "env")
        self.assertEqual(locator["name"], "MR_SYSCOM_WORKSPACE")
        self.assertNotIn("path", locator)

    def test_pack_holds_only_manifest_metadata_and_reference(self) -> None:
        files = {p.name for p in SYSCOM.iterdir()}
        self.assertEqual(
            files,
            {"manifest.json", "software.json", "integration.reference.json", "local.config.example.json", "README.md"},
        )
        self.assertLess(sum(p.stat().st_size for p in SYSCOM.iterdir()), 64 * 1024)

    def test_only_the_example_local_config_mentions_a_drive_path(self) -> None:
        with_drive = {p.name for p in SYSCOM.iterdir() if DRIVE_PATH.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(with_drive, {"local.config.example.json"})

    def test_real_local_config_is_ignored_and_absent(self) -> None:
        self.assertFalse((SYSCOM / "local.config.json").exists())
        gitignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("software-packs/*/local.config.json", gitignore)

    def test_reference_pins_the_observed_commit_and_stays_relative(self) -> None:
        ref = load_json(SYSCOM / "integration.reference.json")
        self.assertRegex(ref["workspace_snapshot"]["ref"], r"^[0-9a-f]{40}$")
        self.assertEqual(ref["mapping_status"], "not-started")
        for root in ref["workspace_roots"]:
            self.assertFalse(root["path"].startswith(("/", "\\")))
            self.assertNotRegex(root["path"], r"^[A-Za-z]:")

    def test_known_source_availability_is_not_overstated(self) -> None:
        availability = load_json(SYSCOM / "software.json")["source_availability"]
        self.assertEqual(availability["screenshots"], "available")
        for kind in ("source_code", "database", "runtime", "documentation", "release_notes"):
            self.assertEqual(availability[kind], "unknown", kind)


class PackMutationsAtManifestLevel(unittest.TestCase):
    def test_manifest_schema_catches_a_bad_manifest(self) -> None:
        manifest = load_json(SYSCOM / "manifest.json")
        manifest["external_workspace"]["access"] = "read-write"
        errors = Validator(REGISTRY).validate(manifest, "software-pack-manifest")
        self.assertTrue(errors)

    def test_fixture_declares_every_collection(self) -> None:
        manifest = load_json(FIXTURE / "manifest.json")
        self.assertEqual(len(manifest["collections"]), 11)
        self.assertEqual(codes(validate_pack(FIXTURE, REGISTRY)), set())


if __name__ == "__main__":
    unittest.main()
