"""Hash determinístico: bytes, arquivos e JSON canônico."""
from __future__ import annotations

import hashlib
import os
import unittest

from tests.helpers import TempDirCase
from core.hashing import (
    DEFAULT_ALGORITHM,
    SUPPORTED_ALGORITHMS,
    Digest,
    digest_reference,
    hash_bytes,
    hash_canonical_json,
    hash_file,
)
from core.serialization import canonical_bytes, canonical_pretty_bytes

# Respostas conhecidas de SHA-256 (as duas primeiras são as do padrão FIPS 180).
EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
RANGE256 = "40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880"


def blob(size: int) -> bytes:
    return bytes((i * 31 + 7) % 256 for i in range(size))


class KnownAnswers(unittest.TestCase):
    def test_default_algorithm_is_sha256_and_the_list_is_closed(self) -> None:
        self.assertEqual(DEFAULT_ALGORITHM, "sha256")
        self.assertEqual(SUPPORTED_ALGORITHMS, ("sha256",))

    def test_bytes(self) -> None:
        self.assertEqual(hash_bytes(b"").digest, EMPTY)
        self.assertEqual(hash_bytes(b"abc").digest, ABC)
        self.assertEqual(hash_bytes(bytes(range(256))).digest, RANGE256)

    def test_structured_result(self) -> None:
        digest = hash_bytes(b"abc")
        self.assertEqual((digest.algorithm, digest.digest, digest.byte_length), ("sha256", ABC, 3))
        self.assertEqual(str(digest), f"sha256:{ABC}")
        self.assertEqual(digest.to_dict(), {"algorithm": "sha256", "digest": ABC, "byte_length": 3})
        self.assertEqual(Digest.from_dict(digest.to_dict()), digest)

    def test_accepts_every_bytes_like_and_rejects_text(self) -> None:
        for data in (b"abc", bytearray(b"abc"), memoryview(b"abc")):
            self.assertEqual(hash_bytes(data).digest, ABC)
        for bad in ("abc", 5, None, ["a"]):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                hash_bytes(bad)  # type: ignore[arg-type]

    def test_weak_or_unknown_algorithms_are_refused(self) -> None:
        for name in ("md5", "sha1", "sha512", "SHA256", ""):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    hash_bytes(b"x", name)
                with self.assertRaises(ValueError):
                    hash_canonical_json({}, name)
                with self.assertRaises(ValueError):
                    Digest(name, "0" * 64, 0)


class Mutation(unittest.TestCase):
    def test_hash_is_stable(self) -> None:
        data = blob(1000)
        self.assertEqual({hash_bytes(data).digest for _ in range(5)}, {hashlib.sha256(data).hexdigest()})

    def test_every_single_bit_flip_changes_the_hash(self) -> None:
        data = blob(64)
        original = hash_bytes(data).digest
        seen = {original}
        for index in range(len(data)):
            for bit in range(8):
                mutated = bytearray(data)
                mutated[index] ^= 1 << bit
                digest = hash_bytes(bytes(mutated)).digest
                self.assertNotEqual(digest, original, (index, bit))
                seen.add(digest)
        self.assertEqual(len(seen), 1 + 64 * 8, "nenhuma colisão entre as 512 mutações")

    def test_append_truncate_and_swap_change_the_hash(self) -> None:
        data = blob(100)
        variants = [data + b"\x00", data[:-1], data[1:], data[::-1], data[50:] + data[:50]]
        digests = {hash_bytes(v).digest for v in variants} | {hash_bytes(data).digest}
        self.assertEqual(len(digests), len(variants) + 1)


class Files(TempDirCase):
    def write(self, name: str, data: bytes) -> str:
        path = self.tmp / name
        path.write_bytes(data)
        return str(path)

    def test_file_hash_equals_bytes_hash_across_chunk_boundaries(self) -> None:
        for size in list(range(0, 40)) + [255, 256, 257]:
            data = blob(size)
            path = self.write(f"f{size}.bin", data)
            for chunk in (1, 7, 16, 1 << 20):
                with self.subTest(size=size, chunk=chunk):
                    self.assertEqual(hash_file(path, chunk_size=chunk), hash_bytes(data))

    def test_default_chunk_boundary_sizes(self) -> None:
        for size in ((1 << 20) - 1, 1 << 20, (1 << 20) + 1, 3 * (1 << 20) + 7):
            data = blob(size)
            with self.subTest(size=size):
                self.assertEqual(hash_file(self.write(f"b{size}", data)), hash_bytes(data))

    def test_length_is_the_number_of_bytes_read(self) -> None:
        digest = hash_file(self.write("a.bin", blob(12345)))
        self.assertEqual(digest.byte_length, 12345)

    def test_bytes_are_hashed_as_they_are_without_newline_translation(self) -> None:
        crlf = self.write("crlf.txt", b"a\r\nb\r\n")
        lf = self.write("lf.txt", b"a\nb\n")
        self.assertEqual(hash_file(crlf), hash_bytes(b"a\r\nb\r\n"))
        self.assertNotEqual(hash_file(crlf), hash_file(lf))

    def test_path_object_and_text_path_agree(self) -> None:
        path = self.write("p.bin", b"abc")
        self.assertEqual(hash_file(path), hash_file(self.tmp / "p.bin"))
        self.assertEqual(hash_file(path).digest, ABC)

    def test_one_byte_change_on_disk_changes_the_file_hash(self) -> None:
        data = bytearray(blob(500))
        path = self.write("m.bin", bytes(data))
        before = hash_file(path)
        data[250] ^= 0x01
        (self.tmp / "m.bin").write_bytes(bytes(data))
        self.assertNotEqual(hash_file(path), before)

    def test_missing_file_and_directory_raise_the_system_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            hash_file(self.tmp / "nope.bin")
        with self.assertRaises(OSError):
            hash_file(self.tmp)

    def test_bad_chunk_size(self) -> None:
        path = self.write("x", b"x")
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                hash_file(path, chunk_size=bad)

    def test_hash_file_of_a_compact_json_file_equals_the_canonical_hash(self) -> None:
        value = {"b": [1, 2], "a": "ç"}
        path = self.write("c.json", canonical_bytes(value))
        self.assertEqual(hash_file(path), hash_canonical_json(value))

    def test_file_hash_is_independent_of_location_and_name(self) -> None:
        data = blob(777)
        first = self.write("one.bin", data)
        (self.tmp / "deep" / "er").mkdir(parents=True)
        second = str(self.tmp / "deep" / "er" / "other-name.dat")
        os.replace(first, second)
        self.assertEqual(hash_file(second), hash_bytes(data))


class CanonicalJsonHash(unittest.TestCase):
    def test_stable_under_key_reorder_and_equal_to_hash_of_canonical_bytes(self) -> None:
        a = {"x": [1, {"b": 2, "a": 1}], "y": "ç"}
        b = {"y": "ç", "x": [1, {"a": 1, "b": 2}]}
        self.assertEqual(hash_canonical_json(a), hash_canonical_json(b))
        self.assertEqual(hash_canonical_json(a), hash_bytes(canonical_bytes(a)))

    def test_any_semantic_change_changes_the_hash(self) -> None:
        base = {"x": [1, 2], "y": {"z": "a"}}
        variants = [
            {"x": [2, 1], "y": {"z": "a"}},
            {"x": [1, 2], "y": {"z": "b"}},
            {"x": [1, 2], "y": {"z": "a"}, "w": None},
            {"x": [1, 2.0], "y": {"z": "a"}},
            {"x": [1, 2], "y": {"Z": "a"}},
        ]
        digests = {hash_canonical_json(v).digest for v in variants} | {hash_canonical_json(base).digest}
        self.assertEqual(len(digests), len(variants) + 1)

    def test_compact_and_pretty_forms_are_different_hash_domains(self) -> None:
        value = {"a": 1}
        self.assertNotEqual(hash_bytes(canonical_pretty_bytes(value)), hash_canonical_json(value))

    def test_invalid_values_are_not_hashed(self) -> None:
        with self.assertRaises(ValueError):
            hash_canonical_json({"x": float("nan")})


class DigestValidation(unittest.TestCase):
    def test_rejects_malformed_digests(self) -> None:
        for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Digest("sha256", bad, 0)  # type: ignore[arg-type]

    def test_rejects_malformed_lengths(self) -> None:
        for bad in (-1, True, 1.5, "3", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Digest("sha256", "a" * 64, bad)  # type: ignore[arg-type]

    def test_from_dict_is_strict(self) -> None:
        good = {"algorithm": "sha256", "digest": "a" * 64, "byte_length": 1}
        self.assertEqual(Digest.from_dict(good).byte_length, 1)
        for bad in ({**good, "extra": 1}, {k: v for k, v in good.items() if k != "digest"}, [], None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Digest.from_dict(bad)

    def test_digest_is_hashable_and_comparable(self) -> None:
        a, b = hash_bytes(b"x"), hash_bytes(b"x")
        self.assertEqual(a, b)
        self.assertEqual(len({a, b}), 1)

    def test_digest_reference_forms(self) -> None:
        digest = hash_bytes(b"abc")
        expected = f"sha256:{ABC}"
        self.assertEqual(digest_reference(digest), expected)
        self.assertEqual(digest_reference(expected), expected)
        self.assertEqual(digest_reference(ABC), expected)

    def test_digest_reference_rejects_everything_else(self) -> None:
        for bad in (ABC.upper(), ABC[:-1], f"md5:{'a' * 32}", f"sha256:{ABC[:-1]}", "sha256:", "garbage", "", None, 5, b"x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                digest_reference(bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
