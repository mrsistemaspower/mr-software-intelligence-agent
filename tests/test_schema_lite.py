"""O validador de schema em si: cada palavra-chave suportada precisa se comportar como o padrão."""
from __future__ import annotations

import unittest

from tests.helpers import TempDirCase, write_json
from core.schema_lite import Registry, SchemaError, Validator


class ValidatorKeywords(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Validator(Registry())

    def ok(self, instance, schema) -> bool:
        return not self.v.validate(instance, schema)

    def test_type_and_bool_is_not_integer(self) -> None:
        self.assertTrue(self.ok(3, {"type": "integer"}))
        self.assertFalse(self.ok(True, {"type": "integer"}))
        self.assertFalse(self.ok(True, {"type": "number"}))
        self.assertTrue(self.ok(None, {"type": ["string", "null"]}))
        self.assertFalse(self.ok(1.5, {"type": "integer"}))

    def test_enum_and_const_distinguish_bool_from_int(self) -> None:
        self.assertFalse(self.ok(1, {"const": True}))
        self.assertTrue(self.ok(True, {"const": True}))
        self.assertFalse(self.ok(1, {"enum": [True, "x"]}))
        self.assertTrue(self.ok("x", {"enum": [True, "x"]}))

    def test_required_properties_and_additional(self) -> None:
        schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}, "additionalProperties": False}
        self.assertTrue(self.ok({"a": "x"}, schema))
        self.assertFalse(self.ok({}, schema))
        self.assertFalse(self.ok({"a": "x", "b": 1}, schema))
        self.assertFalse(self.ok({"a": 1}, schema))

    def test_additional_properties_as_schema(self) -> None:
        schema = {"type": "object", "properties": {"a": {}}, "additionalProperties": {"type": "integer"}}
        self.assertTrue(self.ok({"a": "x", "b": 2}, schema))
        self.assertFalse(self.ok({"b": "x"}, schema))

    def test_array_keywords(self) -> None:
        schema = {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"type": "integer"}}
        self.assertTrue(self.ok([1, 2], schema))
        self.assertFalse(self.ok([], schema))
        self.assertFalse(self.ok([1, 2, 3], schema))
        self.assertFalse(self.ok([1, 1], schema))
        self.assertFalse(self.ok(["a"], schema))

    def test_string_keywords(self) -> None:
        schema = {"type": "string", "minLength": 2, "maxLength": 3, "pattern": "^a"}
        self.assertTrue(self.ok("ab", schema))
        self.assertFalse(self.ok("a", schema))
        self.assertFalse(self.ok("abcd", schema))
        self.assertFalse(self.ok("bb", schema))

    def test_numbers(self) -> None:
        schema = {"type": "number", "minimum": 0, "maximum": 1}
        self.assertTrue(self.ok(0.5, schema))
        self.assertFalse(self.ok(-0.1, schema))
        self.assertFalse(self.ok(1.1, schema))

    def test_formats(self) -> None:
        self.assertTrue(self.ok("2026-10-14T10:00:00-03:00", {"format": "date-time"}))
        self.assertTrue(self.ok("2026-10-14T10:00:00Z", {"format": "date-time"}))
        self.assertFalse(self.ok("2026-10-14", {"format": "date-time"}))
        self.assertFalse(self.ok("2026-13-40T10:00:00Z", {"format": "date-time"}))
        self.assertTrue(self.ok("2026-10-14", {"format": "date"}))
        self.assertFalse(self.ok("2026-02-30", {"format": "date"}))

    def test_combinators(self) -> None:
        self.assertTrue(self.ok(1, {"anyOf": [{"type": "string"}, {"type": "integer"}]}))
        self.assertFalse(self.ok(1.5, {"anyOf": [{"type": "string"}, {"type": "integer"}]}))
        self.assertTrue(self.ok(1, {"oneOf": [{"type": "string"}, {"type": "integer"}]}))
        self.assertFalse(self.ok(1, {"oneOf": [{"type": "integer"}, {"minimum": 0}]}))
        self.assertTrue(self.ok(1, {"allOf": [{"type": "integer"}, {"minimum": 0}]}))
        self.assertFalse(self.ok(-1, {"allOf": [{"type": "integer"}, {"minimum": 0}]}))
        self.assertTrue(self.ok("x", {"not": {"type": "integer"}}))
        self.assertFalse(self.ok(1, {"not": {"type": "integer"}}))

    def test_if_then_else(self) -> None:
        schema = {
            "if": {"properties": {"k": {"const": "a"}}, "required": ["k"]},
            "then": {"required": ["x"]},
            "else": {"required": ["y"]},
        }
        self.assertTrue(self.ok({"k": "a", "x": 1}, schema))
        self.assertFalse(self.ok({"k": "a"}, schema))
        self.assertTrue(self.ok({"k": "b", "y": 1}, schema))
        self.assertFalse(self.ok({"k": "b", "x": 1}, schema))

    def test_if_without_required_matches_when_property_is_absent(self) -> None:
        """Armadilha conhecida do JSON Schema: sem `required`, o `if` casa quando a propriedade falta."""
        schema = {"if": {"properties": {"k": {"const": "a"}}}, "then": {"required": ["x"]}}
        self.assertFalse(self.ok({}, schema))

    def test_ref_across_files_and_local(self) -> None:
        self.assertTrue(self.ok("src:a", {"$ref": "urn:mr-software-intelligence:schema:common:1#/$defs/recordId"}))
        self.assertFalse(self.ok("zzz:a", {"$ref": "urn:mr-software-intelligence:schema:common:1#/$defs/recordId"}))
        local = {"$defs": {"n": {"type": "integer"}}, "type": "object", "properties": {"a": {"$ref": "#/$defs/n"}}}
        self.assertTrue(self.ok({"a": 1}, local))
        self.assertFalse(self.ok({"a": "x"}, local))

    def test_ref_with_sibling_keywords_are_both_applied(self) -> None:
        schema = {"$ref": "urn:mr-software-intelligence:schema:common:1#/$defs/recordId", "pattern": "^fct:"}
        self.assertTrue(self.ok("fct:a", schema))
        self.assertFalse(self.ok("src:a", schema))

    def test_unresolvable_reference_raises(self) -> None:
        with self.assertRaises(SchemaError):
            self.v.validate(1, {"$ref": "urn:mr-software-intelligence:schema:nope:1"})
        with self.assertRaises(SchemaError):
            self.v.validate(1, {"$ref": "#/$defs/missing"})

    def test_unknown_schema_name_raises(self) -> None:
        with self.assertRaises(SchemaError):
            self.v.validate({}, "does-not-exist")


class WellFormedness(TempDirCase):
    def registry_with(self, schema: dict) -> Registry:
        write_json(self.tmp / "probe.schema.json", schema)
        return Registry(self.tmp)

    def base(self, **extra) -> dict:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:mr-software-intelligence:schema:probe:1",
            "title": "Probe",
            "type": "object",
            **extra,
        }

    def test_clean_schema_passes(self) -> None:
        self.assertEqual(self.registry_with(self.base(properties={"a": {"type": "string"}})).check_all(), [])

    def test_unknown_keyword_is_reported(self) -> None:
        problems = self.registry_with(self.base(propertees={"a": {}})).check_all()
        self.assertTrue(any("não suportada" in p for p in problems), problems)

    def test_unknown_keyword_in_nested_schema_is_reported(self) -> None:
        problems = self.registry_with(self.base(properties={"a": {"typo": 1}})).check_all()
        self.assertTrue(any("não suportada" in p for p in problems), problems)

    def test_dangling_ref_is_reported(self) -> None:
        problems = self.registry_with(self.base(properties={"a": {"$ref": "#/$defs/none"}})).check_all()
        self.assertTrue(any("não resolve" in p for p in problems), problems)

    def test_bad_regex_is_reported(self) -> None:
        problems = self.registry_with(self.base(properties={"a": {"type": "string", "pattern": "("}})).check_all()
        self.assertTrue(any("regex inválida" in p for p in problems), problems)

    def test_id_must_match_file_name(self) -> None:
        schema = self.base()
        schema["$id"] = "urn:mr-software-intelligence:schema:other:1"
        problems = self.registry_with(schema).check_all()
        self.assertTrue(any("$id deve ser" in p for p in problems), problems)

    def test_unsupported_format_is_reported(self) -> None:
        problems = self.registry_with(self.base(properties={"a": {"type": "string", "format": "email"}})).check_all()
        self.assertTrue(any("formato não suportado" in p for p in problems), problems)

    def test_duplicate_ids_are_rejected(self) -> None:
        write_json(self.tmp / "a.schema.json", self.base())
        write_json(self.tmp / "b.schema.json", self.base())
        with self.assertRaises(SchemaError):
            Registry(self.tmp)


if __name__ == "__main__":
    unittest.main()
