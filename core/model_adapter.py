"""Fronteira do adaptador de modelo: o que o agente exige de QUALQUER modelo, sem conhecer nenhum.

O contrato mínimo é `execute(task) -> ModelResult`. O adaptador decide só uma coisa: enviar a `ModelTask` estruturada a
um modelo capaz e devolver uma resposta estruturada (ou uma falha). Quem decide o quê rechecar, com que evidência, que
forma a resposta deve ter, se ela é aceitável e para onde segue é o agente, nunca o adaptador.

Este módulo define:

- `AdapterDescriptor`: identidade e limites do adaptador (id, versão, modelo, impressão digital da configuração,
  capacidades, `data_boundary`, se é determinístico e, opcionalmente, o `profile_id` do perfil de ambiente em
  `adapters/llm/`). Tudo é dado; nenhum SDK, rede ou credencial.
- `ModelResult`: a resposta. Ou `output` (JSON canônico) ou `failure` (nunca os dois), mais a identidade do modelo que
  de fato respondeu e o uso, quando o adaptador o conhece. Contagem de tokens NUNCA é obrigatória.
- `check_eligibility`: o portão ANTES de executar. Um adaptador sem a capacidade exigida, ou cuja fronteira de dados vai além
  do permitido, ou que enviaria evidência sensível (ou não conferida) a um terceiro, é recusado sem ser chamado.

Fronteira de dados (`LOCAL` < `PRIVATE` < `EXTERNAL`): o ambiente diz até onde o dado pode ir; o adaptador declara onde o dado
de fato vai. Evidência com `contains_sensitive_data` verdadeiro ou nulo (não conferido) nunca vai a um adaptador `EXTERNAL`.
Isto não é um sistema de DLP: é a estrutura mínima para que um ambiente diga "esta fonte não pode ir a um provedor externo".

Um adaptador é código de terceiros do ponto de vista do núcleo: o que ele devolve é validado, nunca confiado.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .hashing import hash_canonical_json
from .model_task import (
    BOUNDARIES,
    SCHEMA_URN_PREFIX,
    VERSION_PATTERN,
    ModelContractError,
    ModelTask,
    TaskRequirements,
    boundary_rank,
    capabilities_tuple,
    check_choice,
    check_name,
    check_text,
    is_int,
    schema_violations,
)
from .paths import find_local_paths
from .serialization import CanonicalJsonError, canonical_bytes, loads_strict

RESULT_KIND = "model-result"
RESULT_SCHEMA = "model-result"
FAILURE_KINDS = ("TIMEOUT", "UNAVAILABLE", "REFUSED", "ERROR")
MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$"
PROFILE_ID_PATTERN = r"^[a-z][a-z0-9-]*$"
FINGERPRINT_PATTERN = r"^sha256:[0-9a-f]{64}$"
USAGE_KEYS = ("input_tokens", "output_tokens")

# Códigos estáveis de recusa de adaptador.
MISSING_CAPABILITY = "MISSING_CAPABILITY"
BOUNDARY_EXCEEDED = "BOUNDARY_EXCEEDED"
SENSITIVE_EVIDENCE_EXTERNAL = "SENSITIVE_EVIDENCE_EXTERNAL"

_VERSION = re.compile(VERSION_PATTERN)
_MODEL_ID = re.compile(MODEL_ID_PATTERN)
_PROFILE_ID = re.compile(PROFILE_ID_PATTERN)
_FINGERPRINT = re.compile(FINGERPRINT_PATTERN)
_MESSAGE_LIMIT = 400


def _optional(value: object, pattern: "re.Pattern[str]", what: str) -> "str | None":
    if value is None:
        return None
    if not (isinstance(value, str) and pattern.match(value)):
        raise ModelContractError("INVALID_IDENTITY", f"{what} inválido (esperado {pattern.pattern})")
    return value


def config_fingerprint(configuration: object) -> str:
    """Impressão digital de uma configuração de modelo (JSON canônico). A configuração em si nunca é guardada."""
    return str(hash_canonical_json(configuration))


@dataclass(frozen=True)
class ModelIdentity:
    """Quem respondeu, como o adaptador declara: identificador do adaptador, do modelo e da configuração (todos opcionais, exceto o primeiro)."""

    adapter_id: str
    model_id: "str | None" = None
    config_fingerprint: "str | None" = None

    def __post_init__(self) -> None:
        check_name(self.adapter_id, "adapter_id")
        _optional(self.model_id, _MODEL_ID, "model_id")
        _optional(self.config_fingerprint, _FINGERPRINT, "config_fingerprint")

    def to_dict(self) -> dict:
        data: dict = {"adapter_id": self.adapter_id}
        if self.model_id is not None:
            data["model_id"] = self.model_id
        if self.config_fingerprint is not None:
            data["config_fingerprint"] = self.config_fingerprint
        return data

    @classmethod
    def from_dict(cls, data: object) -> "ModelIdentity":
        if not isinstance(data, dict) or "adapter_id" not in data or set(data) - {"adapter_id", "model_id", "config_fingerprint"}:
            raise ModelContractError("MALFORMED", "identidade de modelo malformada")
        return cls(data["adapter_id"], data.get("model_id"), data.get("config_fingerprint"))


@dataclass(frozen=True)
class AdapterDescriptor:
    """Identidade e limites de um adaptador. É o que o agente confere para decidir se ele serve a uma tarefa."""

    adapter_id: str
    adapter_version: str
    capabilities: "tuple[str, ...]"
    data_boundary: str
    deterministic: bool = False
    model_id: "str | None" = None
    config_fingerprint: "str | None" = None
    profile_id: "str | None" = None

    def __post_init__(self) -> None:
        check_name(self.adapter_id, "adapter_id")
        if not (isinstance(self.adapter_version, str) and _VERSION.match(self.adapter_version)):
            raise ModelContractError("INVALID_IDENTITY", f"adapter_version inválida (esperado {VERSION_PATTERN})")
        object.__setattr__(self, "capabilities", capabilities_tuple(self.capabilities, "capabilities"))
        check_choice(self.data_boundary, BOUNDARIES, "data_boundary")
        if type(self.deterministic) is not bool:
            raise ModelContractError("INVALID_IDENTITY", "deterministic deve ser booleano")
        _optional(self.model_id, _MODEL_ID, "model_id")
        _optional(self.config_fingerprint, _FINGERPRINT, "config_fingerprint")
        _optional(self.profile_id, _PROFILE_ID, "profile_id")

    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(self.adapter_id, self.model_id, self.config_fingerprint)

    def identity_dict(self) -> dict:
        """Forma portátil para proveniência e para os parâmetros de estágio (invalida o checkpoint se o adaptador mudar)."""
        data: dict = {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "capabilities": list(self.capabilities),
            "data_boundary": self.data_boundary,
            "deterministic": self.deterministic,
        }
        for key in ("model_id", "config_fingerprint", "profile_id"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: object) -> "AdapterDescriptor":
        required = {"adapter_id", "adapter_version", "capabilities", "data_boundary", "deterministic"}
        optional = {"model_id", "config_fingerprint", "profile_id"}
        if not isinstance(data, dict) or required - set(data) or set(data) - required - optional:
            raise ModelContractError("MALFORMED", "descritor de adaptador malformado")
        return cls(
            data["adapter_id"], data["adapter_version"], tuple(data["capabilities"]), data["data_boundary"], data["deterministic"],
            data.get("model_id"), data.get("config_fingerprint"), data.get("profile_id"),
        )


@dataclass(frozen=True)
class ModelFailure:
    """Falha técnica do adaptador como dado. `retryable`: uma nova tentativa com a mesma entrada poderia dar certo."""

    kind: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        check_choice(self.kind, FAILURE_KINDS, "kind da falha")
        check_text(self.message, "mensagem da falha", _MESSAGE_LIMIT)
        if find_local_paths(self.message):
            raise ModelContractError("LOCAL_PATH", "a mensagem da falha não pode carregar caminho local")
        if type(self.retryable) is not bool:
            raise ModelContractError("INVALID_FAILURE", "retryable deve ser booleano")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "retryable": self.retryable}

    @classmethod
    def from_dict(cls, data: object) -> "ModelFailure":
        if not isinstance(data, dict) or set(data) != {"kind", "message", "retryable"}:
            raise ModelContractError("MALFORMED", "falha malformada")
        return cls(data["kind"], data["message"], data["retryable"])


def _check_usage(usage: object) -> "dict | None":
    if usage is None:
        return None
    if not isinstance(usage, dict) or set(usage) - set(USAGE_KEYS):
        raise ModelContractError("INVALID_USAGE", f"usage só aceita {list(USAGE_KEYS)}")
    for key, value in usage.items():
        if not is_int(value) or value < 0:
            raise ModelContractError("INVALID_USAGE", f"usage.{key} deve ser inteiro não negativo")
    return dict(sorted(usage.items()))


@dataclass(frozen=True)
class ModelResult:
    """Resposta de um adaptador a uma tarefa: `output` estruturado XOR `failure`. Contagem de uso é opcional."""

    task_id: str
    model: ModelIdentity
    output: "dict | None" = None
    failure: "ModelFailure | None" = None
    usage: "dict | None" = None

    def __post_init__(self) -> None:
        if not (isinstance(self.task_id, str) and self.task_id):
            raise ModelContractError("INVALID_RESULT", "task_id ausente")
        if not isinstance(self.model, ModelIdentity):
            raise ModelContractError("INVALID_RESULT", "model deve ser ModelIdentity")
        if (self.output is None) == (self.failure is None):
            raise ModelContractError("INVALID_RESULT", "exatamente um entre output e failure")
        if self.failure is not None and not isinstance(self.failure, ModelFailure):
            raise ModelContractError("INVALID_RESULT", "failure deve ser ModelFailure")
        if self.output is not None:
            if not isinstance(self.output, dict):
                raise ModelContractError("INVALID_RESULT", "output deve ser objeto JSON")
            try:
                object.__setattr__(self, "output", loads_strict(canonical_bytes(self.output)))
            except CanonicalJsonError as exc:
                raise ModelContractError("INVALID_RESULT", f"output não cabe no JSON canônico: {exc}") from exc
        object.__setattr__(self, "usage", _check_usage(self.usage))

    @property
    def ok(self) -> bool:
        return self.failure is None

    def response_sha256(self) -> "str | None":
        """Hash do JSON canônico da resposta (o que se registra como proveniência), ou `None` se foi falha."""
        return hash_canonical_json(self.output).digest if self.output is not None else None

    @classmethod
    def success(cls, task_id: str, model: ModelIdentity, output: dict, usage: "dict | None" = None) -> "ModelResult":
        return cls(task_id, model, output=output, usage=usage)

    @classmethod
    def failed(cls, task_id: str, model: ModelIdentity, failure: ModelFailure, usage: "dict | None" = None) -> "ModelResult":
        return cls(task_id, model, failure=failure, usage=usage)

    def to_dict(self) -> dict:
        data: dict = {"kind": RESULT_KIND, "task_id": self.task_id, "ok": self.ok, "model": self.model.to_dict()}
        if self.output is not None:
            data["output"] = loads_strict(canonical_bytes(self.output))
        if self.failure is not None:
            data["failure"] = self.failure.to_dict()
        if self.usage is not None:
            data["usage"] = dict(self.usage)
        return data

    @classmethod
    def from_dict(cls, data: object) -> "ModelResult":
        problems = schema_violations(data, f"{SCHEMA_URN_PREFIX}{RESULT_SCHEMA}:1")
        if problems:
            raise ModelContractError("SCHEMA", "resposta fora do contrato: " + "; ".join(problems[:5]))
        assert isinstance(data, dict)
        failure = data.get("failure")
        return cls(
            data["task_id"], ModelIdentity.from_dict(data["model"]), output=data.get("output"),
            failure=ModelFailure.from_dict(failure) if failure is not None else None, usage=data.get("usage"),
        )


class ModelAdapter(Protocol):
    """Contrato de um adaptador. Nada além disto é exigido: sem SDK, sem rede obrigatória, sem credencial no contrato."""

    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def execute(self, task: ModelTask) -> ModelResult: ...


@dataclass(frozen=True)
class Ineligibility:
    """Um motivo, estável e legível, para um adaptador não poder executar uma tarefa."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def check_eligibility(descriptor: AdapterDescriptor, requirements: TaskRequirements) -> "tuple[Ineligibility, ...]":
    """Motivos pelos quais `descriptor` não pode executar `requirements`. Vazio = elegível. Determinístico e sem efeito."""
    if not isinstance(descriptor, AdapterDescriptor) or not isinstance(requirements, TaskRequirements):
        raise TypeError("check_eligibility exige AdapterDescriptor e TaskRequirements")
    found: list[Ineligibility] = []
    for capability in requirements.required_capabilities:
        if capability not in descriptor.capabilities:
            found.append(Ineligibility(MISSING_CAPABILITY, capability))
    if boundary_rank(descriptor.data_boundary) > boundary_rank(requirements.max_boundary):
        found.append(Ineligibility(BOUNDARY_EXCEEDED, f"{descriptor.data_boundary} > {requirements.max_boundary}"))
    if descriptor.data_boundary == "EXTERNAL":
        unverified = sorted(item.ref for item in requirements.evidence if not item.sensitivity_verified_clear)
        if unverified:
            found.append(Ineligibility(SENSITIVE_EVIDENCE_EXTERNAL, ", ".join(unverified[:5]) + (" ..." if len(unverified) > 5 else "")))
    return tuple(sorted(found, key=lambda item: (item.code, item.detail)))
