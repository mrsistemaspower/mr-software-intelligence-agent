"""Estado de uma execução (`RunState`): o que o agente sabe sobre o andamento de um fluxo, em disco e portátil.

Identidade determinística. O `run_id` deriva só de `workflow_id`, `software_id`, `workflow_version` e `run_key`
(um rótulo escolhido por quem executa, `default` por padrão). Relógio, máquina, caminho local e processo não entram.
Mudar uma entrada ou o snapshot de conhecimento NÃO muda o `run_id`: é a mesma execução reavaliada, e é isso que
permite retomar e invalidar. O que mudou aparece em `input_identity`, que é recalculada a cada partida e resume
grafo, configuração, snapshot e entradas.

Estados da execução (`RunStatus`):

    PENDING     criada, nenhum estágio começou
    RUNNING     em andamento; se encontrado no disco numa retomada, o processo anterior sumiu
    COMPLETED   todos os estágios concluídos (opcionais podem ter falhado ou ficado bloqueados)
    FAILED      um estágio falhou; nada mais rodou depois de uma falha crítica
    UNSAFE      uma fonte protegida foi alterada de forma inesperada; ver `violations`. Se a violação é achada na
                partida, o estado é gravado SEM reconciliar (nada é invalidado por causa de uma fonte que a pessoa
                ainda pode restaurar) e um estágio RUNNING deixado por uma queda anterior continua RUNNING

Invariantes verificados ao carregar (`RunStateError`): `run_id` recalculado, estágios únicos, no máximo um RUNNING e
coerente com `current_stage`, e o status compatível com os estados dos estágios. O arquivo é gravado com
`core.atomic`: uma queda durante a gravação deixa a versão anterior inteira.

Metadados de geração (`started.at`, `started.generator`, `started.resume_count`) não entram em nenhuma identidade. O núcleo não lê
relógio: `started.at` é fornecido por quem executa.
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping

from . import __version__
from .atomic import WriteResult, atomic_write_json
from .hashing import hash_canonical_json
from .paths import PathPortabilityError, assert_portable, to_fs_path
from .protection import ProtectionError, ProtectionRecord, Violation
from .schema_lite import Registry, Validator
from .serialization import CanonicalJsonError, canonical_dumps, loads_strict
from .snapshot import SNAPSHOT_ID_PATTERN, SOFTWARE_ID_PATTERN
from .stage_graph import VERSION_PATTERN, is_valid_name
from .stage_state import StageRecord, StageState, TransitionError

SCHEMA_NAME = "run-state"
SCHEMA_ID = "urn:mr-software-intelligence:schema:run-state:1"
IDENTITY_VERSION = 1
MAX_VIOLATIONS = 50
GENERATOR = f"core.run_state/{__version__}"

CORRUPT = "CORRUPT"
INVALID = "INVALID"

_RUN_ID = re.compile(r"^run:sha256:[0-9a-f]{64}$")
_INPUT_IDENTITY = re.compile(r"^inp:sha256:[0-9a-f]{64}$")
_DIGEST_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOFTWARE_ID = re.compile(SOFTWARE_ID_PATTERN)
_SNAPSHOT_ID = re.compile(SNAPSHOT_ID_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class RunStateError(ValueError):
    """Estado de execução malformado, inconsistente ou ilegível. `code`: CORRUPT ou INVALID."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNSAFE = "UNSAFE"


def compute_run_id(workflow_id: str, software_id: str | None, workflow_version: str, run_key: str) -> str:
    """Identidade determinística da execução. Nenhum relógio, máquina ou caminho."""
    payload: dict = {
        "kind": "workflow-run",
        "identity_version": IDENTITY_VERSION,
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "run_key": run_key,
    }
    if software_id is not None:
        payload["software_id"] = software_id
    return "run:sha256:" + hash_canonical_json(payload).digest


def compute_input_identity(graph_digest: str, config_digest: str, snapshot_id: str | None, inputs: Mapping[str, str]) -> str:
    """Resumo do que a execução recebeu agora: grafo, configuração, snapshot e hash de cada entrada."""
    payload: dict = {
        "kind": "run-input-identity",
        "identity_version": IDENTITY_VERSION,
        "graph_digest": graph_digest,
        "config_digest": config_digest,
        "inputs": dict(sorted(inputs.items())),
    }
    if snapshot_id is not None:
        payload["knowledge_snapshot_id"] = snapshot_id
    return "inp:sha256:" + hash_canonical_json(payload).digest


@dataclass(frozen=True)
class RunState:
    run_id: str
    workflow_id: str
    workflow_version: str
    run_key: str
    graph_digest: str
    config_digest: str
    input_identity: str
    status: RunStatus
    stages: tuple[StageRecord, ...]
    software_id: str | None = None
    knowledge_snapshot_id: str | None = None
    current_stage: str | None = None
    resume_count: int = 0
    started_at: str | None = None
    protection: tuple[ProtectionRecord, ...] = ()
    violations: tuple[Violation, ...] = ()
    violation_total: int = 0
    environment: Mapping[str, object] | None = field(default=None)
    generator: str = GENERATOR

    def __post_init__(self) -> None:
        if not is_valid_name(self.workflow_id) or not is_valid_name(self.run_key):
            raise RunStateError(INVALID, "workflow_id e run_key devem ser nomes lógicos válidos")
        if not (isinstance(self.workflow_version, str) and _VERSION.match(self.workflow_version)):
            raise RunStateError(INVALID, "workflow_version inválida")
        if self.software_id is not None and not (isinstance(self.software_id, str) and _SOFTWARE_ID.match(self.software_id)):
            raise RunStateError(INVALID, "software_id inválido")
        if self.knowledge_snapshot_id is not None and not (
            isinstance(self.knowledge_snapshot_id, str) and _SNAPSHOT_ID.match(self.knowledge_snapshot_id)
        ):
            raise RunStateError(INVALID, "knowledge_snapshot_id inválido")
        for label, value in (("graph_digest", self.graph_digest), ("config_digest", self.config_digest)):
            if not (isinstance(value, str) and _DIGEST_REF.match(value)):
                raise RunStateError(INVALID, f"{label} inválido")
        if not (isinstance(self.input_identity, str) and _INPUT_IDENTITY.match(self.input_identity)):
            raise RunStateError(INVALID, "input_identity inválida")
        if not isinstance(self.status, RunStatus):
            raise RunStateError(INVALID, "status deve ser RunStatus")
        if type(self.resume_count) is not int or self.resume_count < 0:
            raise RunStateError(INVALID, "resume_count deve ser inteiro não negativo")
        if self.started_at is not None and not (isinstance(self.started_at, str) and _TIMESTAMP.match(self.started_at)):
            raise RunStateError(INVALID, "started_at deve ser date-time RFC 3339")
        if not (isinstance(self.generator, str) and 0 < len(self.generator) <= 100):
            raise RunStateError(INVALID, "generator inválido")
        if not (isinstance(self.run_id, str) and _RUN_ID.match(self.run_id)) or self.run_id != compute_run_id(
            self.workflow_id, self.software_id, self.workflow_version, self.run_key
        ):
            raise RunStateError(CORRUPT, "run_id não confere com a identidade do fluxo")
        stages = tuple(self.stages)
        if not stages or not all(isinstance(item, StageRecord) for item in stages):
            raise RunStateError(INVALID, "stages deve conter StageRecord e não pode ser vazio")
        ids = [item.stage_id for item in stages]
        if len(set(ids)) != len(ids):
            raise RunStateError(INVALID, "estágio repetido")
        object.__setattr__(self, "stages", stages)
        running = [item.stage_id for item in stages if item.state is StageState.RUNNING]
        if len(running) > 1:
            raise RunStateError(INVALID, "mais de um estágio em RUNNING")
        if self.current_stage != (running[0] if running else None):
            raise RunStateError(INVALID, "current_stage deve ser o estágio em RUNNING (ou ausente quando não há)")
        states = {item.state for item in stages}
        if self.status is RunStatus.PENDING and states != {StageState.PENDING}:
            raise RunStateError(INVALID, "execução PENDING só tem estágios PENDING")
        if self.status is RunStatus.COMPLETED and states & {StageState.PENDING, StageState.RUNNING, StageState.INVALIDATED}:
            raise RunStateError(INVALID, "execução COMPLETED não pode ter estágio PENDING, RUNNING ou INVALIDATED")
        if self.status is RunStatus.FAILED and (StageState.RUNNING in states or StageState.FAILED not in states):
            raise RunStateError(INVALID, "execução FAILED exige um estágio FAILED e nenhum RUNNING")
        object.__setattr__(self, "protection", tuple(self.protection))
        object.__setattr__(self, "violations", tuple(self.violations))
        if len(self.violations) > MAX_VIOLATIONS or type(self.violation_total) is not int or self.violation_total < len(self.violations):
            raise RunStateError(INVALID, "violações inconsistentes")
        if self.environment is not None:
            try:
                canonical_dumps(dict(self.environment))
                assert_portable(dict(self.environment), "environment")
            except (CanonicalJsonError, PathPortabilityError, TypeError, ValueError) as exc:
                raise RunStateError(INVALID, f"environment inválido: {exc}") from exc

    # ------------------------------------------------------------------ consulta e cópia
    def stage(self, stage_id: str) -> StageRecord:
        for item in self.stages:
            if item.stage_id == stage_id:
                return item
        raise KeyError(stage_id)

    def with_stage(self, record: StageRecord) -> "RunState":
        replaced = tuple(record if item.stage_id == record.stage_id else item for item in self.stages)
        current = record.stage_id if record.state is StageState.RUNNING else (
            None if self.current_stage == record.stage_id else self.current_stage
        )
        return replace(self, stages=replaced, current_stage=current)

    def with_changes(self, **changes: object) -> "RunState":
        return replace(self, **changes)

    # ------------------------------------------------------------------ contrato
    def to_dict(self) -> dict:
        data: dict = {"schema": SCHEMA_ID, "run_id": self.run_id, "workflow_id": self.workflow_id}
        if self.software_id is not None:
            data["software_id"] = self.software_id
        data.update(workflow_version=self.workflow_version, run_key=self.run_key, graph_digest=self.graph_digest,
                    config_digest=self.config_digest)
        if self.knowledge_snapshot_id is not None:
            data["knowledge_snapshot_id"] = self.knowledge_snapshot_id
        data.update(input_identity=self.input_identity, status=self.status.value)
        if self.current_stage is not None:
            data["current_stage"] = self.current_stage
        started: dict = {"resume_count": self.resume_count, "generator": self.generator}
        if self.started_at is not None:
            started["at"] = self.started_at
        data["started"] = started
        data["stages"] = [item.to_dict() for item in self.stages]
        data["protection"] = [item.to_dict() for item in self.protection]
        data["violations"] = [item.to_dict() for item in self.violations]
        data["violation_total"] = self.violation_total
        if self.environment is not None:
            data["environment"] = dict(self.environment)
        return data

    @classmethod
    def from_dict(cls, data: object) -> "RunState":
        errors = _validator().validate(data, SCHEMA_NAME)
        if errors:
            raise RunStateError(CORRUPT, "fora do contrato run-state: " + "; ".join(errors[:5]))
        started = data["started"]
        try:
            return cls(
                run_id=data["run_id"],
                workflow_id=data["workflow_id"],
                workflow_version=data["workflow_version"],
                run_key=data["run_key"],
                graph_digest=data["graph_digest"],
                config_digest=data["config_digest"],
                input_identity=data["input_identity"],
                status=RunStatus(data["status"]),
                stages=tuple(StageRecord.from_dict(item) for item in data["stages"]),
                software_id=data.get("software_id"),
                knowledge_snapshot_id=data.get("knowledge_snapshot_id"),
                current_stage=data.get("current_stage"),
                resume_count=started["resume_count"],
                started_at=started.get("at"),
                protection=tuple(ProtectionRecord.from_dict(item) for item in data["protection"]),
                violations=tuple(Violation.from_dict(item) for item in data["violations"]),
                violation_total=data["violation_total"],
                environment=data.get("environment"),
                generator=started["generator"],
            )
        except (TransitionError, ProtectionError) as exc:
            raise RunStateError(CORRUPT, str(exc)) from exc


@functools.lru_cache(maxsize=1)
def _validator() -> Validator:
    return Validator(Registry())


def save_run_state(path: "str | os.PathLike[str]", state: RunState) -> WriteResult:
    """Grava o estado de forma atômica (JSON canônico legível). A versão anterior nunca fica pela metade."""
    return atomic_write_json(path, state.to_dict())


def load_run_state(path: "str | os.PathLike[str]") -> RunState:
    """Lê com `loads_strict` e verifica schema e invariantes. Arquivo ilegível ou adulterado levanta `RunStateError`.

    Um estado corrompido NUNCA é substituído em silêncio por um estado novo: quem chama decide.
    """
    try:
        with open(to_fs_path(path), "rb") as handle:
            raw = handle.read()
        return RunState.from_dict(loads_strict(raw))
    except RunStateError:
        raise
    except FileNotFoundError:
        raise
    except (OSError, CanonicalJsonError, ValueError, TypeError, KeyError) as exc:
        raise RunStateError(CORRUPT, f"estado ilegível: {type(exc).__name__}") from exc
