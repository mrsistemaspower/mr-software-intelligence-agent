"""Portabilidade de caminhos: lógico x local, normalização, detecção de caminho local, caminho longo do Windows."""
from __future__ import annotations

import ntpath
import os
import posixpath
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

from tests.helpers import TempDirCase, link_directory
from core import pack
from core.paths import (
    PathPortabilityError,
    _windows_extended,
    assert_portable,
    comparison_key,
    find_local_paths,
    is_logical_path,
    looks_like_local_path,
    normalize_logical_path,
    resolve_logical,
    to_fs_path,
    to_logical_path,
)
from core.schema_lite import Registry, Validator

VALID = {
    "a/b.txt": "a/b.txt",
    "a\\b.txt": "a/b.txt",
    "./a//b/./c.txt": "a/b/c.txt",
    "a/b/": "a/b",
    "dir\\sub\\": "dir/sub",
    "a b/c d.txt": "a b/c d.txt",
    "a/..x/c.png": "a/..x/c.png",
    ".hidden/file": ".hidden/file",
    "x": "x",
    "console.txt": "console.txt",
    "com10/x": "com10/x",
    "aux1": "aux1",
    "nullable/x": "nullable/x",
    "ação/ü.txt": "ação/ü.txt",
    "漢字/😀.md": "漢字/😀.md",
}
INVALID = [
    "", ".", "./", "/", "//", "/etc/x", "\\\\srv\\share\\x", "\\x", "C:\\x\\y.png", "C:/x", "C:x", "c:",
    "a/../b", "..", "a/..", "../x.png", "a\\..\\x.png", "a:b/c", "a/b:stream", "a\x00b", "a\nb", "a\tb", "a\x7fb",
    "a/b.", "a/b ", "a/b./c", "a/ /c", "CON", "con.txt", "a/NUL.md", "COM1", "lpt9.log", "Aux/x",
    "a?b", "a/*.txt", "a|b", "<x>", 'a"b',
]


class NormalizeLogicalPath(unittest.TestCase):
    def test_valid_inputs_normalize_to_one_form(self) -> None:
        for given, expected in VALID.items():
            with self.subTest(given=given):
                self.assertEqual(normalize_logical_path(given), expected)

    def test_normalization_is_idempotent(self) -> None:
        for expected in VALID.values():
            self.assertEqual(normalize_logical_path(expected), expected)

    def test_invalid_inputs_are_rejected(self) -> None:
        for given in INVALID:
            with self.subTest(given=given), self.assertRaises(PathPortabilityError):
                normalize_logical_path(given)

    def test_non_text_is_rejected(self) -> None:
        for given in (None, 5, Path("a/b"), b"a/b", ["a"]):
            with self.subTest(given=given), self.assertRaises(PathPortabilityError):
                normalize_logical_path(given)  # type: ignore[arg-type]

    def test_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(PathPortabilityError, ValueError))

    def test_is_logical_path_means_already_normalized(self) -> None:
        self.assertTrue(is_logical_path("a/b.txt"))
        for given in ("a\\b.txt", "./a", "a//b", "a/b/", "/a", "..", "", None, 5):
            with self.subTest(given=given):
                self.assertFalse(is_logical_path(given))

    def test_no_unicode_normalization_nfc_and_nfd_are_different_paths(self) -> None:
        nfc, nfd = "caf\u00e9/x", "cafe\u0301/x"
        self.assertEqual(normalize_logical_path(nfc), nfc)
        self.assertEqual(normalize_logical_path(nfd), nfd)
        self.assertNotEqual(normalize_logical_path(nfc), normalize_logical_path(nfd))

    def test_case_is_preserved(self) -> None:
        self.assertEqual(normalize_logical_path("Docs/ReadMe.MD"), "Docs/ReadMe.MD")

    def test_result_never_depends_on_the_host_os(self) -> None:
        for given in VALID:
            with self.subTest(given=given):
                result = normalize_logical_path(given)
                self.assertNotIn("\\", result)
                self.assertEqual(posixpath.normpath(result), result)
                self.assertFalse(PurePosixPath(result).is_absolute())
                self.assertFalse(PureWindowsPath(result).is_absolute())
                self.assertEqual(PureWindowsPath(result).drive, "")


class ConsistencyWithFoundationContracts(unittest.TestCase):
    """Os caminhos lógicos nunca podem ser mais frouxos do que os contratos que a Foundation já impõe."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = Validator(Registry())

    def source_errors(self, path: str) -> list[str]:
        record = {"id": "src:a", "kind": "screenshot", "locator": {"workspace": "pack", "path": path},
                  "contains_sensitive_data": None}
        return self.validator.validate(record, "source")

    def entry_errors(self, path: str) -> list[str]:
        schema = {"$ref": "urn:mr-software-intelligence:schema:knowledge-snapshot:1#/$defs/entry"}
        return self.validator.validate({"path": path, "role": "doc", "status": "ABSENT"}, schema)

    def test_every_normalized_valid_path_is_accepted_by_both_schemas(self) -> None:
        for path in VALID.values():
            with self.subTest(path=path):
                self.assertEqual(self.source_errors(path), [])
                self.assertEqual(self.entry_errors(path), [])

    def test_what_the_source_schema_rejects_is_rejected_here_too(self) -> None:
        for bad in ("C:\\x\\y.png", "C:/x/y.png", "/etc/x", "\\\\srv\\x", "../x.png", "a/../x.png", "a\\..\\x.png"):
            with self.subTest(bad=bad):
                self.assertTrue(self.source_errors(bad))
                with self.assertRaises(PathPortabilityError):
                    normalize_logical_path(bad)

    def test_snapshot_schema_rejects_the_structural_violations(self) -> None:
        for bad in ("/a", "a//b", "a/b/", "a/../b", "./a", "a/./b", "a\\b", "a:b", "a\nb", "a\x00b", "a/b.", "a/b ", "", "a?b", "<x>"):
            with self.subTest(bad=bad):
                self.assertTrue(self.entry_errors(bad))

    def test_package_validation_detector_is_a_subset_of_ours(self) -> None:
        samples = [
            "C:\\MR-AI\\x", "c:/users/x", "see D:\\temp\\f", "\\\\srv\\share", "/home/u/x", "/Users/me/x", "/mnt/c/x",
            "/var/log/x", "/opt/x", "/etc/passwd", "/usr/bin/env", "plain text", "a/b/c.txt", "http://example.com/x", "12:30:00",
        ]
        for text in samples:
            if pack.ABS_PATH_RE.search(text):
                with self.subTest(text=text):
                    self.assertTrue(looks_like_local_path(text))


class LocalPathDetection(unittest.TestCase):
    def test_positives(self) -> None:
        for text in ("C:\\MR-AI\\x", "c:/users/x", "see D:\\temp\\f", "\\\\srv\\share\\x", "file:///etc/hosts", "~/docs",
                     "~\\docs", "/home/user/x", "/Users/me/x", "/mnt/c/x", "/var/log/x", "/tmp/x", "/root/x", "x:/y",
                     "run C:\\tools\\a.exe now"):
            with self.subTest(text=text):
                self.assertTrue(looks_like_local_path(text))

    def test_negatives(self) -> None:
        for text in ("a/b/c.txt", "http://example.com/x", "https://host/path", "e.g. a:b", "12:30:00", "name:value", "/api/v1/items",
                     "a/home/b", "docs/etc/x", "relative\\path", "sha256:" + "a" * 64, "snap:sha256:abc", "art:t:sha256:ab", "", "C"):
            with self.subTest(text=text):
                self.assertFalse(looks_like_local_path(text))

    def test_non_text_is_never_a_path(self) -> None:
        self.assertFalse(looks_like_local_path(None))  # type: ignore[arg-type]
        self.assertFalse(looks_like_local_path(5))  # type: ignore[arg-type]

    def test_find_reports_locations_not_content(self) -> None:
        value = {"ok": "a/b", "bad": "C:\\MR-AI\\segredo", "list": ["fine", {"deep": "/home/maria/doc"}], "C:\\chave\\x": 1, "t": ("fine", "\\\\srv\\x")}
        found = find_local_paths(value)
        self.assertEqual(len(found), 4)
        joined = " ".join(found)
        self.assertIn("$.bad", joined)
        self.assertIn("$.list[1].deep", joined)
        self.assertIn("$.t[1]", joined)
        for secret in ("MR-AI", "segredo", "maria", "srv"):
            self.assertNotIn(secret, joined)

    def test_clean_values_pass(self) -> None:
        assert_portable({"a": ["x/y", 1, 2.5, None, True], "b": {"c": "sha256:" + "0" * 64}})

    def test_assert_portable_raises_and_does_not_echo_the_path(self) -> None:
        with self.assertRaises(PathPortabilityError) as ctx:
            assert_portable({"p": "C:\\MR-AI\\x"}, "meu valor")
        self.assertIn("meu valor", str(ctx.exception))
        self.assertIn("$.p", str(ctx.exception))
        self.assertNotIn("MR-AI", str(ctx.exception))

    def test_assert_portable_truncates_long_lists(self) -> None:
        with self.assertRaises(PathPortabilityError) as ctx:
            assert_portable([f"C:\\x{i}" for i in range(20)])
        self.assertIn("...", str(ctx.exception))


class LocalRoots(TempDirCase):
    def test_to_logical_path_inside_the_root(self) -> None:
        (self.tmp / "a" / "b").mkdir(parents=True)
        (self.tmp / "a" / "b" / "c.txt").write_text("x", encoding="utf-8")
        self.assertEqual(to_logical_path(self.tmp / "a" / "b" / "c.txt", self.tmp), "a/b/c.txt")
        self.assertEqual(to_logical_path(str(self.tmp / "a" / "b" / "c.txt"), str(self.tmp)), "a/b/c.txt")
        self.assertEqual(to_logical_path(self.tmp / "not" / "yet.txt", self.tmp), "not/yet.txt")

    def test_the_result_never_contains_the_root(self) -> None:
        result = to_logical_path(self.tmp / "a.txt", self.tmp)
        self.assertEqual(result, "a.txt")
        self.assertFalse(looks_like_local_path(result))

    def test_outside_the_root_never_falls_back_to_the_absolute_path(self) -> None:
        other = self.tmp / "other"
        root = self.tmp / "root"
        root.mkdir()
        other.mkdir()
        for outside in (other / "x.txt", self.tmp, root / ".." / "other" / "x.txt", Path(self.tmp.anchor)):
            with self.subTest(outside=str(outside)), self.assertRaises(PathPortabilityError) as ctx:
                to_logical_path(outside, root)
            self.assertNotIn(str(self.tmp), str(ctx.exception))

    def test_the_root_itself_is_not_a_logical_path(self) -> None:
        with self.assertRaises(PathPortabilityError):
            to_logical_path(self.tmp, self.tmp)

    def test_resolve_logical_stays_inside_the_root(self) -> None:
        (self.tmp / "a").mkdir()
        (self.tmp / "a" / "b.txt").write_text("x", encoding="utf-8")
        resolved = resolve_logical(self.tmp, "a/b.txt")
        self.assertEqual(resolved, (self.tmp / "a" / "b.txt").resolve())
        self.assertEqual(resolve_logical(self.tmp, "a\\b.txt"), resolved)
        self.assertEqual(resolve_logical(self.tmp, "a/new.txt"), (self.tmp / "a" / "new.txt").resolve())

    def test_resolve_logical_rejects_escapes_and_absolute_paths(self) -> None:
        for bad in ("../x", "a/../../x", "/etc/passwd", "C:\\x", str(self.tmp)):
            with self.subTest(bad=bad), self.assertRaises(PathPortabilityError):
                resolve_logical(self.tmp, bad)

    def test_round_trip(self) -> None:
        (self.tmp / "d").mkdir()
        file = self.tmp / "d" / "f.txt"
        file.write_text("x", encoding="utf-8")
        self.assertEqual(resolve_logical(self.tmp, to_logical_path(file, self.tmp)), file.resolve())

    def test_symlink_escaping_the_root_is_refused(self) -> None:
        outside = self.tmp / "outside"
        root = self.tmp / "root"
        outside.mkdir()
        root.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        link_directory(root / "link", outside)
        with self.assertRaises(PathPortabilityError):
            resolve_logical(root, "link/secret.txt")
        with self.assertRaises(PathPortabilityError):
            to_logical_path(root / "link" / "secret.txt", root)


class FileSystemPaths(TempDirCase):
    def test_extended_prefix_rules_are_pure_and_testable_on_any_os(self) -> None:
        short = "C:\\pasta\\a.txt"
        self.assertEqual(_windows_extended(short), short)
        long_path = "C:\\" + "\\".join(f"seg{i}" for i in range(80)) + "\\a.txt"
        self.assertGreaterEqual(len(long_path), 240)
        self.assertEqual(_windows_extended(long_path), "\\\\?\\" + long_path)
        unc = "\\\\servidor\\compartilhamento\\" + "\\".join(f"seg{i}" for i in range(80)) + "\\a.txt"
        self.assertEqual(_windows_extended(unc), "\\\\?\\UNC\\" + unc[2:])
        already = "\\\\?\\" + long_path
        self.assertEqual(_windows_extended(already), already)

    def test_tree_roots_always_get_the_prefix_on_windows_because_descendants_can_be_long(self) -> None:
        self.assertEqual(_windows_extended("C:\\pasta", 0), "\\\\?\\C:\\pasta")
        self.assertEqual(_windows_extended("\\\\srv\\share\\x", 0), "\\\\?\\UNC\\srv\\share\\x")
        self.assertEqual(_windows_extended("\\\\?\\C:\\pasta", 0), "\\\\?\\C:\\pasta")
        if os.name == "nt":
            self.assertTrue(to_fs_path(self.tmp, tree=True).startswith("\\\\?\\"))
            self.assertFalse(to_fs_path(self.tmp).startswith("\\\\?\\"))
        else:
            self.assertEqual(to_fs_path(self.tmp, tree=True), to_fs_path(self.tmp))

    def test_extended_prefix_threshold(self) -> None:
        base = "C:\\" + "a" * 100 + "\\"
        below = base + "b" * (239 - len(base))
        at = base + "b" * (240 - len(base))
        self.assertEqual((len(below), len(at)), (239, 240))
        self.assertEqual(_windows_extended(below), below)
        self.assertEqual(_windows_extended(at), "\\\\?\\" + at)

    def test_to_fs_path_is_absolute_and_normalized(self) -> None:
        result = to_fs_path(Path("x") / "y" / ".." / "z.txt")
        self.assertTrue(os.path.isabs(result.replace("\\\\?\\", "", 1)))
        self.assertTrue(result.endswith(os.path.join("x", "z.txt")))

    def test_to_fs_path_leaves_short_local_paths_alone(self) -> None:
        target = self.tmp / "short.txt"
        self.assertEqual(to_fs_path(target), os.path.abspath(str(target)))

    @unittest.skipUnless(os.name == "nt", "caminho longo estendido só se aplica ao Windows")
    def test_real_io_on_a_path_beyond_max_path(self) -> None:
        from core.atomic import atomic_write_text
        from core.hashing import hash_bytes, hash_file

        base = self.tmp
        for index in range(6):
            base = base / (f"nivel{index}-" + "d" * 50)
        target = base / "arquivo-longo.txt"
        self.assertGreater(len(str(target)), 260)
        top = self.tmp / ("nivel0-" + "d" * 50)
        import shutil

        self.addCleanup(lambda: shutil.rmtree(to_fs_path(top, tree=True), ignore_errors=True))
        result = atomic_write_text(target, "conteúdo longo\n")
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(hash_file(target), hash_bytes("conteúdo longo\n".encode("utf-8")))
        with open(to_fs_path(target), "rb") as handle:
            self.assertEqual(handle.read(), "conteúdo longo\n".encode("utf-8"))


class ComparisonKey(unittest.TestCase):
    def test_case_is_ignored_only_where_the_file_system_ignores_it(self) -> None:
        if os.name == "nt":
            self.assertEqual(comparison_key("Docs/ReadMe.MD"), comparison_key("docs/readme.md"))
        else:
            self.assertNotEqual(comparison_key("Docs/ReadMe.MD"), comparison_key("docs/readme.md"))
        self.assertEqual(comparison_key("a/b"), comparison_key("a/b"))


class HostIndependentSemantics(unittest.TestCase):
    def test_windows_and_posix_flavours_of_the_same_logical_path_agree(self) -> None:
        win = PureWindowsPath("docs", "sub", "file.txt")
        posix = PurePosixPath("docs", "sub", "file.txt")
        self.assertEqual(normalize_logical_path(str(win)), normalize_logical_path(str(posix)))
        self.assertEqual(normalize_logical_path(ntpath.join("docs", "sub", "file.txt")), "docs/sub/file.txt")


if __name__ == "__main__":
    unittest.main()
