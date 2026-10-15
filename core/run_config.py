"""Configuração de uma execução: portátil, validável e com hash.

`RunConfig` reúne tudo o que define O QUE uma execução faz, sem nenhum caminho local:

- o fluxo (`workflow_id`, `workflow_version`, `software_id` opcional, `run_key`) e o grafo de estágios;
- as raízes de fonte e saída, por NOME, com a política de proteção de cada uma (ver `core.protection`);
- a raiz de estado e a raiz de saídas (obrigatoriamente `MUTABLE_OUTPUT`);
- as entradas nomeadas, cada uma ligada a um arquivo (raiz + caminho lógico) de uma raiz protegida;
- os parâmetros de cada estágio (a configuração "relevante" que entra na identidade do estágio);
- a política para estágio interrompido.

Os caminhos locais das raízes são fornecidos só na hora de executar (`Orchestrator(..., roots=...)`) e nunca entram aqui:
mover o workspace de lugar não muda `digest()`. O conhecimento consumido (`KnowledgeSnapshot`) também é fornecido na hora
de executar, porque muda independentemente da configuração.

A configuração não fixa datas, quantidade de estágios, totais esperados, nomes de software nem caminhos absolutos.
Contrato JSON: `schemas/run-config.schema.json`.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .hashing import Digest, hash_canonical_json
from .paths import PathPortabilityError, assert_portable, normalize_logical_path
from .protection import Protection, ProtectionError, RootPolicy
from .schema_lite import Registry, Validator
from .serialization import CanonicalJsonError, canonical_bytes, loads_strict
from .snapshot import SOFTWARE_ID_PATTERN
from .stage_graph import VERSION_PATTERN, GraphError, StageGraph, is_valid_name

SCHEMA_NAME = "run-config"
SCHEMA_ID = "urn:mr-software-intelligence:schema:run-config:1"
DEFAULT_RUN_KEY = "default"

# Layout dentro da raiz de estado.
STATE_DIR = "state"
RUN_STATE_FILE = "state/run-state.json"
CHECKPOINT_DIR = "state/checkpoints"
BASELINE_DIR = "state/protection"

_SOFTWARE_ID = re.compile(SOFTWARE_ID_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)


class ConfigError(ValueError):
    """Configuração inconsistente. Levantada antes de qualquer execução."""


class InterruptionPolicy(str, Enum):
    """O que fazer com um estágio que ficou em RUNNING porque o processo sumiu.

    REQUEUE: volta a PENDING e a tentativa interrompida é devolvida (não foi falha do estágio).
    FAIL:    vira FAILED (categoria INTERRUPTED) e a tentativa conta.
    Nenhuma das duas presume que o estágio terminou, e nenhuma inventa saída.
    """

    REQUEUE = "REQUEUE"
    FAIL = "FAIL"


@dataclass(frozen=True)
class InputBinding:
    """Liga uma entrada nomeada a um arquivo: raiz (por nome) + caminho lógico."""

    root: str
    path: str

    def __post_init__(self) -> None:
        if not is_valid_name(self.root):
            raise ConfigError(f"raiz da entrada inválida: {self.root!r}")
        try:
            normalized = normalize_logical_path(self.path)
        except PathPortabilityError as exc:
            raise ConfigError(f"caminho da entrada inválido: {exc}") from exc
        if normalized != self.path:
            raise ConfigError("o caminho da entrada deve estar normalizado")

    def to_dict(self) -> dict:
        return {"root": self.root, "path": self.path}

    @classmethod
    def from_dict(cls, data: object) -> "InputBinding":
        if not isinstance(data, dict) or set(data) != {"root", "path"}:
            raise ConfigError("vínculo de entrada malformado")
        return cls(data["root"], data["path"])


@dataclass(frozen=True)
class RunConfig:
    workflow_id: str
    workflow_version: str
    graph: StageGraph
    roots: tuple[RootPolicy, ...]
    state_root: str
    output_root: str
    inputs: Mapping[str, InputBinding] = field(default_factory=dict)
    stage_parameters: Mapping[str, dict] = field(default_factory=dict)
    software_id: str | None = None
    run_key: str = DEFAULT_RUN_KEY
    interruption_policy: InterruptionPolicy = InterruptionPolicy.REQUEUE

    def __post_init__(self) -> None:
        if not is_valid_name(self.workflow_id):
            raise ConfigError(f"workflow_id inválido: {self.workflow_id!r}")
        if not (isinstance(self.workflow_version, str) and _VERSION.match(self.workflow_version)):
            raise ConfigError("workflow_version inválida")
        if self.software_id is not None and not (isinstance(self.software_id, str) and _SOFTWARE_ID.match(self.software_id)):
            raise ConfigError("software_id inválido")
        if not is_valid_name(self.run_key):
            raise ConfigError(f"run_key inválida: {self.run_key!r}")
        if not isinstance(self.graph, StageGraph):
            raise ConfigError("graph deve ser StageGraph")
        try:
            object.__setattr__(self, "interruption_policy", InterruptionPolicy(self.interruption_policy))
        except ValueError:
            raise ConfigError(f"política de interrupção desconhecida: {self.interruption_policy!r}") from None
        roots = tuple(self.roots)
        if not all(isinstance(item, RootPolicy) for item in roots):
            raise ConfigError("roots deve conter RootPolicy")
        names = [item.name for item in roots]
        if len(set(names)) != len(names):
            raise ConfigError("nome de raiz repetido")
        object.__setattr__(self, "roots", tuple(sorted(roots, key=lambda item: item.name)))
        by_name = {item.name: item for item in self.roots}
        for label, name in (("state_root", self.state_root), ("output_root", self.output_root)):
            policy = by_name.get(name)
            if policy is None:
                raise ConfigError(f"{label} não está entre as raízes declaradas: {name!r}")
            if policy.protection is not Protection.MUTABLE_OUTPUT:
                raise ConfigError(f"{label} deve ser MUTABLE_OUTPUT; {name} é {policy.protection.value}")
        bindings: dict[str, InputBinding] = {}
        for name, binding in dict(self.inputs).items():
            if not is_valid_name(name):
                raise ConfigError(f"nome de entrada inválido: {name!r}")
            if not isinstance(binding, InputBinding):
                raise ConfigError("inputs deve mapear nomes para InputBinding")
            policy = by_name.get(binding.root)
            if policy is None:
                raise ConfigError(f"a entrada {name} usa a raiz {binding.root!r}, que não foi declarada")
            if not policy.protected:
                raise ConfigError(f"a entrada {name} está numa raiz MUTABLE_OUTPUT; entradas vêm de raízes READ_ONLY ou FROZEN")
            bindings[name] = binding
        declared = {name for spec in self.graph.stages for name in spec.input_contracts}
        missing = sorted(declared - set(bindings))
        if missing:
            raise ConfigError("entradas declaradas por estágios sem vínculo: " + ", ".join(missing))
        unused = sorted(set(bindings) - declared)
        if unused:
            raise ConfigError("vínculos para entradas que nenhum estágio declara: " + ", ".join(unused))
        object.__setattr__(self, "inputs", dict(sorted(bindings.items())))
        params: dict[str, dict] = {}
        for stage_id, value in dict(self.stage_parameters).items():
            if stage_id not in self.graph:
                raise ConfigError(f"parâmetros para estágio inexistente: {stage_id!r}")
            if not isinstance(value, dict):
                raise ConfigError(f"os parâmetros de {stage_id} devem ser um objeto")
            try:
                params[stage_id] = loads_strict(canonical_bytes(value))
            except CanonicalJsonError as exc:
                raise ConfigError(f"os parâmetros de {stage_id} não cabem no JSON canônico: {exc}") from exc
        object.__setattr__(self, "stage_parameters", dict(sorted(params.items())))
        try:
            assert_portable(self.to_dict(), "configuração")
        except PathPortabilityError as exc:
            raise ConfigError(str(exc)) from exc

    def parameters_of(self, stage_id: str) -> dict:
        return self.stage_parameters.get(stage_id, {})

    def policy(self, root: str) -> RootPolicy:
        for item in self.roots:
            if item.name == root:
                return item
        raise ConfigError(f"raiz desconhecida: {root}")

    def to_dict(self) -> dict:
        data: dict = {"schema": SCHEMA_ID, "workflow_id": self.workflow_id, "workflow_version": self.workflow_version}
        if self.software_id is not None:
            data["software_id"] = self.software_id
        data.update(
            run_key=self.run_key,
            graph=self.graph.to_dict(),
            roots=[item.to_dict() for item in self.roots],
            state_root=self.state_root,
            output_root=self.output_root,
            inputs={name: binding.to_dict() for name, binding in self.inputs.items()},
            stage_parameters={name: value for name, value in self.stage_parameters.items()},
            interruption_policy=self.interruption_policy.value,
        )
        return data

    def digest(self) -> Digest:
        """Identidade da configuração: mesmos dados, mesmo hash, em qualquer máquina e qualquer pasta."""
        return hash_canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> "RunConfig":
        errors = _validator().validate(data, SCHEMA_NAME)
        if errors:
            raise ConfigError("fora do contrato run-config: " + "; ".join(errors[:5]))
        try:
            return cls(
                workflow_id=data["workflow_id"],
                workflow_version=data["workflow_version"],
                graph=StageGraph.from_dict(data["graph"]),
                roots=tuple(RootPolicy.from_dict(item) for item in data["roots"]),
                state_root=data["state_root"],
                output_root=data["output_root"],
                inputs={name: InputBinding.from_dict(item) for name, item in data.get("inputs", {}).items()},
                stage_parameters=dict(data.get("stage_parameters", {})),
                software_id=data.get("software_id"),
                run_key=data.get("run_key", DEFAULT_RUN_KEY),
                interruption_policy=data.get("interruption_policy", InterruptionPolicy.REQUEUE.value),
            )
        except (GraphError, ProtectionError) as exc:
            raise ConfigError(str(exc)) from exc


@functools.lru_cache(maxsize=1)
def _validator() -> Validator:
    return Validator(Registry())
