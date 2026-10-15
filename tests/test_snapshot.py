"""KnowledgeSnapshot: identidade determinística, esquema, adulteração, deriva e verificação em disco."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import itertools
import json
import shutil
import unittest

from tests.helpers import REPO, TempDirCase, load_json
from core.hashing import Digest, hash_bytes, hash_file
from core.integrity import APPEARED, MISSING, MUTATED, UNDECLARED, IntegrityError
from core.paths import PathPortabilityError, find_local_paths
from core.schema_lite import Registry
from core.serialization import CanonicalJsonError, loads_strict
from core import snapshot as snap
from core.snapshot import (
    ABSENT,
    PRESENT,
    ArtifactEntry,
    ArtifactSpec,
    KnowledgeSnapshot,
    SnapshotError,
    build_snapshot,
    diff_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_directory,
    snapshot_from_dict,
    validate_snapshot_dict,
    verify_snapshot,
)

# Valores congelados do contrato de identidade (identity_version 1). Se algum mudar, a identidade mudou.
GOLDEN_WITH_SOFTWARE = "snap:sha256:36750cd45702365d0456c8ac346fa13069586cf4b1d3a2381d9a214875bceb60"
GOLDEN_WITHOUT_SOFTWARE = "snap:sha256:a6168839fdeff410db6ad7c8d68eabf53e17b9885d7f39a0a51b950cf874e955"
GOLDEN_ARTIFACTS_DIGEST = {
    "algorithm": "sha256",
    "digest": "62ed3ffa7401776818da690f319e3ea9fd2140e5fa94ababc8541e8afb543688",
    "byte_length": 369,
}


def golden_entries() -> list[ArtifactEntry]:
    return [
        ArtifactEntry.from_bytes("schemas/beta.json", "schema", b'{"b":1}'),
        ArtifactEntry.absent("layers/gamma.json", "layer"),
        ArtifactEntry.from_bytes("docs/alpha.txt", "doc", b"alpha"),
    ]


def make(entries=None, **overrides) -> KnowledgeSnapshot:
    options = dict(created_from_kind="declared-list", created_from_label="teste", software_id="zeta-erp")
    options.update(overrides)
    return build_snapshot(golden_entries() if entries is None else entries, **options)


class FrozenIdentity(unittest.TestCase):
    def test_golden_ids(self) -> None:
        self.assertEqual(make().snapshot_id, GOLDEN_WITH_SOFTWARE)
        self.assertEqual(make(software_id=None).snapshot_id, GOLDEN_WITHOUT_SOFTWARE)
        self.assertEqual(make().artifacts_digest.to_dict(), GOLDEN_ARTIFACTS_DIGEST)

    def test_identity_recomputed_from_first_principles_with_only_hashlib_and_json(self) -> None:
        entries = [
            {"path": "docs/alpha.txt", "role": "doc", "status": "PRESENT", "sha256": hashlib.sha256(b"alpha").hexdigest(), "byte_length": 5},
            {"path": "layers/gamma.json", "role": "layer", "status": "ABSENT"},
            {"path": "schemas/beta.json", "role": "schema", "status": "PRESENT", "sha256": hashlib.sha256(b'{"b":1}').hexdigest(), "byte_length": 7},
        ]
        raw = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        artifacts = hashlib.sha256(raw).hexdigest()
        payload = {"kind": "knowledge-snapshot", "identity_version": 1, "artifacts_digest": artifacts, "software_id": "zeta-erp"}
        expected = "snap:sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        shot = make()
        self.assertEqual(shot.snapshot_id, expected)
        self.assertEqual(shot.artifacts_digest.digest, artifacts)
        self.assertEqual(shot.artifacts_digest.byte_length, len(raw))

    def test_id_format(self) -> None:
        self.assertRegex(make().snapshot_id, r"^snap:sha256:[0-9a-f]{64}$")


class Determinism(unittest.TestCase):
    def test_entry_order_never_matters(self) -> None:
        reference = make()
        for order in itertools.permutations(golden_entries()):
            shot = make(list(order))
            self.assertEqual(shot.snapshot_id, reference.snapshot_id)
            self.assertEqual(shot.to_dict(), reference.to_dict())

    def test_entries_are_stored_sorted_by_logical_path(self) -> None:
        self.assertEqual([e.path for e in make().entries], ["docs/alpha.txt", "layers/gamma.json", "schemas/beta.json"])

    def test_repeated_builds_are_equal(self) -> None:
        self.assertEqual(make(), make())
        self.assertEqual({make().snapshot_id for _ in range(5)}, {GOLDEN_WITH_SOFTWARE})

    def test_timestamp_does_not_change_identity(self) -> None:
        stamps = [None, "2026-01-01T00:00:00Z", "2030-12-31T23:59:59.123456+02:00", "1999-05-05T10:00:00-03:00"]
        shots = [make(created_at=stamp) for stamp in stamps]
        self.assertEqual({s.snapshot_id for s in shots}, {GOLDEN_WITH_SOFTWARE})
        self.assertEqual({s.artifacts_digest for s in shots}, {make().artifacts_digest})
        self.assertEqual(len({json.dumps(s.to_dict(), sort_keys=True) for s in shots}), len(stamps), "o timestamp aparece como metadado")

    def test_generation_metadata_and_provenance_do_not_change_identity(self) -> None:
        parent = make(entries=[ArtifactEntry.absent("x.json", "layer")])
        variants = [
            make(generator="outro-gerador/9.9"),
            make(notes="qualquer observação"),
            make(created_from_kind="pack", created_from_label="rótulo diferente"),
            make(created_from_kind="derived", created_from_label="x"),
            make(parent=parent),
            make(parent=parent.snapshot_id),
        ]
        self.assertEqual({v.snapshot_id for v in variants}, {GOLDEN_WITH_SOFTWARE})

    def test_everything_that_is_content_changes_identity(self) -> None:
        base = make().snapshot_id
        entries = golden_entries()
        changed = [
            make(software_id="omega-crm"),
            make(software_id=None),
            make(entries[:-1]),
            make(entries + [ArtifactEntry.absent("extra.json", "layer")]),
            make([ArtifactEntry.from_bytes("docs/alpha.txt", "doc", b"alphA"), *entries[:2]]),
            make([ArtifactEntry.from_bytes("docs/alpha.txt", "note", b"alpha"), *entries[:2]]),
            make([ArtifactEntry.from_bytes("docs/renamed.txt", "doc", b"alpha"), *entries[:2]]),
            make([ArtifactEntry.from_bytes("layers/gamma.json", "layer", b"{}"), entries[0], entries[2]]),
        ]
        ids = [s.snapshot_id for s in changed]
        self.assertNotIn(base, ids)
        self.assertEqual(len(set(ids)), len(ids), "cada mudança de conteúdo gera uma identidade própria")

    def test_an_absent_layer_that_appears_changes_the_id(self) -> None:
        before = make([ArtifactEntry.absent("layers/human.json", "layer")])
        after = make([ArtifactEntry.from_bytes("layers/human.json", "layer", b"{}")])
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)

    def test_core_never_asks_the_clock_the_machine_or_chance(self) -> None:
        from unittest import mock

        def forbidden(*_a, **_k):
            raise AssertionError("o núcleo não pode consultar relógio, máquina ou acaso")

        with mock.patch("time.time", forbidden), mock.patch("time.time_ns", forbidden), \
                mock.patch("uuid.uuid4", forbidden), mock.patch("random.random", forbidden), \
                mock.patch("socket.gethostname", forbidden), mock.patch("platform.node", forbidden):
            shot = make(created_at="2026-10-15T12:00:00Z")
            self.assertEqual(snapshot_from_dict(shot.to_dict()), shot)
            self.assertEqual(shot.snapshot_id, GOLDEN_WITH_SOFTWARE)


class DirectorySnapshots(TempDirCase):
    SPECS = [
        ArtifactSpec("docs/manual.md", "doc"),
        ArtifactSpec("data/records.json", "records"),
        ("layers/human.json", "layer"),
    ]

    def build_tree(self, root, human: bool = False) -> None:
        (root / "docs").mkdir(parents=True)
        (root / "data").mkdir()
        (root / "docs" / "manual.md").write_bytes("# Manual\nação\n".encode("utf-8"))
        (root / "data" / "records.json").write_bytes(b'{"a":1}')
        if human:
            (root / "layers").mkdir()
            (root / "layers" / "human.json").write_bytes(b"{}")

    def test_statuses_and_hashes(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        shot = snapshot_directory(root, self.SPECS, label="workspace de teste", software_id="zeta-erp")
        by_path = {e.path: e for e in shot.entries}
        self.assertEqual(by_path["docs/manual.md"].status, PRESENT)
        self.assertEqual(by_path["docs/manual.md"].sha256, hash_file(root / "docs" / "manual.md").digest)
        self.assertEqual(by_path["data/records.json"].byte_length, 7)
        self.assertEqual(by_path["layers/human.json"].status, ABSENT)
        self.assertIsNone(by_path["layers/human.json"].sha256)
        self.assertEqual((shot.created_from_kind, shot.created_from_label), ("directory", "workspace de teste"))

    def test_no_local_path_anywhere_in_the_snapshot(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        shot = snapshot_directory(root, self.SPECS, label="ws")
        text = json.dumps(shot.to_dict(), ensure_ascii=False)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(self.tmp.name, text)
        self.assertEqual(find_local_paths(shot.to_dict()), [])

    def test_identity_is_independent_of_the_absolute_location(self) -> None:
        first, second = self.tmp / "primeiro" / "lugar", self.tmp / "outro" / "caminho" / "bem" / "mais" / "fundo"
        self.build_tree(first, human=True)
        shutil.copytree(first, second)
        a = snapshot_directory(first, self.SPECS, label="a", software_id="zeta-erp")
        b = snapshot_directory(second, self.SPECS, label="b", software_id="zeta-erp")
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        self.assertEqual(a.entries, b.entries)

    def test_declaration_order_and_path_spelling_do_not_matter(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        ordered = snapshot_directory(root, self.SPECS, label="x")
        shuffled = snapshot_directory(root, [("layers\\human.json", "layer"), ("./data//records.json", "records"), ("docs/manual.md", "doc")], label="x")
        self.assertEqual(ordered.snapshot_id, shuffled.snapshot_id)

    def test_source_mutation_changes_identity_and_is_detected(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        before = snapshot_directory(root, self.SPECS, label="x")
        (root / "data" / "records.json").write_bytes(b'{"a":2}')
        after = snapshot_directory(root, self.SPECS, label="x")
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)
        report = verify_snapshot(before, root)
        self.assertEqual([(f.kind, f.path) for f in report.findings], [(MUTATED, "data/records.json")])

    def test_a_layer_appearing_is_a_state_change_and_a_finding(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        before = snapshot_directory(root, self.SPECS, label="x")
        (root / "layers").mkdir()
        (root / "layers" / "human.json").write_bytes(b"{}")
        after = snapshot_directory(root, self.SPECS, label="x")
        self.assertNotEqual(before.snapshot_id, after.snapshot_id)
        self.assertEqual([(f.kind, f.path) for f in verify_snapshot(before, root).findings], [(APPEARED, "layers/human.json")])
        self.assertTrue(verify_snapshot(after, root).ok)

    def test_directory_in_place_of_file_is_an_error(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        (root / "layers" / "human.json").mkdir(parents=True)
        with self.assertRaises(SnapshotError):
            snapshot_directory(root, self.SPECS, label="x")

    def test_bad_declared_paths_are_refused(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        for bad in ("../x", "/etc/passwd", "C:\\x", ""):
            with self.subTest(bad=bad), self.assertRaises(PathPortabilityError):
                snapshot_directory(root, [(bad, "doc")], label="x")

    def test_local_path_in_label_or_notes_is_refused(self) -> None:
        root = self.tmp / "ws"
        self.build_tree(root)
        with self.assertRaises(SnapshotError):
            snapshot_directory(root, self.SPECS, label=str(root))
        with self.assertRaises(SnapshotError):
            snapshot_directory(root, self.SPECS, label="ok", notes="veio de C:\\MR-AI\\x")

    def test_a_missing_root_yields_an_all_absent_snapshot(self) -> None:
        shot = snapshot_directory(self.tmp / "nowhere", self.SPECS, label="x")
        self.assertEqual({e.status for e in shot.entries}, {ABSENT})

    def test_copy_of_a_real_foundation_pack_has_the_same_identity_anywhere(self) -> None:
        names = ["manifest.json", "software.json", "sources.json", "facts.json", "screens.json", "layers/not-yet.json"]
        specs = [(name, "pack-file") for name in names]
        first = self.tmp / "copy-one"
        second = self.tmp / "x" / "y" / "copy-two"
        for target in (first, second):
            shutil.copytree(REPO / "evaluations" / "fixtures" / "acme-ledger", target)
        a = snapshot_directory(first, specs, label="pack", software_id="acme-ledger")
        b = snapshot_directory(second, specs, label="pack", software_id="acme-ledger")
        original = snapshot_directory(REPO / "evaluations" / "fixtures" / "acme-ledger", specs, label="pack", software_id="acme-ledger")
        self.assertEqual(a.snapshot_id, b.snapshot_id)
        self.assertEqual(a.snapshot_id, original.snapshot_id)
        self.assertEqual(find_local_paths(a.to_dict()), [])


class EntryInvariants(unittest.TestCase):
    def test_valid_entries(self) -> None:
        present = ArtifactEntry.present("a/b.txt", "doc", hash_bytes(b"x"))
        self.assertEqual((present.status, present.byte_length), (PRESENT, 1))
        absent = ArtifactEntry.absent("a/c.txt", "layer")
        self.assertEqual((absent.status, absent.sha256, absent.byte_length), (ABSENT, None, None))
        self.assertEqual(absent.to_dict(), {"path": "a/c.txt", "role": "layer", "status": "ABSENT"})

    def test_invalid_entries(self) -> None:
        sha = "ab" * 32
        cases = [
            lambda: ArtifactEntry("a\\b", "doc", PRESENT, sha, 1),
            lambda: ArtifactEntry("/a", "doc", ABSENT),
            lambda: ArtifactEntry("a", "Doc", ABSENT),
            lambda: ArtifactEntry("a", "", ABSENT),
            lambda: ArtifactEntry("a", "doc", "UNREADABLE"),
            lambda: ArtifactEntry("a", "doc", PRESENT),
            lambda: ArtifactEntry("a", "doc", PRESENT, sha),
            lambda: ArtifactEntry("a", "doc", PRESENT, sha, -1),
            lambda: ArtifactEntry("a", "doc", PRESENT, sha, True),
            lambda: ArtifactEntry("a", "doc", PRESENT, sha.upper(), 1),
            lambda: ArtifactEntry("a", "doc", ABSENT, sha),
            lambda: ArtifactEntry("a", "doc", ABSENT, None, 0),
        ]
        for build in cases:
            with self.subTest(), self.assertRaises(ValueError):
                build()

    def test_from_dict_is_strict(self) -> None:
        good = ArtifactEntry.present("a", "doc", hash_bytes(b"x")).to_dict()
        self.assertEqual(ArtifactEntry.from_dict(good).path, "a")
        for bad in ({**good, "extra": 1}, {k: v for k, v in good.items() if k != "role"}, [], None, "x"):
            with self.subTest(bad=bad), self.assertRaises(SnapshotError):
                ArtifactEntry.from_dict(bad)

    def test_from_bytes_equals_present_of_the_same_hash(self) -> None:
        self.assertEqual(ArtifactEntry.from_bytes("a", "doc", b"zz"), ArtifactEntry.present("a", "doc", hash_bytes(b"zz")))


class SnapshotInvariants(unittest.TestCase):
    def test_duplicate_and_case_colliding_paths_are_refused(self) -> None:
        a = ArtifactEntry.absent("docs/a.txt", "doc")
        with self.assertRaises(SnapshotError):
            make([a, ArtifactEntry.from_bytes("docs/a.txt", "doc", b"x")])
        with self.assertRaises(SnapshotError):
            make([a, ArtifactEntry.absent("docs/A.txt", "doc")])

    def test_entries_must_be_artifact_entries(self) -> None:
        with self.assertRaises(SnapshotError):
            make([{"path": "a"}])  # type: ignore[list-item]

    def test_direct_construction_checks_everything(self) -> None:
        good = make()
        for field, value in (
            ("snapshot_id", "snap:sha256:" + "0" * 64),
            ("artifacts_digest", Digest("sha256", "0" * 64, 1)),
            ("software_id", "outro-software"),
            ("entries", good.entries[:-1]),
            ("entries", tuple(reversed(good.entries))),
            ("created_from_kind", "elsewhere"),
            ("created_from_label", ""),
            ("created_from_label", "x" * 201),
            ("generator", ""),
            ("created_at", "ontem"),
            ("created_at", "2026-13-01T00:00:00Z"),
            ("notes", ""),
            ("parent_snapshot_id", "snap:sha256:xyz"),
        ):
            with self.subTest(field=field, value=str(value)[:30]), self.assertRaises(SnapshotError):
                dataclasses.replace(good, **{field: value})

    def test_a_snapshot_cannot_be_its_own_parent(self) -> None:
        good = make()
        with self.assertRaises(SnapshotError):
            dataclasses.replace(good, parent_snapshot_id=good.snapshot_id)

    def test_parent_by_object_checks_the_software(self) -> None:
        other = make(software_id="omega-crm")
        with self.assertRaises(SnapshotError):
            make(parent=other)
        with self.assertRaises(SnapshotError):
            make(parent=make(software_id=None))
        self.assertEqual(make(parent=make(entries=[])).parent_snapshot_id, make(entries=[]).snapshot_id)

    def test_empty_snapshot_is_valid_and_stable(self) -> None:
        empty = make(entries=[])
        self.assertEqual(empty.entries, ())
        self.assertEqual(validate_snapshot_dict(empty.to_dict()), [])
        self.assertEqual(snapshot_from_dict(empty.to_dict()), empty)
        self.assertEqual(empty.artifacts_digest, Digest("sha256", hashlib.sha256(b"[]").hexdigest(), 2))

    def test_entry_lookup(self) -> None:
        shot = make()
        self.assertEqual(shot.entry("docs/alpha.txt").role, "doc")
        self.assertIsNone(shot.entry("nope"))


class SchemaContract(unittest.TestCase):
    def good(self) -> dict:
        return make(created_at="2026-10-15T12:00:00-03:00", notes="ok", parent=make(entries=[])).to_dict()

    def test_schema_is_registered_and_clean(self) -> None:
        self.assertIn("urn:mr-software-intelligence:schema:knowledge-snapshot:1", Registry().by_id)
        self.assertEqual(Registry().check_all(), [])

    def test_valid_documents(self) -> None:
        self.assertEqual(validate_snapshot_dict(self.good()), [])
        self.assertEqual(validate_snapshot_dict(make(software_id=None).to_dict()), [])

    def test_every_required_top_level_key_is_required(self) -> None:
        for key in ("schema", "snapshot_id", "identity_version", "created_from", "entries", "artifacts_digest", "generation"):
            doc = self.good()
            del doc[key]
            with self.subTest(key=key):
                self.assertTrue(validate_snapshot_dict(doc))

    def test_optional_keys_may_be_absent(self) -> None:
        for key in ("software_id", "parent_snapshot_id"):
            doc = self.good()
            del doc[key]
            with self.subTest(key=key):
                self.assertEqual(validate_snapshot_dict(doc), [])

    def test_unknown_properties_are_rejected_everywhere(self) -> None:
        for path in ((), ("created_from",), ("generation",), ("artifacts_digest",), ("entries", 0)):
            doc = self.good()
            node = doc
            for step in path:
                node = node[step]
            node["surprise"] = 1
            with self.subTest(path=path):
                self.assertTrue(validate_snapshot_dict(doc))

    def test_wrong_values(self) -> None:
        mutations = {
            "schema": lambda d: d.__setitem__("schema", "urn:other"),
            "identity_version": lambda d: d.__setitem__("identity_version", 2),
            "identity_version_bool": lambda d: d.__setitem__("identity_version", True),
            "snapshot_id": lambda d: d.__setitem__("snapshot_id", "sha256:" + "0" * 64),
            "snapshot_id_upper": lambda d: d.__setitem__("snapshot_id", "snap:sha256:" + "A" * 64),
            "parent": lambda d: d.__setitem__("parent_snapshot_id", "snap:sha256:short"),
            "software_id": lambda d: d.__setitem__("software_id", "Zeta ERP"),
            "kind": lambda d: d["created_from"].__setitem__("kind", "disk"),
            "label_empty": lambda d: d["created_from"].__setitem__("label", ""),
            "generator_missing": lambda d: d["generation"].pop("generator"),
            "created_at": lambda d: d["generation"].__setitem__("created_at", "15/10/2026"),
            "algorithm": lambda d: d["artifacts_digest"].__setitem__("algorithm", "md5"),
            "digest": lambda d: d["artifacts_digest"].__setitem__("digest", "xyz"),
            "length": lambda d: d["artifacts_digest"].__setitem__("byte_length", -1),
            "entries_type": lambda d: d.__setitem__("entries", {}),
            "status": lambda d: d["entries"][0].__setitem__("status", "UNREADABLE"),
            "role": lambda d: d["entries"][0].__setitem__("role", "Has Space"),
        }
        for name, mutate in mutations.items():
            doc = self.good()
            mutate(doc)
            with self.subTest(name=name):
                self.assertTrue(validate_snapshot_dict(doc), name)

    def test_present_and_absent_shapes(self) -> None:
        doc = self.good()
        present = next(e for e in doc["entries"] if e["status"] == PRESENT)
        absent = next(e for e in doc["entries"] if e["status"] == ABSENT)
        broken = copy.deepcopy(doc)
        del next(e for e in broken["entries"] if e["status"] == PRESENT)["sha256"]
        self.assertTrue(validate_snapshot_dict(broken))
        broken = copy.deepcopy(doc)
        del next(e for e in broken["entries"] if e["status"] == PRESENT)["byte_length"]
        self.assertTrue(validate_snapshot_dict(broken))
        broken = copy.deepcopy(doc)
        next(e for e in broken["entries"] if e["status"] == ABSENT)["sha256"] = present["sha256"]
        self.assertTrue(validate_snapshot_dict(broken))
        broken = copy.deepcopy(doc)
        next(e for e in broken["entries"] if e["status"] == ABSENT)["byte_length"] = 0
        self.assertTrue(validate_snapshot_dict(broken))
        self.assertNotIn("sha256", absent)

    def test_unsafe_paths_are_rejected_by_the_schema_too(self) -> None:
        for bad in ("/abs", "a/../b", "a\\b", "a/", "a//b", "C:/x", "a\x00b"):
            doc = self.good()
            doc["entries"][0]["path"] = bad
            with self.subTest(bad=bad):
                self.assertTrue(validate_snapshot_dict(doc))

    def test_python_constants_and_schema_patterns_are_the_same_contract(self) -> None:
        schema = load_json(REPO / "schemas" / "knowledge-snapshot.schema.json")
        common = load_json(REPO / "schemas" / "common.schema.json")
        self.assertEqual(schema["$defs"]["snapshotId"]["pattern"], snap.SNAPSHOT_ID_PATTERN)
        self.assertEqual(schema["$defs"]["entry"]["properties"]["role"]["pattern"], snap.ROLE_PATTERN)
        self.assertEqual(common["$defs"]["softwareId"]["pattern"], snap.SOFTWARE_ID_PATTERN)
        self.assertEqual(schema["properties"]["created_from"]["properties"]["kind"]["enum"], list(snap.CREATED_FROM_KINDS))
        self.assertEqual(schema["properties"]["schema"]["const"], snap.SCHEMA_ID)
        self.assertEqual(schema["$id"], snap.SCHEMA_ID)
        self.assertEqual(schema["properties"]["identity_version"]["const"], snap.IDENTITY_VERSION)

    def test_schema_contains_no_software_or_provider_term_or_absolute_path(self) -> None:
        text = (REPO / "schemas" / "knowledge-snapshot.schema.json").read_text(encoding="utf-8")
        self.assertNotRegex(text.casefold(), r"syscom|acme|claude|openai|kiro|anthropic|gemini|copilot|chatgpt")
        self.assertNotRegex(text, r"[A-Za-z]:\\\\[A-Za-z]")


class TamperDetection(unittest.TestCase):
    """Teste de mutação: alterar qualquer campo que participa da identidade tem de ser flagrado."""

    def test_round_trip(self) -> None:
        shot = make(created_at="2026-10-15T12:00:00Z", notes="n", parent=make(entries=[]))
        self.assertEqual(snapshot_from_dict(shot.to_dict()), shot)
        self.assertEqual(snapshot_from_dict(json.loads(json.dumps(shot.to_dict()))), shot)

    def test_each_identity_field_mutation_is_detected(self) -> None:
        base = make().to_dict()
        mutations = {
            "entry_sha256": lambda d: d["entries"][0].__setitem__("sha256", "0" * 64),
            "entry_length": lambda d: d["entries"][0].__setitem__("byte_length", 6),
            "entry_role": lambda d: d["entries"][0].__setitem__("role", "other"),
            "entry_path": lambda d: d["entries"][0].__setitem__("path", "docs/other.txt"),
            "entry_removed": lambda d: d["entries"].pop(),
            "entry_added": lambda d: d["entries"].append({"path": "zz.json", "role": "layer", "status": "ABSENT"}),
            "entry_status": lambda d: d["entries"][1].update(status="PRESENT", sha256="1" * 64, byte_length=2),
            "entries_reordered": lambda d: d["entries"].reverse(),
            "software_id": lambda d: d.__setitem__("software_id", "omega-crm"),
            "software_removed": lambda d: d.pop("software_id"),
            "snapshot_id": lambda d: d.__setitem__("snapshot_id", "snap:sha256:" + "f" * 64),
            "artifacts_digest": lambda d: d["artifacts_digest"].__setitem__("digest", "e" * 64),
            "artifacts_length": lambda d: d["artifacts_digest"].__setitem__("byte_length", 1),
        }
        for name, mutate in mutations.items():
            doc = copy.deepcopy(base)
            mutate(doc)
            with self.subTest(name=name), self.assertRaises(SnapshotError):
                snapshot_from_dict(doc)

    def test_metadata_mutations_are_not_identity_but_must_stay_valid(self) -> None:
        doc = make().to_dict()
        doc["generation"]["created_at"] = "2030-01-01T00:00:00Z"
        doc["generation"]["notes"] = "alterado"
        doc["created_from"]["label"] = "outro rótulo"
        self.assertEqual(snapshot_from_dict(doc).snapshot_id, GOLDEN_WITH_SOFTWARE)

    def test_local_path_smuggled_into_metadata_is_refused(self) -> None:
        doc = make().to_dict()
        doc["generation"]["notes"] = "gerado em C:\\MR-AI\\algum\\lugar"
        with self.assertRaises(SnapshotError):
            snapshot_from_dict(doc)

    def test_non_dict_input(self) -> None:
        for bad in (None, [], "x", 5):
            with self.subTest(bad=bad), self.assertRaises(SnapshotError):
                snapshot_from_dict(bad)


class Persistence(TempDirCase):
    def test_saved_file_is_canonical_pretty_json(self) -> None:
        shot = make(created_at="2026-10-15T12:00:00Z")
        target = self.tmp / "snap" / "state.json"
        self.assertEqual(save_snapshot(target, shot).status, "CREATED")
        raw = target.read_bytes()
        self.assertTrue(raw.endswith(b"}\n"))
        self.assertNotIn(b"\r", raw)
        self.assertEqual(loads_strict(raw), shot.to_dict())
        self.assertEqual(save_snapshot(target, shot).status, "NO_CHANGE")

    def test_load_returns_the_same_snapshot(self) -> None:
        shot = make(parent=make(entries=[]))
        target = self.tmp / "state.json"
        save_snapshot(target, shot)
        self.assertEqual(load_snapshot(target), shot)

    def test_saved_bytes_do_not_depend_on_dictionary_order(self) -> None:
        shot = make()
        first, second = self.tmp / "1.json", self.tmp / "2.json"
        save_snapshot(first, shot)
        reordered = dict(reversed(list(shot.to_dict().items())))
        from core.atomic import atomic_write_json

        atomic_write_json(second, reordered)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_tampered_file_is_refused(self) -> None:
        shot = make()
        target = self.tmp / "state.json"
        save_snapshot(target, shot)
        text = target.read_text(encoding="utf-8").replace(shot.entries[0].sha256, "0" * 64)
        target.write_text(text, encoding="utf-8")
        with self.assertRaises(SnapshotError):
            load_snapshot(target)

    def test_duplicate_keys_in_a_stored_file_are_refused(self) -> None:
        target = self.tmp / "state.json"
        save_snapshot(target, make())
        text = target.read_text(encoding="utf-8")
        target.write_text(text.replace('"identity_version": 1,', '"identity_version": 1,\n  "identity_version": 1,'), encoding="utf-8")
        with self.assertRaises(CanonicalJsonError):
            load_snapshot(target)

    def test_unserializable_snapshot_state_cannot_be_saved(self) -> None:
        shot = make()
        with self.assertRaises(CanonicalJsonError):
            from core.atomic import atomic_write_json

            atomic_write_json(self.tmp / "x.json", {**shot.to_dict(), "bad": float("nan")})
        self.assertEqual(list(self.tmp.iterdir()), [])


class Drift(unittest.TestCase):
    def kinds(self, old: KnowledgeSnapshot, new: KnowledgeSnapshot) -> list[tuple[str, str]]:
        return [(c.path, c.kind) for c in diff_snapshots(old, new).changes]

    def test_no_change(self) -> None:
        diff = diff_snapshots(make(), make(created_at="2026-10-15T12:00:00Z"))
        self.assertTrue(diff.same)
        self.assertEqual(diff.changes, ())

    def test_every_kind_of_change(self) -> None:
        old = make([
            ArtifactEntry.from_bytes("a.txt", "doc", b"1"),
            ArtifactEntry.from_bytes("b.txt", "doc", b"1"),
            ArtifactEntry.absent("c.json", "layer"),
            ArtifactEntry.from_bytes("d.json", "layer", b"1"),
            ArtifactEntry.from_bytes("e.txt", "doc", b"1"),
            ArtifactEntry.from_bytes("f.txt", "doc", b"1"),
        ])
        new = make([
            ArtifactEntry.from_bytes("a.txt", "doc", b"2"),
            ArtifactEntry.from_bytes("b.txt", "doc", b"1"),
            ArtifactEntry.from_bytes("c.json", "layer", b"1"),
            ArtifactEntry.absent("d.json", "layer"),
            ArtifactEntry.from_bytes("f.txt", "note", b"1"),
            ArtifactEntry.from_bytes("g.txt", "doc", b"1"),
        ])
        self.assertEqual(self.kinds(old, new), [
            ("a.txt", snap.MODIFIED), ("c.json", snap.APPEARED), ("d.json", snap.DISAPPEARED),
            ("e.txt", snap.REMOVED), ("f.txt", snap.ROLE_CHANGED), ("g.txt", snap.ADDED),
        ])

    def test_change_carries_before_and_after_states(self) -> None:
        old = make([ArtifactEntry.absent("c.json", "layer")])
        new = make([ArtifactEntry.from_bytes("c.json", "layer", b"1")])
        (change,) = diff_snapshots(old, new).changes
        self.assertEqual((change.before, change.after), ("ABSENT", f"sha256:{hash_bytes(b'1').digest}"))

    def test_role_and_content_change_together_are_two_changes(self) -> None:
        old = make([ArtifactEntry.from_bytes("a", "doc", b"1")])
        new = make([ArtifactEntry.from_bytes("a", "note", b"2")])
        self.assertEqual(self.kinds(old, new), [("a", snap.MODIFIED), ("a", snap.ROLE_CHANGED)])

    def test_changed_set_matches_the_reference_drift_semantics(self) -> None:
        """Referência: o conjunto de caminhos cujo (situação, hash) difere entre dois estados."""
        old = make([ArtifactEntry.from_bytes(f"f{i}", "doc", bytes([i])) for i in range(6)])
        new = make([
            ArtifactEntry.from_bytes("f0", "doc", b"\x00"), ArtifactEntry.from_bytes("f1", "doc", b"x"),
            ArtifactEntry.absent("f2", "doc"), ArtifactEntry.from_bytes("f3", "doc", b"\x03"),
            ArtifactEntry.from_bytes("f4", "doc", b"\x04"), ArtifactEntry.from_bytes("f6", "doc", b"\x06"),
        ])
        def state(shot: KnowledgeSnapshot, path: str):
            entry = shot.entry(path)
            return None if entry is None else (entry.status, entry.sha256)

        paths = {e.path for e in old.entries} | {e.path for e in new.entries}
        reference = {path for path in paths if state(old, path) != state(new, path)}
        self.assertEqual(reference, {"f1", "f2", "f5", "f6"})
        self.assertEqual({c.path for c in diff_snapshots(old, new).changes}, reference)

    def test_snapshots_of_different_software_do_not_compare(self) -> None:
        with self.assertRaises(SnapshotError):
            diff_snapshots(make(software_id="zeta-erp"), make(software_id="omega-crm"))
        with self.assertRaises(SnapshotError):
            diff_snapshots(make(software_id="zeta-erp"), make(software_id=None))


class VerifyAgainstDisk(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "ws"
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "a.md").write_bytes(b"alpha")
        (self.root / "docs" / "b.md").write_bytes(b"beta")
        self.shot = snapshot_directory(self.root, [("docs/a.md", "doc"), ("docs/b.md", "doc"), ("layers/x.json", "layer")], label="ws")

    def test_pristine(self) -> None:
        report = verify_snapshot(self.shot, self.root)
        self.assertTrue(report.ok)
        self.assertEqual(report.checked, 3)

    def test_mutation_missing_and_appearance(self) -> None:
        (self.root / "docs" / "a.md").write_bytes(b"alphA")
        (self.root / "docs" / "b.md").unlink()
        (self.root / "layers").mkdir()
        (self.root / "layers" / "x.json").write_bytes(b"{}")
        report = verify_snapshot(self.shot, self.root)
        self.assertEqual([(f.kind, f.path) for f in report.findings],
                         [(MUTATED, "docs/a.md"), (MISSING, "docs/b.md"), (APPEARED, "layers/x.json")])
        with self.assertRaises(IntegrityError):
            report.raise_if_failed()

    def test_undeclared_files_and_duplicates_are_optional_extras(self) -> None:
        (self.root / "docs" / "c.md").write_bytes(b"alpha")
        self.assertTrue(verify_snapshot(self.shot, self.root).ok)
        report = verify_snapshot(self.shot, self.root, undeclared_within=["docs"])
        self.assertEqual([(f.kind, f.path) for f in report.findings], [(UNDECLARED, "docs/c.md")])
        (self.root / "docs" / "dup.md").write_bytes(b"alpha")
        report = verify_snapshot(self.shot, self.root, detect_duplicates=True, undeclared_within=["docs"])
        self.assertEqual(len([f for f in report.findings if f.kind == UNDECLARED]), 2)

    def test_the_snapshot_can_be_verified_after_moving_the_workspace(self) -> None:
        moved = self.tmp / "elsewhere" / "deeper" / "ws2"
        moved.parent.mkdir(parents=True)
        shutil.copytree(self.root, moved)
        self.assertTrue(verify_snapshot(self.shot, moved).ok)

    def test_verification_uses_the_stored_entries_not_a_re_scan(self) -> None:
        saved = self.tmp / "state.json"
        save_snapshot(saved, self.shot)
        (self.root / "docs" / "a.md").write_bytes(b"changed")
        report = verify_snapshot(load_snapshot(saved), self.root)
        self.assertEqual([(f.kind, f.path) for f in report.findings], [(MUTATED, "docs/a.md")])


if __name__ == "__main__":
    unittest.main()
