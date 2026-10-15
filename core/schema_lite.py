"""Validador de JSON Schema mínimo, só com a biblioteca padrão.

Implementa o subconjunto de JSON Schema 2020-12 que os schemas deste repositório usam,
inclusive `$ref` entre arquivos (resolvido pelo `$id`), `if/then/else` e `not`.

Decisão de projeto: a lista de palavras-chave aceitas é fechada. Um schema com palavra-chave
desconhecida é rejeitado por `Registry.check_all`, para que um erro de digitação nunca
vire uma regra silenciosamente ignorada.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import re
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
ID_PREFIX = "urn:mr-software-intelligence:schema:"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

SUPPORTED_KEYWORDS = frozenset({
    "$schema", "$id", "$ref", "$defs", "$comment", "title", "description", "default", "examples",
    "type", "enum", "const",
    "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum",
    "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
})
SUPPORTED_FORMATS = frozenset({"date-time", "date"})
JSON_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})

_SCHEMA_VALUED = ("items", "not", "if", "then", "else")
_SCHEMA_MAPS = ("properties", "$defs")
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf")

_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SchemaError(Exception):
    """Erro no próprio schema ou na resolução de referências (não no documento validado)."""


@functools.lru_cache(maxsize=512)
def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_ok(value: object, expected: object) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        if name == "string" and isinstance(value, str):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "null" and value is None:
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
        if name == "number" and _is_number(value):
            return True
        if name == "integer" and (
            (isinstance(value, int) and not isinstance(value, bool))
            or (isinstance(value, float) and value.is_integer())
        ):
            return True
    return False


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _eq(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _format_ok(value: str, fmt: str) -> bool:
    if fmt == "date-time":
        if not _DATE_TIME.match(value):
            return False
        try:
            _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    if fmt == "date":
        if not _DATE.match(value):
            return False
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return True


class Registry:
    """Carrega todos os `*.schema.json` de um diretório e resolve referências por `$id`."""

    def __init__(self, schema_dir: Path | str | None = None) -> None:
        self.schema_dir = Path(schema_dir) if schema_dir else SCHEMA_DIR
        self.by_id: dict[str, dict] = {}
        self.path_by_id: dict[str, Path] = {}
        for path in sorted(self.schema_dir.glob("*.schema.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            sid = doc.get("$id")
            if not isinstance(sid, str):
                raise SchemaError(f"{path.name}: sem $id")
            if sid in self.by_id:
                raise SchemaError(f"$id duplicado: {sid}")
            self.by_id[sid] = doc
            self.path_by_id[sid] = path

    @staticmethod
    def schema_id(name: str) -> str:
        return f"{ID_PREFIX}{name}:1"

    def get(self, name_or_id: str) -> dict:
        sid = name_or_id if name_or_id in self.by_id else self.schema_id(name_or_id)
        try:
            return self.by_id[sid]
        except KeyError:
            raise SchemaError(f"schema desconhecido: {name_or_id}") from None

    def resolve(self, ref: str, current_root: dict) -> tuple[dict, object]:
        """Devolve (documento raiz, subschema) para uma referência."""
        uri, _, fragment = ref.partition("#")
        root = current_root
        if uri:
            if uri not in self.by_id:
                raise SchemaError(f"referência para schema inexistente: {ref}")
            root = self.by_id[uri]
        node: object = root
        if fragment:
            if not fragment.startswith("/"):
                raise SchemaError(f"fragmento não suportado: {ref}")
            for part in fragment[1:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(node, dict) and part in node:
                    node = node[part]
                elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                    node = node[int(part)]
                else:
                    raise SchemaError(f"ponteiro não resolve: {ref}")
        return root, node

    # ------------------------------------------------------------------ meta-checagem
    def check_all(self) -> list[str]:
        problems: list[str] = []
        for sid, doc in self.by_id.items():
            path = self.path_by_id[sid]
            name = path.name[: -len(".schema.json")]
            if doc.get("$schema") != DRAFT:
                problems.append(f"{path.name}: $schema deve ser {DRAFT}")
            if sid != self.schema_id(name):
                problems.append(f"{path.name}: $id deve ser {self.schema_id(name)}")
            if not doc.get("title"):
                problems.append(f"{path.name}: sem title")
            problems.extend(f"{path.name}: {p}" for p in self.check_schema(doc))
        return problems

    def check_schema(self, doc: dict) -> list[str]:
        problems: list[str] = []
        self._walk(doc, doc, "#", problems)
        return problems

    def _walk(self, node: object, root: dict, path: str, problems: list[str]) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            problems.append(f"{path}: subschema deve ser objeto ou booleano")
            return
        for key, value in node.items():
            here = f"{path}/{key}"
            if key not in SUPPORTED_KEYWORDS:
                problems.append(f"{here}: palavra-chave não suportada")
                continue
            if key == "$ref":
                try:
                    self.resolve(value, root)
                except SchemaError as exc:
                    problems.append(f"{here}: {exc}")
            elif key == "type":
                names = value if isinstance(value, list) else [value]
                if not names or any(n not in JSON_TYPES for n in names):
                    problems.append(f"{here}: tipo inválido {value!r}")
            elif key == "required":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    problems.append(f"{here}: deve ser lista de strings")
            elif key == "enum":
                if not isinstance(value, list) or not value:
                    problems.append(f"{here}: deve ser lista não vazia")
            elif key == "pattern":
                try:
                    re.compile(value)
                except (re.error, TypeError) as exc:
                    problems.append(f"{here}: regex inválida ({exc})")
            elif key == "format":
                if value not in SUPPORTED_FORMATS:
                    problems.append(f"{here}: formato não suportado {value!r}")
            elif key == "additionalProperties":
                if not isinstance(value, bool):
                    self._walk(value, root, here, problems)
            elif key in _SCHEMA_VALUED:
                self._walk(value, root, here, problems)
            elif key in _SCHEMA_MAPS:
                if not isinstance(value, dict):
                    problems.append(f"{here}: deve ser objeto")
                else:
                    for sub_name, sub in value.items():
                        self._walk(sub, root, f"{here}/{sub_name}", problems)
            elif key in _SCHEMA_LISTS:
                if not isinstance(value, list) or not value:
                    problems.append(f"{here}: deve ser lista não vazia")
                else:
                    for i, sub in enumerate(value):
                        self._walk(sub, root, f"{here}/{i}", problems)


class Validator:
    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or Registry()

    def validate(self, instance: object, schema: str | dict) -> list[str]:
        """Devolve a lista de violações (vazia = válido). `schema` é nome, `$id` ou documento."""
        root = self.registry.get(schema) if isinstance(schema, str) else schema
        errors: list[str] = []
        self._v(instance, root, root, "$", errors, 0)
        return errors

    def is_valid(self, instance: object, schema: str | dict) -> bool:
        return not self.validate(instance, schema)

    def _v(self, inst: object, schema: object, root: dict, path: str, errors: list[str], depth: int) -> None:
        if schema is True:
            return
        if schema is False:
            errors.append(f"{path}: nenhum valor é permitido aqui")
            return
        if not isinstance(schema, dict):
            raise SchemaError(f"{path}: subschema inválido")
        if depth > 80:
            raise SchemaError("profundidade excessiva (referência circular?)")
        d = depth + 1

        if "$ref" in schema:
            new_root, target = self.registry.resolve(schema["$ref"], root)
            self._v(inst, target, new_root, path, errors, d)

        if "type" in schema and not _type_ok(inst, schema["type"]):
            errors.append(f"{path}: tipo esperado {schema['type']}, recebido {_type_name(inst)}")
            return
        if "const" in schema and not _eq(inst, schema["const"]):
            errors.append(f"{path}: deve ser {schema['const']!r}")
        if "enum" in schema and not any(_eq(inst, v) for v in schema["enum"]):
            errors.append(f"{path}: {inst!r} fora de {schema['enum']}")

        if isinstance(inst, str):
            if "minLength" in schema and len(inst) < schema["minLength"]:
                errors.append(f"{path}: menor que {schema['minLength']} caracteres")
            if "maxLength" in schema and len(inst) > schema["maxLength"]:
                errors.append(f"{path}: maior que {schema['maxLength']} caracteres")
            if "pattern" in schema and not _regex(schema["pattern"]).search(inst):
                errors.append(f"{path}: {inst!r} não casa com {schema['pattern']}")
            if "format" in schema and not _format_ok(inst, schema["format"]):
                errors.append(f"{path}: {inst!r} não é {schema['format']} válido")

        if _is_number(inst):
            if "minimum" in schema and inst < schema["minimum"]:
                errors.append(f"{path}: {inst} menor que {schema['minimum']}")
            if "maximum" in schema and inst > schema["maximum"]:
                errors.append(f"{path}: {inst} maior que {schema['maximum']}")

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                errors.append(f"{path}: menos de {schema['minItems']} itens")
            if "maxItems" in schema and len(inst) > schema["maxItems"]:
                errors.append(f"{path}: mais de {schema['maxItems']} itens")
            if schema.get("uniqueItems"):
                seen = [json.dumps(v, sort_keys=True, ensure_ascii=False) for v in inst]
                if len(set(seen)) != len(seen):
                    errors.append(f"{path}: itens repetidos")
            if "items" in schema:
                for i, item in enumerate(inst):
                    self._v(item, schema["items"], root, f"{path}[{i}]", errors, d)

        if isinstance(inst, dict):
            for name in schema.get("required", []):
                if name not in inst:
                    errors.append(f"{path}: propriedade obrigatória ausente '{name}'")
            props = schema.get("properties", {})
            for name, sub in props.items():
                if name in inst:
                    self._v(inst[name], sub, root, f"{path}.{name}", errors, d)
            extra = schema.get("additionalProperties", True)
            if extra is not True:
                for name in inst:
                    if name in props:
                        continue
                    if extra is False:
                        errors.append(f"{path}: propriedade não permitida '{name}'")
                    else:
                        self._v(inst[name], extra, root, f"{path}.{name}", errors, d)

        for sub in schema.get("allOf", []):
            self._v(inst, sub, root, path, errors, d)
        if "anyOf" in schema:
            if not any(self._passes(inst, sub, root, path, d) for sub in schema["anyOf"]):
                errors.append(f"{path}: nenhuma alternativa de anyOf casou")
        if "oneOf" in schema:
            hits = sum(1 for sub in schema["oneOf"] if self._passes(inst, sub, root, path, d))
            if hits != 1:
                errors.append(f"{path}: oneOf exige exatamente 1 alternativa, casaram {hits}")
        if "not" in schema and self._passes(inst, schema["not"], root, path, d):
            errors.append(f"{path}: valor proibido pelo schema")
        if "if" in schema:
            branch = "then" if self._passes(inst, schema["if"], root, path, d) else "else"
            if branch in schema:
                self._v(inst, schema[branch], root, path, errors, d)

    def _passes(self, inst: object, schema: object, root: dict, path: str, depth: int) -> bool:
        sink: list[str] = []
        self._v(inst, schema, root, path, sink, depth)
        return not sink
