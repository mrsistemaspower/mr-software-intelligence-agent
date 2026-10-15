"""Proteção de raízes: READ_ONLY, FROZEN e MUTABLE_OUTPUT; detecção de mutação sem reverter nada."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempDirCase, link_directory
from core.paths import PathPortabilityError
from core.protection import (
    POINT_END,
    POINT_STAGE,
    Protection,
    ProtectionError,
    ProtectionGuard,
    RootPolicy,
    Verify,
    Violation,
)

RO, FZ, MU = Protection.READ_ONLY, Protection.FROZEN, Protection.MUTABLE_OUTPUT


class Policies(unittest.TestCase):
    def test_defaults(self) -> None:
        self.assertEqual(RootPolicy("src", RO).verify, Verify.EACH_STAGE)
        self.assertEqual(RootPolicy("src", FZ).verify, Verify.EACH_STAGE)
        self.assertEqual(RootPolicy("out", MU).verify, Verify.NONE)
        self.assertEqual(RootPolicy("src", RO).scope, (".",))

    def test_frozen_cannot_switch_verification_off(self) -> None:
        with self.assertRaises(ProtectionError):
            RootPolicy("src", FZ, Verify.NONE)

    def test_read_only_may_verify_only_at_the_end_or_not_at_all(self) -> None:
        self.assertEqual(RootPolicy("src", RO, "RUN_END").verify, Verify.RUN_END)
        self.assertEqual(RootPolicy("src", RO, "NONE").verify, Verify.NONE)

    def test_mutable_output_has_no_integrity_to_verify_and_no_scope(self) -> None:
        with self.assertRaises(ProtectionError):
            RootPolicy("out", MU, Verify.EACH_STAGE)
        with self.assertRaises(ProtectionError):
            RootPolicy("out", MU, None, ("sub",))

    def test_invalid_values_are_rejected(self) -> None:
        for args in (("BAD", RO), ("src", "WRITABLE"), ("src", RO, "SOMETIMES"), ("src", RO, None, ()), ("src", RO, None, "sub"),
                     ("src", RO, None, ("../x",)), ("src", RO, None, ("/abs",)), ("src", RO, None, ("C:/x",))):
            with self.subTest(args), self.assertRaises(ProtectionError):
                RootPolicy(*args)  # type: ignore[arg-type]

    def test_scope_is_normalized_and_deduplicated(self) -> None:
        self.assertEqual(RootPolicy("src", RO, None, ("b\\c", "a", "b/c", "a/")).scope, ("a", "b/c"))

    def test_round_trip(self) -> None:
        policy = RootPolicy("src", FZ, Verify.RUN_END, ("a", "b"))
        self.assertEqual(RootPolicy.from_dict(policy.to_dict()), policy)
        with self.assertRaises(ProtectionError):
            RootPolicy.from_dict({"name": "src"})
        with self.assertRaises(ProtectionError):
            RootPolicy.from_dict({"name": "src", "protection": "READ_ONLY", "extra": 1})

    def test_string_values_are_coerced_to_the_enums(self) -> None:
        policy = RootPolicy("src", "FROZEN", "EACH_STAGE")  # type: ignore[arg-type]
        self.assertIs(policy.protection, FZ)


class GuardCase(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.src = self.tmp / "src"
        self.out = self.tmp / "out"
        (self.src / "sub").mkdir(parents=True)
        self.out.mkdir()
        (self.src / "a.txt").write_text("alpha")
        (self.src / "sub" / "b.txt").write_text("beta")
        self.baselines = self.out / "baselines"

    def guard(self, policies=None, software_id="synthetic-one", **bindings) -> ProtectionGuard:
        policies = policies or [RootPolicy("src", RO), RootPolicy("out", MU)]
        paths = {"src": self.src, "out": self.out}
        paths.update(bindings)
        return ProtectionGuard(policies, paths, baseline_dir=self.baselines, software_id=software_id)

    def kinds(self, violations) -> list:
        return sorted((v.kind, v.path) for v in violations)


class Configuration(GuardCase):
    def test_overlapping_roots_are_rejected(self) -> None:
        inner = self.src / "sub"
        with self.assertRaises(ProtectionError):
            ProtectionGuard([RootPolicy("src", RO), RootPolicy("inner", MU)], {"src": self.src, "inner": inner}, baseline_dir=self.baselines)
        with self.assertRaises(ProtectionError):
            ProtectionGuard([RootPolicy("a", RO), RootPolicy("b", RO)], {"a": self.src, "b": self.src}, baseline_dir=self.baselines)

    def test_bindings_must_match_the_policies(self) -> None:
        with self.assertRaises(ProtectionError):
            ProtectionGuard([RootPolicy("src", RO)], {}, baseline_dir=self.baselines)
        with self.assertRaises(ProtectionError):
            ProtectionGuard([RootPolicy("src", RO)], {"src": self.src, "extra": self.out}, baseline_dir=self.baselines)

    def test_duplicate_root_names_are_rejected(self) -> None:
        with self.assertRaises(ProtectionError):
            ProtectionGuard([RootPolicy("src", RO), RootPolicy("src", MU)], {"src": self.src}, baseline_dir=self.baselines)

    def test_a_protected_root_must_exist(self) -> None:
        with self.assertRaises(ProtectionError):
            self.guard(src=self.tmp / "nowhere")

    def test_writing_is_granted_only_to_mutable_output_roots(self) -> None:
        guard = self.guard([RootPolicy("src", RO), RootPolicy("out", MU), RootPolicy("ice", FZ)], ice=self._frozen_dir())
        self.assertEqual(guard.output_path("out", "x/y.txt"), (self.out / "x" / "y.txt").resolve())
        for root in ("src", "ice"):
            with self.subTest(root), self.assertRaises(ProtectionError):
                guard.output_path(root, "x.txt")
        with self.assertRaises(ProtectionError):
            guard.output_path("ghost", "x.txt")

    def _frozen_dir(self) -> Path:
        path = self.tmp / "ice"
        path.mkdir(exist_ok=True)
        (path / "evidence.txt").write_text("fixed")
        return path

    def test_paths_cannot_escape_the_root(self) -> None:
        guard = self.guard()
        for logical in ("../outside.txt", "/abs.txt", "a/../../x"):
            with self.subTest(logical), self.assertRaises(PathPortabilityError):
                guard.output_path("out", logical)
            with self.subTest(logical), self.assertRaises(PathPortabilityError):
                guard.read_path("src", logical)

    def test_a_link_pointing_outside_the_root_is_refused(self) -> None:
        link_directory(self.out / "escape", self.tmp)
        with self.assertRaises(PathPortabilityError):
            self.guard().output_path("out", "escape/leak.txt")

    def test_verify_requires_begin(self) -> None:
        with self.assertRaises(ProtectionError):
            self.guard().verify(POINT_STAGE)

    def test_unknown_point_is_rejected(self) -> None:
        guard = self.guard()
        guard.begin()
        with self.assertRaises(ValueError):
            guard.verify("SOMETIME")


class ReadOnly(GuardCase):
    def test_untouched_tree_verifies_clean(self) -> None:
        guard = self.guard()
        records, found = guard.begin()
        self.assertEqual(found, ())
        self.assertEqual(guard.verify(POINT_STAGE), ())
        self.assertEqual(guard.verify(POINT_END), ())
        by_root = {r.root: r for r in records}
        self.assertEqual(by_root["src"].entries, 2)
        self.assertRegex(by_root["src"].baseline_snapshot_id, r"^snap:sha256:[0-9a-f]{64}$")
        self.assertIsNone(by_root["out"].baseline_snapshot_id)

    def test_modification_is_detected_and_never_reverted(self) -> None:
        guard = self.guard()
        guard.begin()
        (self.src / "a.txt").write_text("someone edited this")
        found = guard.verify(POINT_STAGE)
        self.assertEqual(self.kinds(found), [("MUTATED", "a.txt")])
        self.assertEqual((self.src / "a.txt").read_text(), "someone edited this", "o agente não desfaz o que não é dele")

    def test_deletion_addition_and_replacement_are_detected(self) -> None:
        guard = self.guard()
        guard.begin()
        (self.src / "a.txt").unlink()
        (self.src / "new.txt").write_text("surprise")
        (self.src / "sub" / "b.txt").unlink()
        (self.src / "sub" / "b.txt").mkdir()
        self.assertEqual(self.kinds(guard.verify(POINT_STAGE)), [("MISSING", "a.txt"), ("UNDECLARED", "new.txt"), ("UNREADABLE", "sub/b.txt")])

    def test_a_new_baseline_is_taken_every_run(self) -> None:
        first = self.guard()
        first.begin()
        (self.src / "a.txt").write_text("edited between runs")
        second = self.guard()
        records, found = second.begin()
        self.assertEqual(found, ())
        self.assertEqual(second.verify(POINT_END), (), "mudança ENTRE execuções é legítima para READ_ONLY")

    def test_violations_carry_no_content_and_no_local_path(self) -> None:
        guard = self.guard()
        guard.begin()
        (self.src / "a.txt").write_text("SECRET-CONTENT")
        (found,) = guard.verify(POINT_STAGE)
        text = json.dumps(found.to_dict())
        self.assertNotIn("SECRET", text)
        self.assertNotIn(str(self.tmp), text)
        self.assertEqual(Violation.from_dict(found.to_dict()), found)
        self.assertEqual(str(found), "MUTATED src:a.txt")

    def test_verification_modes(self) -> None:
        for mode, at_stage, at_end in (("EACH_STAGE", True, True), ("RUN_END", False, True), ("NONE", False, False)):
            with self.subTest(mode):
                (self.src / "a.txt").write_text("alpha")
                guard = self.guard([RootPolicy("src", RO, mode), RootPolicy("out", MU)])
                guard.begin()
                (self.src / "a.txt").write_text("changed " + mode)
                self.assertEqual(bool(guard.verify(POINT_STAGE)), at_stage)
                self.assertEqual(bool(guard.verify(POINT_END)), at_end)

    def test_scope_limits_what_is_watched(self) -> None:
        guard = self.guard([RootPolicy("src", RO, None, ("a.txt",)), RootPolicy("out", MU)])
        guard.begin()
        (self.src / "sub" / "b.txt").write_text("outside the scope")
        (self.src / "stray.txt").write_text("also outside")
        self.assertEqual(guard.verify(POINT_STAGE), ())
        (self.src / "a.txt").write_text("inside")
        self.assertEqual(self.kinds(guard.verify(POINT_STAGE)), [("MUTATED", "a.txt")])

    def test_directory_scope_sees_new_files_below_it_only(self) -> None:
        guard = self.guard([RootPolicy("src", RO, None, ("sub",)), RootPolicy("out", MU)])
        guard.begin()
        (self.src / "top.txt").write_text("outside")
        self.assertEqual(guard.verify(POINT_STAGE), ())
        (self.src / "sub" / "fresh.txt").write_text("inside")
        self.assertEqual(self.kinds(guard.verify(POINT_STAGE)), [("UNDECLARED", "sub/fresh.txt")])

    def test_a_scoped_path_that_did_not_exist_is_flagged_when_it_appears(self) -> None:
        guard = self.guard([RootPolicy("src", RO, None, ("later.txt",)), RootPolicy("out", MU)])
        guard.begin()
        self.assertEqual(guard.verify(POINT_STAGE), ())
        (self.src / "later.txt").write_text("hello")
        self.assertEqual(self.kinds(guard.verify(POINT_STAGE)), [("APPEARED", "later.txt")])

    def test_an_empty_tree_is_fine(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        guard = self.guard([RootPolicy("src", RO), RootPolicy("out", MU)], src=empty)
        records, found = guard.begin()
        self.assertEqual((found, guard.verify(POINT_STAGE)), ((), ()))
        (empty / "x").write_text("1")
        self.assertEqual(self.kinds(guard.verify(POINT_STAGE)), [("UNDECLARED", "x")])

    def test_mutable_roots_are_never_checked(self) -> None:
        guard = self.guard()
        guard.begin()
        (self.out / "anything.txt").write_text("the agent writes here")
        self.assertEqual(guard.verify(POINT_END), ())


class Frozen(GuardCase):
    def setUp(self) -> None:
        super().setUp()
        self.ice = self._ice()

    def _ice(self) -> Path:
        path = self.tmp / "ice"
        (path / "docs").mkdir(parents=True)
        (path / "docs" / "manual.txt").write_text("v1")
        (path / "data.txt").write_text("rows")
        return path

    def frozen_guard(self, software_id: str = "synthetic-one") -> ProtectionGuard:
        return ProtectionGuard(
            [RootPolicy("ice", FZ), RootPolicy("out", MU)],
            {"ice": self.ice, "out": self.out},
            baseline_dir=self.baselines,
            software_id=software_id,
        )

    def test_first_run_records_and_persists_the_baseline(self) -> None:
        guard = self.frozen_guard()
        records, found = guard.begin()
        self.assertEqual(found, ())
        self.assertTrue((self.baselines / "ice.snapshot.json").is_file())
        self.assertEqual({r.root: r.entries for r in records}["ice"], 2)

    def test_baseline_survives_across_runs_and_catches_changes_between_them(self) -> None:
        self.frozen_guard().begin()
        (self.ice / "data.txt").write_text("rows, edited")
        records, found = self.frozen_guard().begin()
        self.assertEqual(self.kinds(found), [("MUTATED", "data.txt")])
        self.assertEqual((self.ice / "data.txt").read_text(), "rows, edited", "nada é revertido")

    def test_restoring_the_evidence_lets_the_next_run_proceed(self) -> None:
        self.frozen_guard().begin()
        (self.ice / "data.txt").write_text("changed")
        self.assertTrue(self.frozen_guard().begin()[1])
        (self.ice / "data.txt").write_text("rows")
        self.assertEqual(self.frozen_guard().begin()[1], ())

    def test_added_removed_and_replaced_files_are_all_violations(self) -> None:
        self.frozen_guard().begin()
        (self.ice / "extra.txt").write_text("new")
        (self.ice / "docs" / "manual.txt").unlink()
        self.assertEqual(self.kinds(self.frozen_guard().begin()[1]), [("MISSING", "docs/manual.txt"), ("UNDECLARED", "extra.txt")])

    def test_the_stored_baseline_is_not_rewritten_by_later_runs(self) -> None:
        self.frozen_guard().begin()
        before = (self.baselines / "ice.snapshot.json").read_bytes()
        (self.ice / "data.txt").write_text("changed")
        self.frozen_guard().begin()
        self.assertEqual((self.baselines / "ice.snapshot.json").read_bytes(), before)

    def test_tampered_baseline_is_refused_not_trusted(self) -> None:
        self.frozen_guard().begin()
        path = self.baselines / "ice.snapshot.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entries"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ProtectionError):
            self.frozen_guard().begin()

    def test_garbage_baseline_is_refused(self) -> None:
        self.frozen_guard().begin()
        (self.baselines / "ice.snapshot.json").write_bytes(b"\x00\x01 not json")
        with self.assertRaises(ProtectionError):
            self.frozen_guard().begin()

    def test_a_baseline_from_another_software_is_refused(self) -> None:
        self.frozen_guard().begin()
        with self.assertRaises(ProtectionError):
            self.frozen_guard(software_id="synthetic-two").begin()

    def test_frozen_is_checked_after_every_stage_by_default(self) -> None:
        guard = self.frozen_guard()
        guard.begin()
        (self.ice / "data.txt").write_text("mutated mid-run")
        self.assertEqual(self.kinds(guard.verify(POINT_STAGE)), [("MUTATED", "data.txt")])


class Limits(GuardCase):
    """Sistemas de arquivos diferentes aceitam nomes que outros não aceitam. A varredura é simulada para que o teste
    rode em qualquer sistema, inclusive onde o nome nem pode ser criado."""

    def begin_with_listing(self, names: list):
        root = self.tmp / "listed"
        root.mkdir(exist_ok=True)
        guard = self.guard([RootPolicy("src", RO), RootPolicy("out", MU)], src=root)
        with mock.patch("core.protection.os.walk", side_effect=lambda start: [(start, [], list(names))]):
            return guard.begin()

    def test_non_portable_names_prevent_capture_instead_of_being_silently_skipped(self) -> None:
        for name in ("trailing.", "bad:name.txt", "con", "what?.txt", "has\x01control"):
            with self.subTest(name), self.assertRaises(ProtectionError):
                self.begin_with_listing(["ok.txt", name])

    def test_case_variants_cannot_be_described_portably(self) -> None:
        with self.assertRaises(ProtectionError):
            self.begin_with_listing(["File.txt", "file.txt"])

    def test_portable_names_are_accepted(self) -> None:
        records, found = self.begin_with_listing(["a.txt", "B.txt"])
        self.assertEqual(found, ())
        self.assertEqual({r.root: r.entries for r in records}["src"], 2)

    def test_a_link_inside_the_tree_that_points_outside_is_refused(self) -> None:
        outside = self.tmp / "outside-target.txt"
        outside.write_text("secret")
        try:
            (self.src / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("ligação simbólica indisponível neste ambiente")
        with self.assertRaises(ProtectionError):
            self.guard().begin()


if __name__ == "__main__":
    unittest.main()
