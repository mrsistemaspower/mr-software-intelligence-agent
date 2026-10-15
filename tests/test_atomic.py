"""Escrita atômica: bytes, texto e JSON; idempotência; falhas que não podem corromper o destino."""
from __future__ import annotations

import errno
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempDirCase
from core.atomic import CREATED, NO_CHANGE, UPDATED, WriteResult, atomic_write_bytes, atomic_write_json, atomic_write_text
from core.hashing import hash_bytes, hash_canonical_json, hash_file
from core.serialization import CanonicalJsonError, canonical_bytes, canonical_pretty_bytes, loads_strict

OLD = b"conteudo anterior que precisa sobreviver a qualquer falha\n"


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


class AtomicText(TempDirCase):
    def test_creates_file_and_missing_directories(self) -> None:
        target = self.tmp / "a" / "b" / "c.txt"
        result = atomic_write_text(target, "olá, mundo\n")
        self.assertEqual(result.status, CREATED)
        self.assertEqual(target.read_bytes(), "olá, mundo\n".encode("utf-8"))
        self.assertEqual(names(target.parent), ["c.txt"])

    def test_status_sequence_created_no_change_updated(self) -> None:
        target = self.tmp / "t.txt"
        self.assertEqual(atomic_write_text(target, "um").status, CREATED)
        self.assertEqual(atomic_write_text(target, "um").status, NO_CHANGE)
        self.assertEqual(atomic_write_text(target, "dois").status, UPDATED)
        self.assertEqual(target.read_bytes(), b"dois")

    def test_result_carries_the_digest_of_what_is_on_disk(self) -> None:
        target = self.tmp / "t.txt"
        for text in ("um", "um", "dois"):
            result = atomic_write_text(target, text)
            self.assertIsInstance(result, WriteResult)
            self.assertEqual(result.digest, hash_file(target))

    def test_newlines_are_normalized_to_lf_by_default(self) -> None:
        target = self.tmp / "t.txt"
        atomic_write_text(target, "a\r\nb\rc\nd")
        self.assertEqual(target.read_bytes(), b"a\nb\nc\nd")

    def test_newlines_can_be_kept_exactly_and_the_os_never_translates_them(self) -> None:
        target = self.tmp / "t.txt"
        atomic_write_text(target, "a\r\nb\n", normalize_newlines=False)
        self.assertEqual(target.read_bytes(), b"a\r\nb\n")
        atomic_write_text(target, "a\nb\n")
        self.assertNotIn(b"\r", target.read_bytes())

    def test_utf8_without_bom(self) -> None:
        target = self.tmp / "t.txt"
        atomic_write_text(target, "ação 漢字 😀")
        raw = target.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.decode("utf-8"), "ação 漢字 😀")

    def test_invalid_text_is_refused_before_touching_the_disk(self) -> None:
        target = self.tmp / "novo" / "t.txt"
        with self.assertRaises(ValueError):
            atomic_write_text(target, "a\ud800b")
        with self.assertRaises(TypeError):
            atomic_write_text(target, b"bytes")  # type: ignore[arg-type]
        self.assertFalse((self.tmp / "novo").exists())

    def test_empty_text(self) -> None:
        target = self.tmp / "vazio.txt"
        self.assertEqual(atomic_write_text(target, "").status, CREATED)
        self.assertEqual(target.read_bytes(), b"")


class AtomicBytes(TempDirCase):
    def test_roundtrip_of_arbitrary_bytes(self) -> None:
        data = bytes(range(256)) * 4096
        target = self.tmp / "b.bin"
        result = atomic_write_bytes(target, data)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(result.digest, hash_bytes(data))

    def test_accepts_bytes_like_and_rejects_text(self) -> None:
        for index, data in enumerate((b"x", bytearray(b"x"), memoryview(b"x"))):
            target = self.tmp / f"{index}.bin"
            atomic_write_bytes(target, data)
            self.assertEqual(target.read_bytes(), b"x")
        with self.assertRaises(TypeError):
            atomic_write_bytes(self.tmp / "t", "texto")  # type: ignore[arg-type]

    def test_no_temporary_artifact_is_left_after_success(self) -> None:
        for index in range(5):
            atomic_write_bytes(self.tmp / "t.bin", bytes([index]) * 100)
        self.assertEqual(names(self.tmp), ["t.bin"])

    def test_unchanged_content_is_not_rewritten(self) -> None:
        target = self.tmp / "t.bin"
        atomic_write_bytes(target, b"abc")
        stamp = 1_000_000_000_000_000_000
        os.utime(target, ns=(stamp, stamp))
        self.assertEqual(atomic_write_bytes(target, b"abc").status, NO_CHANGE)
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertEqual(atomic_write_bytes(target, b"abc", skip_unchanged=False).status, UPDATED)
        self.assertNotEqual(target.stat().st_mtime_ns, stamp)

    def test_no_temporary_is_even_created_when_nothing_changes(self) -> None:
        target = self.tmp / "t.bin"
        atomic_write_bytes(target, b"abc")
        with mock.patch("core.atomic._create_temp", side_effect=AssertionError("não deveria criar temporário")):
            self.assertEqual(atomic_write_bytes(target, b"abc").status, NO_CHANGE)

    def test_same_size_different_content_is_rewritten(self) -> None:
        target = self.tmp / "t.bin"
        atomic_write_bytes(target, b"abcd")
        self.assertEqual(atomic_write_bytes(target, b"abce").status, UPDATED)
        self.assertEqual(target.read_bytes(), b"abce")

    def test_temporary_lives_beside_the_target_and_is_never_reused(self) -> None:
        target = self.tmp / "sub" / "t.bin"
        real_replace = os.replace
        calls: list[tuple[str, str]] = []

        def spy(src: str, dst: str) -> None:
            calls.append((src, dst))
            real_replace(src, dst)

        with mock.patch("core.atomic.os.replace", side_effect=spy):
            for index in range(4):
                atomic_write_bytes(target, bytes([index]))
        self.assertEqual(len(calls), 4)
        for src, dst in calls:
            self.assertEqual(os.path.dirname(src), os.path.dirname(dst))
            base = os.path.basename(src)
            self.assertTrue(base.startswith(".") and base.endswith(".tmp"), base)
        self.assertEqual(len({src for src, _ in calls}), 4)

    def test_very_long_target_names_still_get_a_valid_temporary(self) -> None:
        target = self.tmp / (("n" * 200) + ".bin")
        atomic_write_bytes(target, b"x")
        self.assertEqual(names(self.tmp), [target.name])


class FailureNeverCorruptsTheTarget(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.target = self.tmp / "alvo.bin"
        self.target.write_bytes(OLD)
        self.before = hash_file(self.target)

    def assert_untouched(self) -> None:
        self.assertEqual(self.target.read_bytes(), OLD)
        self.assertEqual(hash_file(self.target), self.before)
        self.assertEqual(names(self.tmp), ["alvo.bin"], "nenhum temporário pode sobrar")

    def test_failure_in_replace(self) -> None:
        with mock.patch("core.atomic.os.replace", side_effect=OSError(errno.EACCES, "negado")):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.target, b"novo conteudo")
        self.assert_untouched()

    def test_failure_in_fsync(self) -> None:
        with mock.patch("core.atomic.os.fsync", side_effect=OSError(errno.EIO, "erro de E/S")):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.target, b"novo conteudo")
        self.assert_untouched()

    def test_disk_full_after_a_partial_write(self) -> None:
        real_write = os.write

        def partial(fd: int, data: object) -> int:
            real_write(fd, bytes(data)[:5])
            raise OSError(errno.ENOSPC, "sem espaço")

        with mock.patch("core.atomic.os.write", side_effect=partial):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.target, b"novo conteudo bem maior que cinco bytes")
        self.assert_untouched()

    def test_interruption_by_the_user_is_cleaned_up_too(self) -> None:
        with mock.patch("core.atomic.os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                atomic_write_bytes(self.target, b"novo conteudo")
        self.assert_untouched()

    def test_write_that_makes_no_progress_is_an_error_not_a_hang(self) -> None:
        with mock.patch("core.atomic.os.write", return_value=0):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.target, b"novo conteudo")
        self.assert_untouched()

    def test_unserializable_json_never_reaches_the_disk(self) -> None:
        for bad in ({"x": float("nan")}, {"x": {1, 2}}, {1: "a"}, {"x": "a\ud800"}):
            with self.subTest(bad=bad), self.assertRaises(CanonicalJsonError):
                atomic_write_json(self.target, bad)
        self.assert_untouched()

    def test_failed_first_write_leaves_no_target_and_no_temporary(self) -> None:
        fresh = self.tmp / "novo" / "arquivo.json"
        with mock.patch("core.atomic.os.replace", side_effect=OSError(errno.EACCES, "negado")):
            with self.assertRaises(OSError):
                atomic_write_json(fresh, {"a": 1})
        self.assertFalse(fresh.exists())
        self.assertEqual(names(fresh.parent), [])

    def test_failure_does_not_mask_itself_when_the_temporary_cannot_be_removed(self) -> None:
        with mock.patch("core.atomic.os.replace", side_effect=OSError(errno.EACCES, "negado")), \
                mock.patch("core.atomic.os.unlink", side_effect=OSError(errno.EPERM, "não removível")):
            with self.assertRaises(OSError) as ctx:
                atomic_write_bytes(self.target, b"novo conteudo")
        self.assertEqual(ctx.exception.errno, errno.EACCES)
        for leftover in self.tmp.iterdir():
            if leftover.name != "alvo.bin":
                leftover.unlink()

    def test_destination_that_is_a_directory(self) -> None:
        directory = self.tmp / "pasta"
        directory.mkdir()
        with self.assertRaises(IsADirectoryError):
            atomic_write_bytes(directory, b"x")
        self.assertEqual(names(directory), [])
        self.assertEqual(sorted(names(self.tmp)), ["alvo.bin", "pasta"])

    def test_parent_that_is_a_file(self) -> None:
        with self.assertRaises(OSError):
            atomic_write_bytes(self.target / "filho.txt", b"x")
        self.assert_untouched()

    def test_a_later_good_write_still_succeeds_after_a_failure(self) -> None:
        with mock.patch("core.atomic.os.replace", side_effect=OSError(errno.EACCES, "negado")):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.target, b"novo")
        result = atomic_write_bytes(self.target, b"novo")
        self.assertEqual(result.status, UPDATED)
        self.assertEqual(self.target.read_bytes(), b"novo")
        self.assertEqual(names(self.tmp), ["alvo.bin"])


class AtomicJson(TempDirCase):
    VALUE = {"b": [3, 2, 1], "a": {"z": None, "y": "ação"}, "n": 1.5}

    def test_pretty_form_is_the_canonical_pretty_bytes(self) -> None:
        target = self.tmp / "v.json"
        result = atomic_write_json(target, self.VALUE)
        self.assertEqual(target.read_bytes(), canonical_pretty_bytes(self.VALUE))
        self.assertTrue(target.read_bytes().endswith(b"}\n"))
        self.assertEqual(result.status, CREATED)

    def test_compact_form_file_hash_equals_the_canonical_hash(self) -> None:
        target = self.tmp / "v.json"
        atomic_write_json(target, self.VALUE, pretty=False)
        self.assertEqual(target.read_bytes(), canonical_bytes(self.VALUE))
        self.assertEqual(hash_file(target), hash_canonical_json(self.VALUE))

    def test_key_order_of_the_input_never_changes_the_file(self) -> None:
        first, second = self.tmp / "1.json", self.tmp / "2.json"
        atomic_write_json(first, {"a": 1, "b": 2, "c": {"x": 1, "y": 2}})
        atomic_write_json(second, {"c": {"y": 2, "x": 1}, "b": 2, "a": 1})
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_written_json_reads_back_as_the_same_value(self) -> None:
        target = self.tmp / "v.json"
        atomic_write_json(target, self.VALUE)
        self.assertEqual(loads_strict(target.read_bytes()), self.VALUE)

    def test_rewriting_the_same_value_is_a_no_change_even_with_another_key_order(self) -> None:
        target = self.tmp / "v.json"
        atomic_write_json(target, {"a": 1, "b": 2})
        self.assertEqual(atomic_write_json(target, {"b": 2, "a": 1}).status, NO_CHANGE)
        self.assertEqual(atomic_write_json(target, {"b": 3, "a": 1}).status, UPDATED)

    def test_json_file_has_no_carriage_return(self) -> None:
        target = self.tmp / "v.json"
        atomic_write_json(target, {"texto": "a\r\nb", "lista": [1, 2]})
        self.assertNotIn(b"\r", target.read_bytes())


class ReadersNeverSeeAPartialFile(TempDirCase):
    def test_concurrent_reader_sees_only_whole_versions(self) -> None:
        target = self.tmp / "shared.bin"
        versions = (b"A" * 200_000, b"B" * 300_000)
        atomic_write_bytes(target, versions[0])
        stop = threading.Event()
        anomalies: list[int] = []
        reads = [0]

        def reader() -> None:
            while not stop.is_set():
                try:
                    data = target.read_bytes()
                except OSError:  # no Windows, a troca pode colidir com a leitura
                    continue
                reads[0] += 1
                if data not in versions:
                    anomalies.append(len(data))

        thread = threading.Thread(target=reader)
        thread.start()
        successes = 0
        try:
            for index in range(60):
                try:
                    atomic_write_bytes(target, versions[index % 2], skip_unchanged=False)
                    successes += 1
                except PermissionError:  # o leitor estava com o arquivo aberto; a troca falha inteira
                    continue
        finally:
            stop.set()
            thread.join()
        self.assertEqual(anomalies, [])
        self.assertGreater(successes, 0)
        self.assertGreater(reads[0], 0)
        self.assertEqual(names(self.tmp), ["shared.bin"])
        self.assertIn(target.read_bytes(), versions)


if __name__ == "__main__":
    unittest.main()
