"""JSON canônico (`mr-canonical-json/1`): mesmo valor, mesmos bytes; o que é recusado; leitura estrita."""
from __future__ import annotations

import copy
import datetime
import decimal
import enum
import itertools
import json
import unittest
from pathlib import Path

from tests.helpers import REPO  # noqa: F401  (garante o sys.path)
from core.serialization import (
    CONTRACT,
    MAX_DEPTH,
    CanonicalJsonError,
    canonical_bytes,
    canonical_dumps,
    canonical_pretty_bytes,
    loads_strict,
)


def nested(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


class SameValueSameBytes(unittest.TestCase):
    def test_key_insertion_order_never_matters(self) -> None:
        pairs = [("delta", [1, {"y": 2, "x": 1}]), ("alpha", None), ("charlie", "ação"), ("bravo", {"k": True})]
        outputs = {canonical_bytes(dict(order)) for order in itertools.permutations(pairs)}
        self.assertEqual(len(outputs), 1)

    def test_nested_reorder_is_stable(self) -> None:
        a = {"outer": {"b": 1, "a": {"z": 0, "y": [{"q": 1, "p": 2}]}}}
        b = {"outer": {"a": {"y": [{"p": 2, "q": 1}], "z": 0}, "b": 1}}
        self.assertEqual(canonical_bytes(a), canonical_bytes(b))
        self.assertEqual(canonical_pretty_bytes(a), canonical_pretty_bytes(b))

    def test_list_order_is_significant(self) -> None:
        self.assertNotEqual(canonical_bytes([1, 2]), canonical_bytes([2, 1]))

    def test_tuple_is_an_array_like_a_list(self) -> None:
        self.assertEqual(canonical_bytes((1, "a", None)), canonical_bytes([1, "a", None]))

    def test_input_is_not_mutated(self) -> None:
        value = {"b": [3, {"d": 1, "c": 2}], "a": "x"}
        before = copy.deepcopy(value)
        canonical_bytes(value)
        canonical_pretty_bytes(value)
        self.assertEqual(value, before)
        self.assertEqual(list(value), ["b", "a"])

    def test_contract_name_is_not_rfc_8785(self) -> None:
        self.assertEqual(CONTRACT, "mr-canonical-json/1")
        self.assertNotIn("8785", CONTRACT)
        self.assertNotIn("jcs", CONTRACT.lower())


class CompactForm(unittest.TestCase):
    def test_no_whitespace_and_no_trailing_newline(self) -> None:
        raw = canonical_bytes({"b": [1, 2], "a": {"c": "d e"}})
        self.assertEqual(raw, b'{"a":{"c":"d e"},"b":[1,2]}')
        self.assertFalse(raw.endswith(b"\n"))

    def test_keys_are_ordered_by_code_point(self) -> None:
        keys = ["é", "a", "B", "😀", "Z", "_", "0"]
        text = canonical_dumps({k: 1 for k in keys})
        self.assertEqual(list(json.loads(text)), ["0", "B", "Z", "_", "a", "é", "😀"])

    def test_code_point_order_differs_from_utf16_order_on_purpose(self) -> None:
        # RFC 8785 ordena por unidade UTF-16 e poria U+10000 antes de U+FFFF. Este contrato não.
        text = canonical_dumps({"\U00010000": 1, "\uffff": 2})
        self.assertLess(text.index("\uffff"), text.index("\U00010000"))

    def test_non_ascii_is_literal_utf8_without_bom(self) -> None:
        raw = canonical_bytes({"k": "ação 漢字 😀"})
        self.assertIn("ação 漢字 😀".encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        raw.decode("utf-8")

    def test_only_mandatory_escapes_and_lowercase_hex(self) -> None:
        text = canonical_dumps("\"\\\n\r\t\b\f\u0001\u001f\u007f/")
        self.assertEqual(text, '"\\"\\\\\\n\\r\\t\\b\\f\\u0001\\u001f\u007f/"')

    def test_line_and_paragraph_separators_stay_literal(self) -> None:
        self.assertEqual(canonical_dumps("\u2028\u2029"), '"\u2028\u2029"')

    def test_no_unicode_normalization(self) -> None:
        nfc, nfd = "\u00e9", "e\u0301"
        self.assertNotEqual(canonical_bytes(nfc), canonical_bytes(nfd))
        self.assertEqual(json.loads(canonical_bytes(nfd)), nfd)


class Numbers(unittest.TestCase):
    def test_int_float_and_bool_are_distinct(self) -> None:
        self.assertEqual(canonical_dumps([1, 1.0, True]), "[1,1.0,true]")
        self.assertNotEqual(canonical_bytes(1), canonical_bytes(1.0))
        self.assertNotEqual(canonical_bytes(1), canonical_bytes(True))
        self.assertNotEqual(canonical_bytes(0), canonical_bytes(False))

    def test_floats_use_the_shortest_round_trip_form(self) -> None:
        self.assertEqual(canonical_dumps([0.1, 1.5, 1e22, 1e-7, 123456.789]), "[0.1,1.5,1e+22,1e-07,123456.789]")
        for value in (0.1, 1 / 3, 2 ** -1074, 1.7976931348623157e308):
            self.assertEqual(float(canonical_dumps(value)), value)

    def test_negative_zero_is_kept(self) -> None:
        self.assertEqual(canonical_dumps(-0.0), "-0.0")
        self.assertNotEqual(canonical_bytes(-0.0), canonical_bytes(0.0))

    def test_big_integers_are_exact(self) -> None:
        self.assertEqual(canonical_dumps(10 ** 30), "1" + "0" * 30)
        self.assertEqual(canonical_dumps(-(2 ** 70)), str(-(2 ** 70)))

    def test_non_finite_numbers_are_rejected(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad), self.assertRaises(CanonicalJsonError):
                canonical_bytes({"x": bad})

    def test_non_finite_numbers_are_rejected_with_their_location(self) -> None:
        with self.assertRaises(CanonicalJsonError) as ctx:
            canonical_bytes({"x": [1, float("nan")]})
        self.assertIn(".x[1]", str(ctx.exception))
        with self.assertRaises(CanonicalJsonError) as ctx:
            canonical_pretty_bytes(float("inf"))
        self.assertIn("$", str(ctx.exception))


class Rejections(unittest.TestCase):
    def test_non_string_keys_are_rejected(self) -> None:
        for bad in ({1: "a"}, {None: "a"}, {True: "a"}, {(1, 2): "a"}, {1.5: "a"}):
            with self.subTest(bad=bad), self.assertRaises(CanonicalJsonError):
                canonical_bytes(bad)

    def test_unsupported_types_are_rejected(self) -> None:
        samples = [
            {1, 2}, frozenset({1}), b"bytes", bytearray(b"x"), datetime.datetime(2000, 1, 1), datetime.date(2000, 1, 1),
            Path("a/b"), decimal.Decimal("1.5"), object(), complex(1, 2), range(3), lambda: 1,
        ]
        for sample in samples:
            with self.subTest(type=type(sample).__name__), self.assertRaises(CanonicalJsonError):
                canonical_bytes({"x": sample})

    def test_scalar_subclasses_are_rejected_because_only_exact_types_are_deterministic(self) -> None:
        class Color(enum.IntEnum):
            RED = 1

        class Label(str):
            pass

        class Ratio(float):
            pass

        for sample in (Color.RED, Label("x"), Ratio(0.5)):
            with self.subTest(type=type(sample).__name__), self.assertRaises(CanonicalJsonError):
                canonical_bytes([sample])

    def test_lone_surrogates_are_rejected_in_values_and_keys(self) -> None:
        with self.assertRaises(CanonicalJsonError):
            canonical_bytes({"x": "a\ud800b"})
        with self.assertRaises(CanonicalJsonError):
            canonical_bytes({"a\udc00": 1})
        self.assertEqual(canonical_dumps("\ud83d\ude00".encode("utf-16", "surrogatepass").decode("utf-16")), '"😀"')

    def test_depth_limit_and_cycles(self) -> None:
        canonical_bytes(nested(MAX_DEPTH))
        with self.assertRaises(CanonicalJsonError):
            canonical_bytes(nested(MAX_DEPTH + 1))
        loop: list = []
        loop.append(loop)
        with self.assertRaises(CanonicalJsonError):
            canonical_bytes(loop)
        mapping: dict = {}
        mapping["self"] = mapping
        with self.assertRaises(CanonicalJsonError):
            canonical_bytes(mapping)

    def test_shared_but_acyclic_references_are_fine(self) -> None:
        shared = {"a": 1}
        self.assertEqual(canonical_dumps([shared, shared]), '[{"a":1},{"a":1}]')

    def test_errors_say_where(self) -> None:
        with self.assertRaises(CanonicalJsonError) as ctx:
            canonical_bytes({"outer": [1, {"bad": {1, 2}}]})
        self.assertIn("outer", str(ctx.exception))
        self.assertIn("bad", str(ctx.exception))

    def test_error_is_a_value_error_for_callers_that_catch_the_broad_type(self) -> None:
        self.assertTrue(issubclass(CanonicalJsonError, ValueError))


class PrettyForm(unittest.TestCase):
    def test_layout(self) -> None:
        raw = canonical_pretty_bytes({"b": [1, 2], "a": {}, "c": []})
        self.assertEqual(raw.decode("utf-8"), '{\n  "a": {},\n  "b": [\n    1,\n    2\n  ],\n  "c": []\n}\n')

    def test_single_trailing_lf_and_never_cr(self) -> None:
        raw = canonical_pretty_bytes({"k": "linha1\r\nlinha2", "z": [{"a": 1}]})
        self.assertTrue(raw.endswith(b"}\n"))
        self.assertFalse(raw.endswith(b"\n\n"))
        self.assertNotIn(b"\r", raw)

    def test_same_value_as_compact_form(self) -> None:
        value = {"b": [1, 2.5, None], "a": {"é": "ç"}}
        self.assertEqual(json.loads(canonical_pretty_bytes(value)), json.loads(canonical_bytes(value)))

    def test_scalar_documents(self) -> None:
        self.assertEqual(canonical_pretty_bytes("x"), b'"x"\n')
        self.assertEqual(canonical_pretty_bytes(None), b"null\n")

    def test_rejections_are_the_same_as_compact(self) -> None:
        with self.assertRaises(CanonicalJsonError):
            canonical_pretty_bytes({"x": float("nan")})
        with self.assertRaises(CanonicalJsonError):
            canonical_pretty_bytes({1: 1})


class StrictLoading(unittest.TestCase):
    def test_round_trip(self) -> None:
        value = {"b": [1, 2.5, None, True], "a": {"é": "ç\n"}, "n": 10 ** 25}
        self.assertEqual(loads_strict(canonical_bytes(value)), value)
        self.assertEqual(loads_strict(canonical_pretty_bytes(value)), value)
        self.assertEqual(loads_strict(canonical_dumps(value)), value)

    def test_canonicalization_is_idempotent(self) -> None:
        raw = canonical_bytes({"b": 1, "a": [3, 2, {"z": 0, "y": 1}]})
        self.assertEqual(canonical_bytes(loads_strict(raw)), raw)

    def test_duplicate_keys_are_rejected_even_though_json_loads_accepts_them(self) -> None:
        text = '{"a": 1, "a": 2}'
        self.assertEqual(json.loads(text), {"a": 2})
        with self.assertRaises(CanonicalJsonError):
            loads_strict(text)
        with self.assertRaises(CanonicalJsonError):
            loads_strict('{"outer": [{"k": 1, "k": 1}]}')

    def test_non_finite_literals_are_rejected(self) -> None:
        for text in ("NaN", "Infinity", "-Infinity", '{"x": NaN}', "[1e999]", "-1e999"):
            with self.subTest(text=text), self.assertRaises(CanonicalJsonError):
                loads_strict(text)

    def test_bom_and_bad_utf8_are_rejected(self) -> None:
        with self.assertRaises(CanonicalJsonError):
            loads_strict(b"\xef\xbb\xbf{}")
        with self.assertRaises(CanonicalJsonError):
            loads_strict(b'{"a": "\xff"}')

    def test_malformed_json_is_reported_with_the_contract_error(self) -> None:
        for text in ("", "{", '{"a":}', "[1,]", "'x'"):
            with self.subTest(text=text), self.assertRaises(CanonicalJsonError):
                loads_strict(text)

    def test_accepts_text_and_bytes_only(self) -> None:
        self.assertEqual(loads_strict("[1]"), [1])
        self.assertEqual(loads_strict(b"[1]"), [1])
        self.assertEqual(loads_strict(bytearray(b"[1]")), [1])
        with self.assertRaises(TypeError):
            loads_strict(5)  # type: ignore[arg-type]

    def test_integers_and_floats_keep_their_types(self) -> None:
        value = loads_strict("[1, 1.0, 1e2]")
        self.assertEqual([type(v) for v in value], [int, float, float])


if __name__ == "__main__":
    unittest.main()
