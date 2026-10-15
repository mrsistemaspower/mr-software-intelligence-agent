"""Invariantes entre registros. Cada código de problema é provocado por uma mutação do pacote sintético."""
from __future__ import annotations

import copy
import unittest

from tests.helpers import FIXTURE, SYSCOM, TempDirCase, codes, copy_pack, load_json, write_json
from core.pack import ABS_PATH_RE, ID_RE, Pack, iter_refs, load_pack, validate_pack
from core.schema_lite import Registry

REGISTRY = Registry()


def by_id(data: dict, rid: str) -> dict:
    return next(r for r in data["records"] if r["id"] == rid)


class FixtureIsClean(unittest.TestCase):
    def test_fixture_has_no_issues(self) -> None:
        self.assertEqual([str(i) for i in validate_pack(FIXTURE, REGISTRY)], [])

    def test_load_pack_counts(self) -> None:
        pack = load_pack(FIXTURE)
        self.assertIsInstance(pack, Pack)
        counts: dict[str, int] = {}
        for collection in pack.owner.values():
            counts[collection] = counts.get(collection, 0) + 1
        self.assertEqual(
            counts,
            {"sources": 7, "evidence": 11, "facts": 5, "screens": 2, "interactions": 2, "lookups": 2, "workflows": 1,
             "business-rules": 1, "contradictions": 2, "reviews": 4, "releases": 1},
        )
        self.assertEqual(len(pack.records), 38)

    def test_all_routes_and_states_are_exercised(self) -> None:
        pack = load_pack(FIXTURE)
        reviews = [r for r in pack.records.values() if r["id"].startswith("rev:")]
        self.assertEqual({r["route"] for r in reviews}, {"AI_RECHECK", "SOURCE_GAP", "HUMAN_DECISION", "HUMAN_VALIDATION"})
        self.assertEqual({r["status"] for r in pack.records.values() if r["id"].startswith("ctr:")}, {"OPEN", "RESOLVED"})
        statuses = {r["epistemic"]["status"] for r in pack.records.values() if "epistemic" in r}
        self.assertEqual(statuses, {"OBSERVED", "INFERRED", "VALIDATED"})
        self.assertEqual(
            {r["target"]["status"] for r in pack.records.values() if r["id"].startswith("lku:")}, {"KNOWN", "UNKNOWN"}
        )


class Helpers(unittest.TestCase):
    def test_id_regex(self) -> None:
        for good in ("src:a", "evd:a-b.c_d", "rel:1"):
            self.assertRegex(good, ID_RE)
        for bad in ("zzz:a", "src:", "src:A", "src: a", "fct"):
            self.assertNotRegex(bad, ID_RE)

    def test_refs_ignore_the_records_own_id_and_free_text(self) -> None:
        record = {"id": "fct:a", "statement": "veja fct:b no texto", "about": ["scr:a"], "nested": {"x": "evd:z"}}
        self.assertEqual({ref for _, ref in iter_refs(record)}, {"scr:a", "evd:z"})

    def test_absolute_path_detector(self) -> None:
        for bad in ("C:\\x", "C:/x", "veja D:\\y\\z", "\\\\srv\\share", "/home/u", "/Users/u/x"):
            self.assertRegex(bad, ABS_PATH_RE)
        for good in ("https://example.org/a", "scr:a/b", "a/b/c", "docs/ARCH.md", "key: value", "etc/hosts"):
            self.assertNotRegex(good, ABS_PATH_RE)


class Mutations(TempDirCase):
    """Cada teste quebra uma invariante e confere que o código certo aparece."""

    def setUp(self) -> None:
        super().setUp()
        self.pack = copy_pack(FIXTURE, self.tmp / "acme-ledger")

    def edit(self, collection: str, fn) -> None:
        path = self.pack / f"{collection}.json"
        data = load_json(path)
        fn(data)
        write_json(path, data)

    def edit_manifest(self, fn) -> None:
        path = self.pack / "manifest.json"
        data = load_json(path)
        fn(data)
        write_json(path, data)

    def issues(self, pack=None) -> set[str]:
        return codes(validate_pack(pack or self.pack, REGISTRY))

    def test_p001_unreadable_json(self) -> None:
        (self.pack / "sources.json").write_text("{ not json", encoding="utf-8")
        self.assertIn("P001", self.issues())

    def test_p002_manifest_schema_violation(self) -> None:
        self.edit_manifest(lambda d: d.pop("isolation"))
        self.assertIn("P002", self.issues())

    def test_p003_directory_name_differs_from_software_id(self) -> None:
        other = copy_pack(self.pack, self.tmp / "other-name")
        self.assertIn("P003", self.issues(other))

    def test_p004_metadata_mismatch(self) -> None:
        path = self.pack / "software.json"
        data = load_json(path)
        data["display_name"] = "Outro Nome"
        write_json(path, data)
        self.assertIn("P004", self.issues())

    def test_p005_integration_reference_mismatch(self) -> None:
        syscom = copy_pack(SYSCOM, self.tmp / "syscom")
        path = syscom / "integration.reference.json"
        data = load_json(path)
        data["software_id"] = "someone-else"
        write_json(path, data)
        self.assertIn("P005", self.issues(syscom))

    def test_p006_absolute_path_in_public_file(self) -> None:
        self.edit_manifest(lambda d: d["integration"].update(notes="Veja C:\\temp\\x"))
        self.assertIn("P006", self.issues())

    def test_p007_declared_collection_is_missing(self) -> None:
        (self.pack / "releases.json").unlink()
        self.assertIn("P007", self.issues())

    def test_p008_collection_present_but_undeclared(self) -> None:
        self.edit_manifest(lambda d: d["collections"].remove("releases"))
        self.assertIn("P008", self.issues())

    def test_p009_bad_container(self) -> None:
        write_json(self.pack / "sources.json", {"records": []})
        self.assertIn("P009", self.issues())

    def test_p010_record_schema_violation(self) -> None:
        self.edit("facts", lambda d: d["records"][0].pop("statement"))
        self.assertIn("P010", self.issues())

    def test_p012_duplicate_id(self) -> None:
        self.edit("facts", lambda d: d["records"].append(copy.deepcopy(d["records"][0])))
        self.assertIn("P012", self.issues())

    def test_p013_source_in_unknown_workspace(self) -> None:
        self.edit("sources", lambda d: by_id(d, "src:manual-v10")["locator"].update(workspace="other"))
        self.assertIn("P013", self.issues())

    def test_p020_dangling_reference(self) -> None:
        self.edit("facts", lambda d: by_id(d, "fct:posting-purpose")["supported_by"].append("evd:nope"))
        self.assertIn("P020", self.issues())

    def test_p021_evidence_from_another_source(self) -> None:
        self.edit(
            "facts",
            lambda d: by_id(d, "fct:picker-lists-accounts")["epistemic"]["provenance"][0].update(evidence_id="evd:posting-title"),
        )
        self.assertIn("P021", self.issues())

    def test_p022_pinned_hash_differs(self) -> None:
        self.edit(
            "facts",
            lambda d: by_id(d, "fct:manual-says-delete")["epistemic"]["provenance"][0].update(source_sha256="0" * 64),
        )
        self.assertIn("P022", self.issues())

    def test_p022_when_the_source_changed_after_observation(self) -> None:
        self.edit("sources", lambda d: by_id(d, "src:manual-v10").update(sha256="0" * 64))
        self.assertIn("P022", self.issues())

    def test_p022_when_the_source_has_no_hash_but_one_was_pinned(self) -> None:
        self.edit("sources", lambda d: by_id(d, "src:manual-v10").pop("sha256"))
        self.assertIn("P022", self.issues())

    def test_p023_repeated_element_keys(self) -> None:
        def mutate(data):
            scr = by_id(data, "scr:posting-window")
            scr["elements"][1]["key"] = scr["elements"][0]["key"]

        self.edit("screens", mutate)
        self.assertIn("P023", self.issues())

    def test_p024_interaction_on_a_missing_element(self) -> None:
        self.edit("interactions", lambda d: by_id(d, "int:save-persists")["trigger"].update(element_key="nope"))
        self.assertIn("P024", self.issues())

    def test_p024_lookup_on_a_missing_element(self) -> None:
        self.edit("lookups", lambda d: by_id(d, "lku:account")["from"].update(element_key="nope"))
        self.assertIn("P024", self.issues())

    def test_p026_repeated_step_order(self) -> None:
        self.edit("workflows", lambda d: by_id(d, "wfl:post-entry")["steps"][1].update(order=1))
        self.assertIn("P026", self.issues())

    def test_p027_chosen_position_is_not_a_position(self) -> None:
        self.edit("contradictions", lambda d: by_id(d, "ctr:save-label")["resolution"].update(chosen_ref="fct:posting-purpose"))
        self.assertIn("P027", self.issues())

    def test_p028_open_contradiction_without_quarantine(self) -> None:
        self.edit("facts", lambda d: by_id(d, "fct:capture-no-delete")["epistemic"].pop("quarantine"))
        self.assertIn("P028", self.issues())

    def test_p028_when_quarantine_points_elsewhere(self) -> None:
        self.edit(
            "facts",
            lambda d: by_id(d, "fct:capture-no-delete")["epistemic"]["quarantine"].update(ref="ctr:save-label"),
        )
        self.assertIn("P028", self.issues())

    def test_p029_resolved_contradiction_keeps_quarantine(self) -> None:
        quarantine = {"reason": "contradiction", "ref": "ctr:save-label", "since": "2026-10-06T09:00:00-03:00"}
        self.edit("facts", lambda d: by_id(d, "fct:button-renamed")["epistemic"].update(quarantine=quarantine))
        self.assertIn("P029", self.issues())

    def test_p031_open_validation_review_on_an_already_validated_hypothesis(self) -> None:
        def mutate(data):
            rev = by_id(data, "rev:validate-balance")
            rev["state"] = "OPEN"
            rev.pop("answer")

        self.edit("reviews", mutate)
        self.assertIn("P031", self.issues())

    def test_p032_validator_differs_from_who_answered(self) -> None:
        self.edit("reviews", lambda d: by_id(d, "rev:validate-balance")["answer"].update(answered_by="Outra Pessoa"))
        self.assertIn("P032", self.issues())

    def test_p032_validation_linked_to_a_non_human_review(self) -> None:
        self.edit("business-rules", lambda d: by_id(d, "rul:balance")["epistemic"]["validation"].update(review_id="rev:gap-cost-center"))
        self.assertIn("P032", self.issues())

    def test_p033_review_validates_a_record_that_does_not_point_back(self) -> None:
        self.edit("business-rules", lambda d: by_id(d, "rul:balance")["epistemic"]["validation"].update(review_id="rev:gap-cost-center"))
        self.assertIn("P033", self.issues())

    def test_p034_release_between_equal_versions(self) -> None:
        self.edit("releases", lambda d: by_id(d, "rel:save-renamed")["to_version"].update(label="1.0"))
        self.assertIn("P034", self.issues())

    def test_p035_unresolved_grouping_cannot_be_validated(self) -> None:
        validation = {"validated_by": "Maria Example", "validated_at": "2026-10-14T10:00:00-03:00", "method": "runtime_test"}

        def mutate(data):
            ep = by_id(data, "scr:posting-window")["epistemic"]
            ep["status"] = "VALIDATED"
            ep["validation"] = validation

        self.edit("screens", mutate)
        self.assertIn("P035", self.issues())

    def test_p036_person_answer_with_agent_identifier(self) -> None:
        self.edit("reviews", lambda d: by_id(d, "rev:validate-balance")["answer"].update(answered_by="agent:reader"))
        self.assertIn("P036", self.issues())

    def test_p040_agent_cannot_validate(self) -> None:
        self.edit("business-rules", lambda d: by_id(d, "rul:balance")["epistemic"]["validation"].update(validated_by="agent:reader"))
        self.assertIn("P040", self.issues())


if __name__ == "__main__":
    unittest.main()
