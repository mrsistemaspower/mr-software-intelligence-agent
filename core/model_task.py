"""Tarefa de modelo: o contrato estruturado e neutro de provedor entre o agente e um modelo de linguagem.

O agente decide O QUE perguntar, COM QUE evidência e QUE forma a resposta deve ter. Um adaptador de modelo (ver
`core.model_adapter`) só recebe uma `ModelTask` e devolve uma resposta estruturada. Nada aqui conhece SDK, rede,
provedor ou prompt de provedor: as instruções são frases curtas e genéricas, em dados.

Uma `ModelTask` carrega:

- `task_type`, `objective` e `instructions`: o que se pede, em texto curto;
- `evidence_refs`: a evidência AUTORIZADA, cada item com hash, tipo de fonte, modalidade e a marca tri-estado
  `contains_sensitive_data` (`None` = não conferido, tratado como possivelmente sensível, como em `source`);
- `allowed_context`: o único contexto que o modelo pode usar. Se tiver a chave `evidence`, o conteúdo de cada
  evidência vem ali e é CONFERIDO contra o hash de `evidence_refs`: uma tarefa não consegue carregar conteúdo de
  evidência não autorizada, nem conteúdo diferente do hash;
- `output_contract`: nome, versão e schema da resposta exigida (a resposta é validada fora do modelo, nunca confiada);
- `required_capabilities`, `max_boundary` e `risk_level`: o que o adaptador precisa oferecer e até onde o dado pode ir;
- `attempt`: número da tentativa técnica (metadado: não entra na identidade).

Identidade: `task_id` é `artifact_identity` de tipo `model.task`, calculada do que importa (hash de cada evidência,
metadados, instruções, contrato, contexto sem o conteúdo bruto das evidências, que já está preso pelo hash). Não depende
de relógio, máquina, caminho, tentativa, provedor ou modelo. Mesma tarefa, mesmo id, sempre.

Limites: a tarefa não tem limite de tamanho próprio (o adaptador declara `long-context` quando precisa); o conteúdo de
evidência vai embutido no contexto e por isso a tarefa serializada pode ser grande e é transitória: o agente não a grava.
"""
from __future__ import annotations

import base64
import binascii
import functools
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .hashing import hash_bytes, hash_canonical_json
from .identity import artifact_identity
from .paths import find_local_paths, looks_like_local_path
from .schema_lite import SchemaError, Validator, Registry
from .serialization import CanonicalJsonError, canonical_bytes, loads_strict

TASK_KIND = "model-task"
TASK_SCHEMA = "model-task"
TASK_ARTIFACT_TYPE = "model.task"
IDENTITY_SCHEMA_VERSION = "1"
SCHEMA_URN_PREFIX = "urn:mr-software-intelligence:schema:"

# Espelha common#/$defs/sourceKind. Um teste confere a igualdade, para que os dois não divirjam.
SOURCE_KINDS = (
    "screenshot", "filesystem", "documentation", "wiki", "source-code", "database-schema", "runtime", "release-notes", "human-expert",
)
# `vision`, `structured-output` e `long-context` vêm do vocabulário de common#/$defs/capability; `tool-use` só existe para modelos.
CAPABILITIES = ("vision", "structured-output", "long-context", "tool-use")
# Ordem crescente de exposição do dado. LOCAL: não sai do ambiente; PRIVATE: rede controlada por quem usa; EXTERNAL: terceiro.
BOUNDARIES = ("LOCAL", "PRIVATE", "EXTERNAL")
MODALITIES = ("text", "image", "binary")
RISK_LEVELS = ("LOW", "NORMAL", "HIGH")
ENCODINGS = ("utf-8", "base64")
IMPLIED_CAPABILITY = {"image": "vision"}
EVIDENCE_KEY = "evidence"

NAME_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
DEFAULT_MEDIA_TYPE = {"text": "text/plain", "image": "application/octet-stream", "binary": "application/octet-stream"}

MAX_TEXT = 1000
MAX_INSTRUCTIONS = 20
MAX_LOCATOR = 256
MAX_VIOLATIONS = 20

_NAME = re.compile(NAME_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)
_REF = re.compile(REF_PATTERN)
_MEDIA_TYPE = re.compile(MEDIA_TYPE_PATTERN)
_SHA256 = re.compile(SHA256_PATTERN)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ModelContractError(ValueError):
    """Um objeto do contrato de modelo viola suas regras. `code` é estável e serve para decidir."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> "ModelContractError":
    return ModelContractError(code, message)


def is_int(value: object) -> bool:
    return type(value) is int


def is_number(value: object) -> bool:
    return type(value) in (int, float)


def check_text(value: object, what: str, limit: int = MAX_TEXT) -> str:
    """Texto curto e legível: não vazio, sem controle e dentro do limite."""
    if not isinstance(value, str) or not value.strip():
        raise _fail("INVALID_TEXT", f"{what} deve ser texto não vazio")
    if len(value) > limit:
        raise _fail("INVALID_TEXT", f"{what} excede {limit} caracteres")
    if _CONTROL.search(value):
        raise _fail("INVALID_TEXT", f"{what} contém caracteres de controle")
    return value


def check_name(value: object, what: str) -> str:
    if not (isinstance(value, str) and len(value) <= 64 and _NAME.match(value)):
        raise _fail("INVALID_NAME", f"{what} inválido (esperado {NAME_PATTERN}, até 64 caracteres)")
    return value


def check_ref(value: object, what: str) -> str:
    if not (isinstance(value, str) and _REF.match(value)) or looks_like_local_path(value):
        raise _fail("INVALID_REF", f"{what} inválido (esperado {REF_PATTERN}, sem caminho local)")
    return value


def check_choice(value: object, allowed: tuple, what: str) -> str:
    if not (isinstance(value, str) and value in allowed):
        raise _fail("INVALID_CHOICE", f"{what} deve ser um de {list(allowed)}: {value!r}")
    return value


def check_confidence(value: object, what: str) -> float:
    if not is_number(value) or not 0 <= value <= 1:
        raise _fail("INVALID_CONFIDENCE", f"{what} deve ser número de 0 a 1")
    return value  # type: ignore[return-value]


def boundary_rank(boundary: str) -> int:
    return BOUNDARIES.index(check_choice(boundary, BOUNDARIES, "boundary"))


def capabilities_tuple(values: Iterable[object], what: str = "required_capabilities") -> "tuple[str, ...]":
    if isinstance(values, (str, bytes)):
        raise _fail("INVALID_CAPABILITY", f"{what} deve ser uma lista")
    found = {check_choice(item, CAPABILITIES, "capacidade") for item in values}
    return tuple(sorted(found))


def _dict_keys(data: object, required: "set[str]", optional: "set[str]", what: str) -> dict:
    if not isinstance(data, dict):
        raise _fail("MALFORMED", f"{what} deve ser objeto")
    missing, extra = required - set(data), set(data) - required - optional
    if missing or extra:
        raise _fail("MALFORMED", f"{what} malformado (faltam: {sorted(missing)}; sobram: {sorted(extra)})")
    return data


@dataclass(frozen=True)
class EvidenceRef:
    """Uma evidência AUTORIZADA, identificada e presa por hash. O conteúdo nunca mora aqui.

    `ref` é o identificador portátil que o modelo deve citar. `sha256` é o hash do conteúdo no momento em que a
    evidência foi autorizada: quem executa confere o conteúdo atual contra ele. `locator` diz a quem executa ONDE ler
    (um nome lógico, nunca um caminho local); a tarefa enviada ao modelo não o carrega.
    """

    ref: str
    sha256: str
    source_type: str
    modality: str
    byte_length: int
    contains_sensitive_data: "bool | None"
    media_type: "str | None" = None
    locator: "str | None" = None

    def __post_init__(self) -> None:
        check_ref(self.ref, "ref de evidência")
        if not (isinstance(self.sha256, str) and _SHA256.match(self.sha256)):
            raise _fail("INVALID_HASH", "sha256 da evidência deve ter 64 hexadecimais minúsculos")
        check_choice(self.source_type, SOURCE_KINDS, "source_type")
        check_choice(self.modality, MODALITIES, "modality")
        if not is_int(self.byte_length) or self.byte_length < 0:
            raise _fail("INVALID_LENGTH", "byte_length deve ser inteiro não negativo")
        if not (self.contains_sensitive_data is None or type(self.contains_sensitive_data) is bool):
            raise _fail("INVALID_SENSITIVITY", "contains_sensitive_data deve ser true, false ou null")
        if self.media_type is None:
            object.__setattr__(self, "media_type", DEFAULT_MEDIA_TYPE[self.modality])
        elif not (isinstance(self.media_type, str) and _MEDIA_TYPE.match(self.media_type)):
            raise _fail("INVALID_MEDIA_TYPE", f"media_type inválido (esperado {MEDIA_TYPE_PATTERN})")
        if self.locator is not None:
            check_text(self.locator, "locator", MAX_LOCATOR)
            if find_local_paths(self.locator):
                raise _fail("LOCAL_PATH", "locator não pode ser caminho local; use um nome lógico")

    @property
    def sensitivity_verified_clear(self) -> bool:
        """Só `False` explícito prova que a evidência não é sensível; `None` (não conferido) não prova."""
        return self.contains_sensitive_data is False

    def implied_capabilities(self) -> "tuple[str, ...]":
        implied = IMPLIED_CAPABILITY.get(self.modality)
        return (implied,) if implied else ()

    def without_locator(self) -> "EvidenceRef":
        return EvidenceRef(
            self.ref, self.sha256, self.source_type, self.modality, self.byte_length, self.contains_sensitive_data, self.media_type
        )

    def to_dict(self) -> dict:
        data = {
            "ref": self.ref,
            "sha256": self.sha256,
            "source_type": self.source_type,
            "modality": self.modality,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "contains_sensitive_data": self.contains_sensitive_data,
        }
        if self.locator is not None:
            data["locator"] = self.locator
        return data

    @classmethod
    def from_dict(cls, data: object) -> "EvidenceRef":
        required = {"ref", "sha256", "source_type", "modality", "byte_length", "contains_sensitive_data"}
        _dict_keys(data, required, {"media_type", "locator"}, "evidência")
        return cls(**{key: data[key] for key in data})  # type: ignore[index,arg-type]

    @classmethod
    def of(
        cls, ref: str, content: bytes, source_type: str, modality: str = "text", *,
        contains_sensitive_data: "bool | None" = None, media_type: "str | None" = None, locator: "str | None" = None,
    ) -> "EvidenceRef":
        """Autoriza `content` como evidência, prendendo-o por hash e tamanho."""
        digest = hash_bytes(content)
        return cls(ref, digest.digest, source_type, modality, digest.byte_length, contains_sensitive_data, media_type, locator)


@dataclass(frozen=True)
class OutputContract:
    """Forma exigida da resposta: um nome, uma versão e o schema que a valida (referência a um schema do repositório)."""

    name: str
    version: str
    schema_ref: str

    def __post_init__(self) -> None:
        check_name(self.name, "nome do contrato de saída")
        if not (isinstance(self.version, str) and _VERSION.match(self.version)):
            raise _fail("INVALID_VERSION", f"versão do contrato inválida (esperado {VERSION_PATTERN})")
        if not (isinstance(self.schema_ref, str) and self.schema_ref.startswith(SCHEMA_URN_PREFIX) and len(self.schema_ref) <= 256):
            raise _fail("INVALID_SCHEMA_REF", f"schema_ref deve começar com {SCHEMA_URN_PREFIX}")

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "schema_ref": self.schema_ref}

    @classmethod
    def from_dict(cls, data: object) -> "OutputContract":
        _dict_keys(data, {"name", "version", "schema_ref"}, set(), "contrato de saída")
        return cls(data["name"], data["version"], data["schema_ref"])  # type: ignore[index]


@functools.lru_cache(maxsize=1)
def _validator() -> Validator:
    return Validator(Registry())


def schema_violations(instance: object, schema_ref: str) -> "list[str]":
    """Violações de `instance` contra o schema de `schema_ref` (urn com fragmento opcional). Lista vazia = válido."""
    try:
        found = _validator().validate(instance, {"$ref": schema_ref})
    except SchemaError as exc:
        return [f"contrato de saída desconhecido: {exc}"]
    return found[:MAX_VIOLATIONS]


def check_output(contract: OutputContract, output: object) -> "list[str]":
    """Valida a resposta do modelo contra o contrato da tarefa. A resposta nunca é confiada sem passar por aqui."""
    return schema_violations(output, contract.schema_ref)


def implied_capabilities(evidence: Iterable[EvidenceRef]) -> "tuple[str, ...]":
    return tuple(sorted({cap for item in evidence for cap in item.implied_capabilities()}))


def effective_capabilities(requested: Iterable[str], evidence: Iterable[EvidenceRef]) -> "tuple[str, ...]":
    """O que o adaptador precisa oferecer: o pedido mais o que a modalidade da evidência impõe (imagem exige `vision`)."""
    evidence = tuple(evidence)
    return tuple(sorted(set(capabilities_tuple(requested)) | set(implied_capabilities(evidence))))


@dataclass(frozen=True)
class TaskRequirements:
    """O que se exige de um adaptador ANTES de executar: capacidades, até onde o dado pode ir e a sensibilidade da evidência."""

    required_capabilities: "tuple[str, ...]"
    max_boundary: str
    evidence: "tuple[EvidenceRef, ...]" = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_capabilities", capabilities_tuple(self.required_capabilities))
        check_choice(self.max_boundary, BOUNDARIES, "max_boundary")
        object.__setattr__(self, "evidence", tuple(self.evidence))


def _decode_payload(ref: EvidenceRef, payload: object) -> bytes:
    _dict_keys(payload, {"media_type", "encoding", "content"}, set(), f"conteúdo da evidência {ref.ref}")
    encoding = check_choice(payload["encoding"], ENCODINGS, "encoding")  # type: ignore[index]
    expected = "utf-8" if ref.modality == "text" else "base64"
    if encoding != expected:
        raise _fail("EVIDENCE_ENCODING", f"a evidência {ref.ref} ({ref.modality}) deve usar {expected}")
    content = payload["content"]  # type: ignore[index]
    if not isinstance(content, str):
        raise _fail("EVIDENCE_CONTENT", f"o conteúdo da evidência {ref.ref} deve ser texto")
    try:
        raw = content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise _fail("EVIDENCE_CONTENT", f"o conteúdo da evidência {ref.ref} não decodifica ({type(exc).__name__})") from exc
    if payload["media_type"] != ref.media_type:  # type: ignore[index]
        raise _fail("EVIDENCE_CONTENT", f"o media_type do conteúdo de {ref.ref} difere do autorizado")
    return raw


def evidence_payload(ref: EvidenceRef, data: bytes) -> dict:
    """Conteúdo de uma evidência na forma que a tarefa carrega: texto UTF-8 ou base64, com o media_type autorizado."""
    if ref.modality == "text":
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail("EVIDENCE_UNDECODABLE", f"a evidência {ref.ref} não é UTF-8 válido") from exc
        return {"media_type": ref.media_type, "encoding": "utf-8", "content": content}
    return {"media_type": ref.media_type, "encoding": "base64", "content": base64.b64encode(data).decode("ascii")}


@dataclass(frozen=True)
class ModelTask:
    """Pedido estruturado a um modelo. Imutável; `task_id` deriva do conteúdo e é conferido ao construir."""

    task_type: str
    objective: str
    instructions: "tuple[str, ...]"
    evidence_refs: "tuple[EvidenceRef, ...]"
    allowed_context: dict
    output_contract: OutputContract
    required_capabilities: "tuple[str, ...]"
    max_boundary: str
    risk_level: str = "NORMAL"
    attempt: int = 1
    task_id: "str | None" = None

    def __post_init__(self) -> None:
        check_name(self.task_type, "task_type")
        check_text(self.objective, "objective")
        instructions = tuple(self.instructions)
        if len(instructions) > MAX_INSTRUCTIONS:
            raise _fail("INVALID_INSTRUCTIONS", f"no máximo {MAX_INSTRUCTIONS} instruções")
        object.__setattr__(self, "instructions", tuple(check_text(item, "instrução") for item in instructions))

        evidence = tuple(item.without_locator() if isinstance(item, EvidenceRef) else item for item in self.evidence_refs)
        if not all(isinstance(item, EvidenceRef) for item in evidence):
            raise _fail("INVALID_EVIDENCE", "evidence_refs deve conter EvidenceRef")
        evidence = tuple(sorted(evidence, key=lambda item: item.ref))
        if len({item.ref for item in evidence}) != len(evidence):
            raise _fail("DUPLICATE_EVIDENCE", "evidence_refs repete uma referência")
        object.__setattr__(self, "evidence_refs", evidence)

        if not isinstance(self.output_contract, OutputContract):
            raise _fail("INVALID_CONTRACT", "output_contract deve ser OutputContract")
        capabilities = capabilities_tuple(self.required_capabilities)
        missing = set(implied_capabilities(evidence)) - set(capabilities)
        if missing:
            raise _fail("MISSING_IMPLIED_CAPABILITY", f"a modalidade da evidência exige: {sorted(missing)}")
        object.__setattr__(self, "required_capabilities", capabilities)
        check_choice(self.max_boundary, BOUNDARIES, "max_boundary")
        check_choice(self.risk_level, RISK_LEVELS, "risk_level")
        if not is_int(self.attempt) or self.attempt < 1:
            raise _fail("INVALID_ATTEMPT", "attempt deve ser inteiro a partir de 1")

        object.__setattr__(self, "allowed_context", self._checked_context(evidence))
        identity = self._identity().artifact_id
        if self.task_id is not None and self.task_id != identity:
            raise _fail("ID_MISMATCH", "task_id não confere com o conteúdo da tarefa")
        object.__setattr__(self, "task_id", identity)

    def _checked_context(self, evidence: "tuple[EvidenceRef, ...]") -> dict:
        if not isinstance(self.allowed_context, dict):
            raise _fail("INVALID_CONTEXT", "allowed_context deve ser objeto")
        try:
            context = loads_strict(canonical_bytes(self.allowed_context))
        except CanonicalJsonError as exc:
            raise _fail("INVALID_CONTEXT", f"allowed_context não cabe no JSON canônico: {exc}") from exc
        assert isinstance(context, dict)
        # O conteúdo bruto de evidência é dado do mundo (pode citar caminhos); o resto do contexto é do agente e é portátil.
        if find_local_paths({key: value for key, value in context.items() if key != EVIDENCE_KEY}):
            raise _fail("LOCAL_PATH", "allowed_context não pode carregar caminho local")
        payloads = context.get(EVIDENCE_KEY)
        if payloads is None:
            return context
        if not isinstance(payloads, dict):
            raise _fail("INVALID_CONTEXT", f"allowed_context.{EVIDENCE_KEY} deve ser objeto")
        authorized = {item.ref: item for item in evidence}
        unknown = sorted(set(payloads) - set(authorized))
        if unknown:
            raise _fail("UNAUTHORIZED_EVIDENCE", f"conteúdo de evidência não autorizada no contexto: {unknown[:5]}")
        for ref, payload in payloads.items():
            raw = _decode_payload(authorized[ref], payload)
            digest = hash_bytes(raw)
            if digest.digest != authorized[ref].sha256 or digest.byte_length != authorized[ref].byte_length:
                raise _fail("EVIDENCE_HASH", f"o conteúdo da evidência {ref} não confere com o hash autorizado")
        return context

    def requirements(self) -> TaskRequirements:
        return TaskRequirements(self.required_capabilities, self.max_boundary, self.evidence_refs)

    def _identity(self):  # noqa: ANN202 - ArtifactIdentity
        context = {key: value for key, value in self.allowed_context.items() if key != EVIDENCE_KEY}
        meta = [
            {key: value for key, value in item.to_dict().items() if key not in ("sha256", "locator")} for item in self.evidence_refs
        ]
        return artifact_identity(
            TASK_ARTIFACT_TYPE,
            IDENTITY_SCHEMA_VERSION,
            inputs={item.ref: item.sha256 for item in self.evidence_refs},
            parameters={
                "task_type": self.task_type,
                "objective": self.objective,
                "instructions": list(self.instructions),
                "evidence": meta,
                "context_digest": str(hash_canonical_json(context)),
                "output_contract": self.output_contract.to_dict(),
                "required_capabilities": list(self.required_capabilities),
                "max_boundary": self.max_boundary,
                "risk_level": self.risk_level,
            },
        )

    def to_dict(self) -> dict:
        return {
            "kind": TASK_KIND,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "objective": self.objective,
            "instructions": list(self.instructions),
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "allowed_context": loads_strict(canonical_bytes(self.allowed_context)),
            "output_contract": self.output_contract.to_dict(),
            "required_capabilities": list(self.required_capabilities),
            "max_boundary": self.max_boundary,
            "risk_level": self.risk_level,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ModelTask":
        problems = schema_violations(data, f"{SCHEMA_URN_PREFIX}{TASK_SCHEMA}:1")
        if problems:
            raise _fail("SCHEMA", "tarefa fora do contrato: " + "; ".join(problems[:5]))
        assert isinstance(data, dict)
        return cls(
            task_type=data["task_type"],
            objective=data["objective"],
            instructions=tuple(data["instructions"]),
            evidence_refs=tuple(EvidenceRef.from_dict(item) for item in data["evidence_refs"]),
            allowed_context=data["allowed_context"],
            output_contract=OutputContract.from_dict(data["output_contract"]),
            required_capabilities=tuple(data["required_capabilities"]),
            max_boundary=data["max_boundary"],
            risk_level=data["risk_level"],
            attempt=data["attempt"],
            task_id=data["task_id"],
        )


def context_evidence(task: ModelTask) -> "Mapping[str, dict]":
    """Conteúdo de evidência que a tarefa carrega, por ref (só leitura). Vazio se a tarefa não embute conteúdo."""
    payloads = task.allowed_context.get(EVIDENCE_KEY, {})
    return dict(payloads) if isinstance(payloads, dict) else {}
