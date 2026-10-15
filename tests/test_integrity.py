"""Integridade de fontes: "eu processei exatamente esta fonte"."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.helpers import FIXTURE, TempDirCase, link_directory, load_json
from core.hashing import hash_bytes
from core.integrity import (
    APPEARED,
    MISSING,
    MUTATED,
    UNDECLARED,
    UNREADABLE,
    ExpectedSource,
    IntegrityError,
    IntegrityReport,
    expect_absent,
    expect_present,
    expected_from_source_records,
    require_intact,
    verify_sources,
)
from core.paths import PathPortabilityError

FILES = {
    "evidence/one.txt": b"primeira evidencia\n",
    "evidence/two.bin": bytes(range(200)),
    "docs/manual.md": "# Manual\n\nação\n".encode("utf-8"),
}


def kinds(report: IntegrityReport) -> list[tuple[str, str]]:
    return [(f.kind, f.path) for f in report.findings]


class Base(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "root"
        for name, data in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.expected = [expect_present(name, hash_bytes(data)) for name, data in FILES.items()]

    def verify(self, **options) -> IntegrityReport:
        return verify_sources(self.root, self.expected, **options)


class Pristine(Base):
    def test_everything_matches(self) -> None:
        report = self.verify()
        self.assertTrue(report.ok)
        self.assertEqual((report.checked, report.findings, report.duplicates), (3, (), ()))
        self.assertIn("confirmada", report.summary())
        report.raise_if_failed()

    def test_order_of_expectations_is_irrelevant(self) -> None:
        self.assertEqual(verify_sources(self.root, list(reversed(self.expected))), self.verify())

    def test_verification_is_read_only(self) -> None:
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.verify(detect_duplicates=True, undeclared_within=["."])
        after = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["root"])

    def test_expectation_with_matching_byte_length(self) -> None:
        digest = hash_bytes(FILES["evidence/two.bin"])
        self.assertTrue(verify_sources(self.root, [expect_present("evidence/two.bin", digest)]).ok)


class SourceMutation(Base):
    def test_every_byte_position_flip_is_detected(self) -> None:
        target = self.root / "evidence" / "one.txt"
        original = FILES["evidence/one.txt"]
        for index in range(len(original)):
            mutated = bytearray(original)
            mutated[index] ^= 0x01
            target.write_bytes(bytes(mutated))
            with self.subTest(index=index):
                report = self.verify()
                self.assertEqual(kinds(report), [(MUTATED, "evidence/one.txt")])
        target.write_bytes(original)
        self.assertTrue(self.verify().ok)

    def test_append_truncate_and_same_size_replacement(self) -> None:
        target = self.root / "evidence" / "two.bin"
        original = FILES["evidence/two.bin"]
        for label, data in (("append", original + b"\x00"), ("truncate", original[:-1]), ("empty", b""), ("swap", original[::-1])):
            target.write_bytes(data)
            with self.subTest(label):
                self.assertEqual(kinds(self.verify()), [(MUTATED, "evidence/two.bin")])

    def test_finding_carries_expected_and_actual_hashes(self) -> None:
        (self.root / "docs" / "manual.md").write_bytes(b"outro")
        (finding,) = self.verify().findings
        self.assertEqual(finding.expected, f"sha256:{hash_bytes(FILES['docs/manual.md']).digest}")
        self.assertEqual(finding.actual, str(hash_bytes(b"outro")))

    def test_wrong_declared_length_alone_is_a_mutation(self) -> None:
        real = hash_bytes(FILES["evidence/two.bin"])
        wrong = ExpectedSource("evidence/two.bin", real.digest, real.byte_length + 1)
        (finding,) = verify_sources(self.root, [wrong]).findings
        self.assertEqual(finding.kind, MUTATED)
        self.assertIn("bytes esperados", finding.detail)

    def test_mutation_is_not_silent_for_frozen_evidence(self) -> None:
        processed: list[str] = []

        def process_frozen_evidence() -> None:
            require_intact(self.root, self.expected)
            processed.append("lido")

        process_frozen_evidence()
        self.assertEqual(processed, ["lido"])
        (self.root / "evidence" / "one.txt").write_bytes(b"adulterado")
        with self.assertRaises(IntegrityError) as ctx:
            process_frozen_evidence()
        self.assertEqual(processed, ["lido"], "o processamento não pode continuar depois do achado")
        self.assertEqual(kinds(ctx.exception.report), [(MUTATED, "evidence/one.txt")])
        self.assertIn("MUTATED", str(ctx.exception))
        self.assertIn("evidence/one.txt", str(ctx.exception))


class MissingAndAppeared(Base):
    def test_missing_source(self) -> None:
        (self.root / "docs" / "manual.md").unlink()
        report = self.verify()
        self.assertEqual(kinds(report), [(MISSING, "docs/manual.md")])
        self.assertFalse(report.ok)
        with self.assertRaises(IntegrityError):
            report.raise_if_failed()

    def test_everything_missing(self) -> None:
        report = verify_sources(self.tmp / "nowhere", self.expected)
        self.assertEqual({f.kind for f in report.findings}, {MISSING})
        self.assertEqual(len(report.findings), 3)

    def test_expected_absence_that_holds_is_fine(self) -> None:
        self.assertTrue(verify_sources(self.root, [expect_absent("layers/not-yet.json")]).ok)

    def test_expected_absence_that_no_longer_holds(self) -> None:
        late = self.root / "layers" / "late.json"
        late.parent.mkdir()
        late.write_text("{}", encoding="utf-8")
        self.assertEqual(kinds(verify_sources(self.root, [expect_absent("layers/late.json")])), [(APPEARED, "layers/late.json")])

    def test_directory_where_a_file_is_expected_is_unreadable(self) -> None:
        target = self.root / "evidence" / "one.txt"
        target.unlink()
        target.mkdir()
        self.assertEqual(kinds(self.verify()), [(UNREADABLE, "evidence/one.txt")])

    def test_findings_are_sorted_and_repeatable(self) -> None:
        (self.root / "docs" / "manual.md").unlink()
        (self.root / "evidence" / "two.bin").write_bytes(b"x")
        first, second = self.verify(), self.verify()
        self.assertEqual(first, second)
        self.assertEqual(kinds(first), [(MISSING, "docs/manual.md"), (MUTATED, "evidence/two.bin")])

    def test_reports_never_contain_the_local_root(self) -> None:
        (self.root / "docs" / "manual.md").unlink()
        (self.root / "evidence" / "one.txt").unlink()
        (self.root / "evidence" / "one.txt").mkdir()
        (self.root / "evidence" / "two.bin").write_bytes(b"x")
        report = self.verify()
        text = report.summary() + repr(report)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(self.tmp.name, text)

    def test_unreadable_detail_does_not_leak_the_system_error_text(self) -> None:
        from unittest import mock

        with mock.patch("core.integrity.hash_file", side_effect=PermissionError(13, "Permission denied", str(self.root / "x"))):
            report = self.verify()
        self.assertEqual({f.kind for f in report.findings}, {UNREADABLE})
        self.assertNotIn(str(self.tmp), repr(report))


class SymlinkEscape(Base):
    def test_a_link_pointing_outside_the_root_is_unreadable_not_followed(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "one.txt").write_bytes(FILES["evidence/one.txt"])
        link_directory(self.root / "evidence" / "linked", outside)
        expected = [expect_present("evidence/linked/one.txt", hash_bytes(FILES["evidence/one.txt"]))]
        self.assertEqual(kinds(verify_sources(self.root, expected)), [(UNREADABLE, "evidence/linked/one.txt")])


class DuplicatesAndUndeclared(Base):
    def test_duplicate_content_is_informational(self) -> None:
        copy = self.root / "evidence" / "one-copy.txt"
        copy.write_bytes(FILES["evidence/one.txt"])
        self.expected.append(expect_present("evidence/one-copy.txt", hash_bytes(FILES["evidence/one.txt"])))
        report = self.verify(detect_duplicates=True)
        self.assertTrue(report.ok, "duplicata não é violação")
        self.assertEqual(report.duplicates, (("evidence/one-copy.txt", "evidence/one.txt"),))
        self.assertEqual(self.verify().duplicates, ())

    def test_no_duplicates_when_all_contents_differ(self) -> None:
        self.assertEqual(self.verify(detect_duplicates=True).duplicates, ())

    def test_undeclared_file_under_a_requested_scope(self) -> None:
        (self.root / "evidence" / "extra.txt").write_text("x", encoding="utf-8")
        self.assertEqual(kinds(self.verify(undeclared_within=["evidence"])), [(UNDECLARED, "evidence/extra.txt")])
        self.assertEqual(kinds(self.verify(undeclared_within=["."])), [(UNDECLARED, "evidence/extra.txt")])
        self.assertTrue(self.verify(undeclared_within=["docs"]).ok, "fora do escopo pedido não é achado")
        self.assertTrue(self.verify().ok, "sem escopo, nada é procurado")

    def test_nested_undeclared_files_are_found_in_order(self) -> None:
        for name in ("evidence/deep/z.txt", "evidence/deep/a.txt", "evidence/b.txt"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        found = [f.path for f in self.verify(undeclared_within=["evidence"]).findings]
        self.assertEqual(found, ["evidence/b.txt", "evidence/deep/a.txt", "evidence/deep/z.txt"])

    def test_overlapping_scopes_report_each_file_once(self) -> None:
        (self.root / "evidence" / "extra.txt").write_text("x", encoding="utf-8")
        report = self.verify(undeclared_within=[".", "evidence", "evidence"])
        self.assertEqual(kinds(report), [(UNDECLARED, "evidence/extra.txt")])

    def test_expected_absent_file_that_exists_is_appeared_not_undeclared(self) -> None:
        path = self.root / "layers" / "late.json"
        path.parent.mkdir()
        path.write_text("{}", encoding="utf-8")
        report = verify_sources(self.root, [*self.expected, expect_absent("layers/late.json")], undeclared_within=["layers"])
        self.assertEqual(kinds(report), [(APPEARED, "layers/late.json")])

    def test_bad_scope_is_a_caller_error(self) -> None:
        for bad in ("../outside", "/abs", "C:\\x"):
            with self.subTest(bad=bad), self.assertRaises(PathPortabilityError):
                self.verify(undeclared_within=[bad])

    def test_scope_that_does_not_exist_finds_nothing(self) -> None:
        self.assertTrue(self.verify(undeclared_within=["not/there"]).ok)

    @unittest.skipUnless(os.name == "nt", "caminho acima de MAX_PATH só se aplica ao Windows")
    def test_the_scan_reaches_files_beyond_max_path(self) -> None:
        import shutil

        from core.atomic import atomic_write_text
        from core.paths import to_fs_path

        top = "nivel0-" + "d" * 50
        deep = "/".join(f"nivel{i}-" + "d" * 50 for i in range(6))
        self.addCleanup(lambda: shutil.rmtree(to_fs_path(self.root / top, tree=True), ignore_errors=True))
        atomic_write_text(self.root / deep / "extra.txt", "x")
        self.assertEqual(kinds(self.verify(undeclared_within=["."])), [(UNDECLARED, deep + "/extra.txt")])


class ExpectationValidation(unittest.TestCase):
    SHA = "ab" * 32

    def test_valid(self) -> None:
        self.assertEqual(ExpectedSource("a/b", self.SHA, 3).byte_length, 3)
        self.assertIsNone(ExpectedSource("a/b").sha256)

    def test_invalid(self) -> None:
        bad_cases = [
            lambda: ExpectedSource("a\\b", self.SHA),
            lambda: ExpectedSource("/abs", self.SHA),
            lambda: ExpectedSource("a/b", self.SHA.upper()),
            lambda: ExpectedSource("a/b", "ab"),
            lambda: ExpectedSource("a/b", self.SHA, -1),
            lambda: ExpectedSource("a/b", self.SHA, True),
            lambda: ExpectedSource("a/b", None, 3),
        ]
        for build in bad_cases:
            with self.subTest(), self.assertRaises(ValueError):
                build()

    def test_factories_normalize_the_path(self) -> None:
        self.assertEqual(expect_present("a\\b//c.txt", self.SHA).path, "a/b/c.txt")
        self.assertEqual(expect_absent("./x/y").path, "x/y")
        digest = hash_bytes(b"abc")
        item = expect_present("f", digest)
        self.assertEqual((item.sha256, item.byte_length), (digest.digest, 3))

    def test_conflicting_expectations_for_one_path_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            verify_sources(".", [ExpectedSource("a", self.SHA), ExpectedSource("a", "cd" * 32)])

    def test_identical_repeated_expectations_are_deduplicated(self) -> None:
        report = verify_sources(Path("."), [ExpectedSource("zz-no-such-file", self.SHA)] * 3)
        self.assertEqual(report.checked, 1)

    def test_expectations_must_be_expected_sources(self) -> None:
        with self.assertRaises(TypeError):
            verify_sources(".", [("a", self.SHA)])  # type: ignore[list-item]


class FromSourceRecords(Base):
    """Integra com os registros `source` da Foundation (segundo software: o pacote sintético)."""

    def records(self) -> list[dict]:
        return load_json(FIXTURE / "sources.json")["records"]

    def test_unpinned_sources_cannot_be_asserted_by_default(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            expected_from_source_records(self.records())
        self.assertIn("src:expert-interview-01", str(ctx.exception))

    def test_pinned_records_become_expectations(self) -> None:
        expected = expected_from_source_records(self.records(), require_pinned=False)
        self.assertEqual(len(expected), 6)
        self.assertEqual(expected[0].path, "synthetic/shot-posting-01.png")
        self.assertEqual(expected[0].sha256, "a1b2c3d4e5f60718" * 4)

    def test_fixture_sources_do_not_exist_so_everything_is_missing(self) -> None:
        expected = expected_from_source_records(self.records(), require_pinned=False)
        report = verify_sources(self.root, expected)
        self.assertEqual({f.kind for f in report.findings}, {MISSING})
        self.assertEqual(len(report.findings), 6)

    def test_real_files_with_the_wrong_pin_are_mutations(self) -> None:
        expected = expected_from_source_records(self.records(), require_pinned=False)
        for item in expected:
            path = self.root / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"conteudo real que nao tem o hash fixado")
        report = verify_sources(self.root, expected)
        self.assertEqual({f.kind for f in report.findings}, {MUTATED})

    def test_real_files_with_the_right_pin_pass(self) -> None:
        records = []
        for index, (name, data) in enumerate(FILES.items()):
            records.append({"id": f"src:r{index}", "kind": "documentation", "locator": {"workspace": "pack", "path": name},
                            "sha256": hash_bytes(data).digest, "contains_sensitive_data": False})
        expected = expected_from_source_records(records)
        require_intact(self.root, expected)

    def test_records_from_several_workspaces_need_a_choice(self) -> None:
        records = [
            {"id": "src:a", "locator": {"workspace": "one", "path": "a.txt"}, "sha256": "ab" * 32},
            {"id": "src:b", "locator": {"workspace": "two", "path": "b.txt"}, "sha256": "cd" * 32},
        ]
        with self.assertRaises(ValueError):
            expected_from_source_records(records)
        self.assertEqual([e.path for e in expected_from_source_records(records, workspace="two")], ["b.txt"])


if __name__ == "__main__":
    unittest.main()
