"""Checkpoint de estágio: a prova de que um estágio concluído pode ser pulado com segurança.

Um checkpoint amarra o resultado de um estágio ao que o produziu. A identidade de execução
(`execution_identity`, calculada com `core.identity.artifact_identity`) depende SÓ de:

- o fluxo (`workflow_id`, `software_id`) e a definição do estágio (id, versão e `definition_digest`);
- a configuração relevante do estágio (`parameters_digest`);
- o snapshot de conhecimento consumido (`knowledge_snapshot_id`);
- o hash de cada entrada externa declarada (`inputs`);
- a identidade das saídas de cada dependência (`upstream`).

Não depende de relógio, máquina, caminho absoluto, número da tentativa nem de modelo de linguagem. O checkpoint
também carrega o hash e o tamanho de cada saída (`outputs`) e seu resumo (`outputs_digest`).

Um checkpoint é VÁLIDO só se TUDO confere agora (`check_checkpoint`):

1. o arquivo do checkpoint está íntegro: `checkpoint_id`, `outputs_digest` e `execution_identity` são recalculados
   ao carregar, então adulterar qualquer campo é recusado (`CheckpointError`);
2. a identidade esperada, recalculada com os valores atuais, é a registrada, e os motivos de divergência são
   relatados um a um (definição, snapshot, configuração, entrada, dependência);
3. as saídas existem no disco, com os mesmos bytes (hash e tamanho).

Existência de arquivo, sozinha, nunca basta. A gravação do checkpoint é atômica (`core.atomic`).
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .atomic import WriteResult, atomic_write_json
from .hashing import Digest, hash_canonical_json
from .identity import ArtifactIdentity, artifact_identity
from .integrity import MISSING, MUTATED, UNREADABLE, expect_present, verify_sources
from .paths import PathPortabilityError, assert_portable, is_logical_path, normalize_logical_path, to_fs_path
from .schema_lite import Registry, Validator
from .serialization import CanonicalJsonError, loads_strict
from .snapshot import SNAPSHOT_ID_PATTERN, SOFTWARE_ID_PATTERN
from .stage_graph import StageSpec, is_valid_name

SCHEMA_NAME = "stage-checkpoint"
SCHEMA_ID = "urn:mr-software-intelligence:schema:stage-checkpoint:1"
IDENTITY_VERSION = 1
EXECUTION_TYPE = "stage.execution"
EXECUTION_VERSION = "1"
STATUS_COMPLETED = "COMPLETED"
OUTPUT_PREFIX = "outputs"
CHECKPOINT_DIRNAME = "checkpoints"

CORRUPT = "CORRUPT"
INVALID = "INVALID"

# Motivos de divergência, em ordem de prioridade (o primeiro vira a causa registrada no estágio).
MISMATCH_PRIORITY = (
    "WORKFLOW_CHANGED", "DEFINITION_CHANGED", "SNAPSHOT_CHANGED", "CONFIG_CHANGED", "INPUT_CHANGED",
    "UPSTREAM_CHANGED", "OUTPUT_SET_CHANGED", "OUTPUT_MISSING", "OUTPUT_MUTATED", "OUTPUT_UNREADABLE",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT_ID = re.compile(r"^ckpt:sha256:[0-9a-f]{64}$")
_EXECUTION_ID = re.compile(r"^art:stage\.execution:sha256:[0-9a-f]{64}$")
_SOFTWARE_ID = re.compile(SOFTWARE_ID_PATTERN)
_SNAPSHOT_ID = re.compile(SNAPSHOT_ID_PATTERN)


class CheckpointError(ValueError):
    """Checkpoint malformado, adulterado ou ilegível. `code`: CORRUPT (não dá para confiar no arquivo) ou INVALID."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def output_logical_path(stage_id: str, name: str) -> str:
    """Onde a saída `name` do estágio mora na raiz de saídas. Levanta se o resultado não for um caminho portátil."""
    if not (is_valid_name(stage_id) and is_valid_name(name)):
        raise CheckpointError(INVALID, "stage_id e nome de saída devem ser nomes lógicos válidos")
    try:
        return normalize_logical_path(f"{OUTPUT_PREFIX}/{stage_id}/{name}")
    except PathPortabilityError as exc:
        raise CheckpointError(INVALID, f"caminho de saída não portátil: {exc}") from exc


@dataclass(frozen=True)
class OutputRecord:
    name: str
    path: str
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if not is_valid_name(self.name):
            raise CheckpointError(INVALID, "nome de saída inválido")
        if not is_logical_path(self.path):
            raise CheckpointError(INVALID, "o caminho da saída deve ser um caminho lógico normalizado")
        if not (isinstance(self.sha256, str) and _SHA256.match(self.sha256)):
            raise CheckpointError(INVALID, "sha256 da saída inválido")
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise CheckpointError(INVALID, "byte_length da saída inválido")

    @classmethod
    def of(cls, stage_id: str, name: str, digest: Digest) -> "OutputRecord":
        return cls(name, output_logical_path(stage_id, name), digest.digest, digest.byte_length)

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "sha256": self.sha256, "byte_length": self.byte_length}

    @classmethod
    def from_dict(cls, data: object) -> "OutputRecord":
        if not isinstance(data, dict) or set(data) != {"name", "path", "sha256", "byte_length"}:
            raise CheckpointError(CORRUPT, "saída malformada")
        return cls(data["name"], data["path"], data["sha256"], data["byte_length"])


def outputs_digest(outputs: Iterable[OutputRecord]) -> str:
    """Resumo das saídas (`sha256:<hex>`), independente da ordem em que foram informadas."""
    ordered = sorted(outputs, key=lambda item: item.name)
    return str(hash_canonical_json([item.to_dict() for item in ordered]))


def parameters_digest(parameters: object) -> str:
    """Resumo da configuração relevante de um estágio (JSON canônico)."""
    return str(hash_canonical_json({} if parameters is None else parameters))


def execution_identity(
    *,
    workflow_id: str,
    software_id: str | None,
    spec: StageSpec,
    snapshot_id: str | None,
    parameters_digest: str,
    inputs: Mapping[str, str],
    upstream: Mapping[str, str],
) -> ArtifactIdentity:
    """Identidade de execução de um estágio: o que, com quais entradas, sobre qual conhecimento."""
    return _identity(
        workflow_id, software_id, spec.stage_id, spec.version, str(spec.definition_digest()), parameters_digest, snapshot_id, inputs, upstream
    )


def _identity(
    workflow_id: str,
    software_id: str | None,
    stage_id: str,
    stage_version: str,
    definition_digest: str,
    parameters_digest: str,
    snapshot_id: str | None,
    inputs: Mapping[str, str],
    upstream: Mapping[str, str],
) -> ArtifactIdentity:
    references: dict[str, str] = {}
    for name, value in inputs.items():
        references[f"input/{name}"] = value
    for name, value in upstream.items():
        references[f"upstream/{name}"] = value
    return artifact_identity(
        EXECUTION_TYPE,
        EXECUTION_VERSION,
        inputs=references,
        parameters={
            "workflow_id": workflow_id,
            "stage_id": stage_id,
            "stage_version": stage_version,
            "definition_digest": definition_digest,
            "parameters_digest": parameters_digest,
        },
        snapshot=snapshot_id,
        software_id=software_id,
    )


def _checkpoint_id(identity: str, outputs: str) -> str:
    payload = {
        "kind": "stage-checkpoint",
        "identity_version": IDENTITY_VERSION,
        "execution_identity": identity,
        "outputs_digest": outputs,
        "status": STATUS_COMPLETED,
    }
    return "ckpt:sha256:" + hash_canonical_json(payload).digest


def _check_refs(kind: str, mapping: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise CheckpointError(INVALID, f"{kind} deve ser um mapa")
    result: dict[str, str] = {}
    for name, value in mapping.items():
        if not is_valid_name(name):
            raise CheckpointError(INVALID, f"{kind}: nome inválido")
        if not (isinstance(value, str) and _DIGEST_REF.match(value)):
            raise CheckpointError(INVALID, f"{kind}: hash inválido para {name}")
        result[name] = value
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class StageCheckpoint:
    checkpoint_id: str
    workflow_id: str
    stage_id: str
    stage_version: str
    definition_digest: str
    parameters_digest: str
    execution_identity: str
    outputs: tuple[OutputRecord, ...]
    outputs_digest: str
    attempt: int
    inputs: Mapping[str, str]
    upstream: Mapping[str, str]
    software_id: str | None = None
    knowledge_snapshot_id: str | None = None
    status: str = STATUS_COMPLETED

    def __post_init__(self) -> None:
        if not is_valid_name(self.workflow_id) or not is_valid_name(self.stage_id):
            raise CheckpointError(INVALID, "workflow_id e stage_id devem ser nomes lógicos válidos")
        if self.status != STATUS_COMPLETED:
            raise CheckpointError(INVALID, "só existe checkpoint de estágio COMPLETED")
        if self.software_id is not None and not (isinstance(self.software_id, str) and _SOFTWARE_ID.match(self.software_id)):
            raise CheckpointError(INVALID, "software_id inválido")
        if self.knowledge_snapshot_id is not None and not (
            isinstance(self.knowledge_snapshot_id, str) and _SNAPSHOT_ID.match(self.knowledge_snapshot_id)
        ):
            raise CheckpointError(INVALID, "knowledge_snapshot_id inválido")
        if type(self.attempt) is not int or self.attempt < 1:
            raise CheckpointError(INVALID, "attempt deve ser inteiro positivo")
        for label, value in (("definition_digest", self.definition_digest), ("parameters_digest", self.parameters_digest)):
            if not (isinstance(value, str) and _DIGEST_REF.match(value)):
                raise CheckpointError(INVALID, f"{label} inválido")
        object.__setattr__(self, "inputs", _check_refs("inputs", self.inputs))
        object.__setattr__(self, "upstream", _check_refs("upstream", self.upstream))
        outputs = tuple(self.outputs)
        if not all(isinstance(item, OutputRecord) for item in outputs):
            raise CheckpointError(INVALID, "outputs deve conter OutputRecord")
        names = [item.name for item in outputs]
        if names != sorted(names) or len(set(names)) != len(names):
            raise CheckpointError(INVALID, "saídas devem estar ordenadas por nome, sem repetição")
        for item in outputs:
            if item.path != output_logical_path(self.stage_id, item.name):
                raise CheckpointError(CORRUPT, "o caminho da saída não é o caminho canônico do estágio")
        object.__setattr__(self, "outputs", outputs)
        if self.outputs_digest != outputs_digest(outputs):
            raise CheckpointError(CORRUPT, "outputs_digest não confere com as saídas")
        try:
            expected = _identity(
                self.workflow_id, self.software_id, self.stage_id, self.stage_version, self.definition_digest,
                self.parameters_digest, self.knowledge_snapshot_id, self.inputs, self.upstream,
            )
        except ValueError as exc:
            raise CheckpointError(INVALID, f"identidade não calculável: {exc}") from exc
        if not (isinstance(self.execution_identity, str) and _EXECUTION_ID.match(self.execution_identity)) or (
            self.execution_identity != expected.artifact_id
        ):
            raise CheckpointError(CORRUPT, "execution_identity não confere com os campos do checkpoint")
        if self.checkpoint_id != _checkpoint_id(self.execution_identity, self.outputs_digest) or not _CHECKPOINT_ID.match(
            str(self.checkpoint_id)
        ):
            raise CheckpointError(CORRUPT, "checkpoint_id não confere com o conteúdo (adulterado)")
        try:
            assert_portable(self.to_dict(), "checkpoint")
        except PathPortabilityError as exc:
            raise CheckpointError(INVALID, str(exc)) from exc

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        software_id: str | None,
        spec: StageSpec,
        snapshot_id: str | None,
        parameters_digest: str,
        inputs: Mapping[str, str],
        upstream: Mapping[str, str],
        outputs: Iterable[OutputRecord],
        attempt: int,
    ) -> "StageCheckpoint":
        ordered = tuple(sorted(outputs, key=lambda item: item.name))
        identity = execution_identity(
            workflow_id=workflow_id, software_id=software_id, spec=spec, snapshot_id=snapshot_id,
            parameters_digest=parameters_digest, inputs=inputs, upstream=upstream,
        )
        digest = outputs_digest(ordered)
        return cls(
            checkpoint_id=_checkpoint_id(identity.artifact_id, digest),
            workflow_id=workflow_id,
            stage_id=spec.stage_id,
            stage_version=spec.version,
            definition_digest=str(spec.definition_digest()),
            parameters_digest=parameters_digest,
            execution_identity=identity.artifact_id,
            outputs=ordered,
            outputs_digest=digest,
            attempt=attempt,
            inputs=dict(inputs),
            upstream=dict(upstream),
            software_id=software_id,
            knowledge_snapshot_id=snapshot_id,
        )

    def to_dict(self) -> dict:
        data: dict = {"schema": SCHEMA_ID, "checkpoint_id": self.checkpoint_id, "workflow_id": self.workflow_id}
        if self.software_id is not None:
            data["software_id"] = self.software_id
        data.update(
            stage_id=self.stage_id,
            stage_version=self.stage_version,
            definition_digest=self.definition_digest,
            parameters_digest=self.parameters_digest,
        )
        if self.knowledge_snapshot_id is not None:
            data["knowledge_snapshot_id"] = self.knowledge_snapshot_id
        data.update(
            inputs=dict(self.inputs),
            upstream=dict(self.upstream),
            execution_identity=self.execution_identity,
            outputs=[item.to_dict() for item in self.outputs],
            outputs_digest=self.outputs_digest,
            status=self.status,
            attempt=self.attempt,
        )
        return data

    @classmethod
    def from_dict(cls, data: object) -> "StageCheckpoint":
        errors = _validator().validate(data, SCHEMA_NAME)
        if errors:
            raise CheckpointError(CORRUPT, "fora do contrato stage-checkpoint: " + "; ".join(errors[:5]))
        return cls(
            checkpoint_id=data["checkpoint_id"],
            workflow_id=data["workflow_id"],
            stage_id=data["stage_id"],
            stage_version=data["stage_version"],
            definition_digest=data["definition_digest"],
            parameters_digest=data["parameters_digest"],
            execution_identity=data["execution_identity"],
            outputs=tuple(OutputRecord.from_dict(item) for item in data["outputs"]),
            outputs_digest=data["outputs_digest"],
            attempt=data["attempt"],
            inputs=data["inputs"],
            upstream=data["upstream"],
            software_id=data.get("software_id"),
            knowledge_snapshot_id=data.get("knowledge_snapshot_id"),
            status=data["status"],
        )


@functools.lru_cache(maxsize=1)
def _validator() -> Validator:
    return Validator(Registry())


@dataclass(frozen=True)
class Mismatch:
    """Um motivo pelo qual o checkpoint deixou de valer. `detail` nomeia a entrada, dependência ou saída."""

    code: str
    detail: str | None = None


def check_checkpoint(
    checkpoint: StageCheckpoint,
    *,
    workflow_id: str,
    software_id: str | None,
    spec: StageSpec,
    snapshot_id: str | None,
    parameters_digest: str,
    inputs: Mapping[str, "str | None"],
    upstream: Mapping[str, str],
    output_root: "str | os.PathLike[str]",
) -> tuple[Mismatch, ...]:
    """Todos os motivos pelos quais `checkpoint` NÃO vale para o estado atual (vazio = válido). Não modifica nada.

    `inputs`: hash atual de cada entrada declarada (`None` = indisponível, o que conta como mudança).
    `upstream`: `outputs_digest` atual de cada dependência.
    """
    found: list[Mismatch] = []
    if (checkpoint.workflow_id, checkpoint.software_id, checkpoint.stage_id) != (workflow_id, software_id, spec.stage_id):
        found.append(Mismatch("WORKFLOW_CHANGED"))
    if checkpoint.definition_digest != str(spec.definition_digest()) or checkpoint.stage_version != spec.version:
        found.append(Mismatch("DEFINITION_CHANGED"))
    if checkpoint.knowledge_snapshot_id != snapshot_id:
        found.append(Mismatch("SNAPSHOT_CHANGED"))
    if checkpoint.parameters_digest != parameters_digest:
        found.append(Mismatch("CONFIG_CHANGED"))
    for name in sorted(set(spec.input_contracts) | set(checkpoint.inputs)):
        current = inputs.get(name)
        if current is None or checkpoint.inputs.get(name) != current:
            found.append(Mismatch("INPUT_CHANGED", name))
    for name in sorted(set(spec.depends_on) | set(checkpoint.upstream)):
        if checkpoint.upstream.get(name) != upstream.get(name):
            found.append(Mismatch("UPSTREAM_CHANGED", name))
    recorded = {item.name: item for item in checkpoint.outputs}
    if set(recorded) != set(spec.output_contracts):
        found.append(Mismatch("OUTPUT_SET_CHANGED"))
    names_by_path = {item.path: item.name for item in checkpoint.outputs}
    if checkpoint.outputs:
        report = verify_sources(
            output_root, [expect_present(item.path, item.sha256, item.byte_length) for item in checkpoint.outputs]
        )
        codes = {MISSING: "OUTPUT_MISSING", MUTATED: "OUTPUT_MUTATED", UNREADABLE: "OUTPUT_UNREADABLE"}
        for finding in report.findings:
            found.append(Mismatch(codes.get(finding.kind, "OUTPUT_UNREADABLE"), names_by_path.get(finding.path)))
    if not found:
        try:
            recomputed = execution_identity(
                workflow_id=workflow_id, software_id=software_id, spec=spec, snapshot_id=snapshot_id,
                parameters_digest=parameters_digest, inputs={k: v for k, v in inputs.items() if v is not None and k in spec.input_contracts},
                upstream={k: upstream[k] for k in spec.depends_on if k in upstream},
            )
        except ValueError:
            found.append(Mismatch("DEFINITION_CHANGED"))
        else:
            if recomputed.artifact_id != checkpoint.execution_identity:
                found.append(Mismatch("DEFINITION_CHANGED"))
    rank = {code: index for index, code in enumerate(MISMATCH_PRIORITY)}
    return tuple(sorted(found, key=lambda item: (rank[item.code], item.detail or "")))


class CheckpointStore:
    """Checkpoints de um diretório local, um arquivo por estágio. A gravação é atômica."""

    def __init__(self, directory: "str | os.PathLike[str]") -> None:
        self._directory = Path(directory)

    def path(self, stage_id: str) -> Path:
        if not is_valid_name(stage_id):
            raise CheckpointError(INVALID, "stage_id inválido")
        return self._directory / f"{stage_id}.json"

    def save(self, checkpoint: StageCheckpoint) -> WriteResult:
        return atomic_write_json(self.path(checkpoint.stage_id), checkpoint.to_dict())

    def load(self, stage_id: str) -> StageCheckpoint | None:
        """O checkpoint do estágio, `None` se não existe. Arquivo ilegível ou adulterado levanta `CheckpointError`."""
        target = to_fs_path(self.path(stage_id))
        if not os.path.exists(target):
            return None
        try:
            with open(target, "rb") as handle:
                raw = handle.read()
            checkpoint = StageCheckpoint.from_dict(loads_strict(raw))
        except CheckpointError:
            raise
        except (OSError, CanonicalJsonError, ValueError, TypeError, KeyError) as exc:
            raise CheckpointError(CORRUPT, f"checkpoint ilegível: {type(exc).__name__}") from exc
        if checkpoint.stage_id != stage_id:
            raise CheckpointError(CORRUPT, "o checkpoint pertence a outro estágio")
        return checkpoint

    def delete(self, stage_id: str) -> bool:
        """Remove o checkpoint (arquivo de estado do próprio agente). `True` se existia."""
        target = to_fs_path(self.path(stage_id))
        try:
            os.unlink(target)
        except FileNotFoundError:
            return False
        return True
