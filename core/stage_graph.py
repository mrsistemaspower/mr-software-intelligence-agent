"""Grafo de estágios configurável: o "o quê" e o "quando" de um fluxo de execução longa.

Um fluxo é um conjunto de estágios nomeados com dependências explícitas. Os nomes são arbitrários: nada aqui
conhece software, fase, tela ou quantidade de estágios. Cada estágio declara:

- `stage_id` e `version` (versão da definição do estágio: mudar a versão invalida o trabalho já feito);
- `depends_on`: estágios que precisam estar concluídos antes;
- `input_contracts`: nomes lógicos das entradas externas que o estágio consome;
- `output_contracts`: nomes lógicos das saídas que o estágio é obrigado a produzir;
- `optional`: falha de um estágio opcional não derruba a execução;
- `retry`: número máximo de tentativas (política de repetição).

Validação, antes de qualquer execução (`GraphError`, com `code`): grafo vazio, identificador duplicado,
autodependência, dependência desconhecida, ciclo e estágio obrigatório que depende de estágio opcional
(um opcional que falha deixaria o dependente obrigatório sem como rodar; a regra impede o impasse na origem).

Ordem determinística: ordenação topológica de Kahn em que, entre estágios prontos ao mesmo tempo, vence o
menor `stage_id` em ordem de ponto de código Unicode. A ordem de declaração NUNCA influencia a ordem de
execução, a serialização nem o `digest()` do grafo.

Nenhuma função aqui lê relógio, ambiente ou disco.
"""
from __future__ import annotations

import functools
import heapq
import re
from dataclasses import dataclass
from typing import Iterable

from .hashing import Digest, hash_canonical_json
from .paths import PathPortabilityError, normalize_logical_path
from .schema_lite import Registry, Validator

SCHEMA_NAME = "stage-graph"
SCHEMA_ID = "urn:mr-software-intelligence:schema:stage-graph:1"
NAME_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
MAX_NAME_LENGTH = 64
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
MAX_ATTEMPTS_LIMIT = 20

EMPTY_GRAPH = "EMPTY_GRAPH"
INVALID_STAGE = "INVALID_STAGE"
DUPLICATE_STAGE = "DUPLICATE_STAGE"
SELF_DEPENDENCY = "SELF_DEPENDENCY"
UNKNOWN_DEPENDENCY = "UNKNOWN_DEPENDENCY"
REQUIRED_DEPENDS_ON_OPTIONAL = "REQUIRED_DEPENDS_ON_OPTIONAL"
CYCLE = "CYCLE"
INVALID_GRAPH = "INVALID_GRAPH"

_NAME = re.compile(NAME_PATTERN)
_VERSION = re.compile(VERSION_PATTERN)


class GraphError(ValueError):
    """Grafo inválido. `code` é estável e serve para decidir, `str(erro)` serve para pessoas."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def is_valid_name(value: object) -> bool:
    """Nome lógico válido (estágio, entrada ou saída): minúsculas, dígitos e separadores `.`, `_`, `-`.

    Também precisa ser um componente de caminho portátil (sem nome reservado do Windows), porque saídas viram arquivos.
    """
    if not (isinstance(value, str) and len(value) <= MAX_NAME_LENGTH and _NAME.match(value)):
        return False
    try:
        return normalize_logical_path(value) == value
    except PathPortabilityError:
        return False


def _names(kind: str, values: Iterable[object]) -> tuple[str, ...]:
    items = list(values)
    for item in items:
        if not is_valid_name(item):
            raise GraphError(INVALID_STAGE, f"{kind} inválido: {item!r} (esperado {NAME_PATTERN}, até {MAX_NAME_LENGTH} caracteres)")
    if len(set(items)) != len(items):
        raise GraphError(INVALID_STAGE, f"{kind} com nome repetido")
    return tuple(sorted(items))


@dataclass(frozen=True)
class RetryPolicy:
    """Quantas tentativas um estágio tem no total. A repetição acontece em uma retomada, nunca em laço interno."""

    max_attempts: int = 1

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise GraphError(INVALID_STAGE, f"max_attempts deve ser inteiro de 1 a {MAX_ATTEMPTS_LIMIT}")


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    version: str = "1"
    depends_on: tuple[str, ...] = ()
    input_contracts: tuple[str, ...] = ()
    output_contracts: tuple[str, ...] = ()
    optional: bool = False
    retry: RetryPolicy = RetryPolicy()

    def __post_init__(self) -> None:
        if not is_valid_name(self.stage_id):
            raise GraphError(INVALID_STAGE, f"stage_id inválido: {self.stage_id!r} (esperado {NAME_PATTERN})")
        if not (isinstance(self.version, str) and _VERSION.match(self.version)):
            raise GraphError(INVALID_STAGE, f"{self.stage_id}: version inválida (esperado {VERSION_PATTERN})")
        if type(self.optional) is not bool:
            raise GraphError(INVALID_STAGE, f"{self.stage_id}: optional deve ser booleano")
        if not isinstance(self.retry, RetryPolicy):
            raise GraphError(INVALID_STAGE, f"{self.stage_id}: retry deve ser RetryPolicy")
        object.__setattr__(self, "depends_on", _names("depends_on", self.depends_on))
        object.__setattr__(self, "input_contracts", _names("input_contracts", self.input_contracts))
        object.__setattr__(self, "output_contracts", _names("output_contracts", self.output_contracts))

    def definition(self) -> dict:
        """O que define o trabalho do estágio. A política de repetição fica de fora: mudar o número de
        tentativas não muda o resultado, então não deve invalidar trabalho concluído."""
        return {
            "stage_id": self.stage_id,
            "version": self.version,
            "depends_on": list(self.depends_on),
            "input_contracts": list(self.input_contracts),
            "output_contracts": list(self.output_contracts),
            "optional": self.optional,
        }

    def definition_digest(self) -> Digest:
        return hash_canonical_json(self.definition())

    def to_dict(self) -> dict:
        data = self.definition()
        data["retry"] = {"max_attempts": self.retry.max_attempts}
        return data

    @classmethod
    def from_dict(cls, data: object) -> "StageSpec":
        allowed = {"stage_id", "version", "depends_on", "input_contracts", "output_contracts", "optional", "retry"}
        if not isinstance(data, dict) or "stage_id" not in data or set(data) - allowed:
            raise GraphError(INVALID_STAGE, "definição de estágio malformada")
        retry = data.get("retry", {"max_attempts": 1})
        if not isinstance(retry, dict) or set(retry) - {"max_attempts"}:
            raise GraphError(INVALID_STAGE, "retry malformado")
        return cls(
            stage_id=data["stage_id"],
            version=data.get("version", "1"),
            depends_on=tuple(data.get("depends_on", ())),
            input_contracts=tuple(data.get("input_contracts", ())),
            output_contracts=tuple(data.get("output_contracts", ())),
            optional=data.get("optional", False),
            retry=RetryPolicy(retry.get("max_attempts", 1)),
        )


def _find_cycle(by_id: dict[str, StageSpec], stuck: set[str]) -> list[str]:
    """Um ciclo concreto entre estágios que o algoritmo de Kahn não conseguiu ordenar (determinístico)."""
    current = min(stuck)
    position: dict[str, int] = {}
    walk: list[str] = []
    while current not in position:
        position[current] = len(walk)
        walk.append(current)
        # Todo estágio que sobrou tem ao menos uma dependência que também sobrou.
        current = min(dep for dep in by_id[current].depends_on if dep in stuck)
    return walk[position[current]:]


class StageGraph:
    """Grafo validado e imutável. Construir levanta `GraphError` se qualquer regra for violada."""

    def __init__(self, stages: Iterable[StageSpec]) -> None:
        items = list(stages)
        if not items:
            raise GraphError(EMPTY_GRAPH, "o grafo não tem estágios")
        if not all(isinstance(item, StageSpec) for item in items):
            raise TypeError("stages deve conter StageSpec")
        counts: dict[str, int] = {}
        for item in items:
            counts[item.stage_id] = counts.get(item.stage_id, 0) + 1
        repeated = sorted(name for name, total in counts.items() if total > 1)
        if repeated:
            raise GraphError(DUPLICATE_STAGE, "stage_id repetido: " + ", ".join(repeated))
        by_id = {item.stage_id: item for item in items}
        for name in sorted(by_id):
            spec = by_id[name]
            if name in spec.depends_on:
                raise GraphError(SELF_DEPENDENCY, f"o estágio {name} depende de si mesmo")
            unknown = [dep for dep in spec.depends_on if dep not in by_id]
            if unknown:
                raise GraphError(UNKNOWN_DEPENDENCY, f"o estágio {name} depende de estágio inexistente: {', '.join(unknown)}")
            if not spec.optional:
                optional = [dep for dep in spec.depends_on if by_id[dep].optional]
                if optional:
                    raise GraphError(
                        REQUIRED_DEPENDS_ON_OPTIONAL,
                        f"o estágio obrigatório {name} depende de estágio opcional: {', '.join(optional)}",
                    )
        self._by_id = by_id
        self._order = self._sort(by_id)
        self._dependents = {name: tuple(sorted(o for o in by_id if name in by_id[o].depends_on)) for name in by_id}

    @staticmethod
    def _sort(by_id: dict[str, StageSpec]) -> tuple[str, ...]:
        pending = {name: len(spec.depends_on) for name, spec in by_id.items()}
        ready = [name for name, count in pending.items() if count == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            name = heapq.heappop(ready)
            order.append(name)
            for other, spec in by_id.items():
                if name in spec.depends_on:
                    pending[other] -= 1
                    if pending[other] == 0:
                        heapq.heappush(ready, other)
        if len(order) != len(by_id):
            stuck = set(by_id) - set(order)
            cycle = _find_cycle(by_id, stuck)
            chain = " -> ".join(cycle + [cycle[0]])
            raise GraphError(CYCLE, f"ciclo de dependências (cada estágio depende do próximo): {chain}")
        return tuple(order)

    # ------------------------------------------------------------------ consulta
    @property
    def order(self) -> tuple[str, ...]:
        """Ordem topológica determinística (ver o docstring do módulo)."""
        return self._order

    @property
    def stages(self) -> tuple[StageSpec, ...]:
        return tuple(self._by_id[name] for name in self._order)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, stage_id: object) -> bool:
        return stage_id in self._by_id

    def get(self, stage_id: str) -> StageSpec:
        try:
            return self._by_id[stage_id]
        except KeyError:
            raise GraphError(UNKNOWN_DEPENDENCY, f"estágio desconhecido: {stage_id}") from None

    def dependents(self, stage_id: str) -> tuple[str, ...]:
        """Dependentes diretos, em ordem alfabética."""
        self.get(stage_id)
        return self._dependents[stage_id]

    def descendants(self, stage_id: str) -> tuple[str, ...]:
        """Todos os que dependem, direta ou indiretamente, do estágio. Em ordem topológica. Exclui o próprio estágio."""
        self.get(stage_id)
        seen: set[str] = set()
        stack = [stage_id]
        while stack:
            for child in self._dependents[stack.pop()]:
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return tuple(name for name in self._order if name in seen)

    def ancestors(self, stage_id: str) -> tuple[str, ...]:
        """Todos de que o estágio depende, direta ou indiretamente. Em ordem topológica. Exclui o próprio estágio."""
        self.get(stage_id)
        seen: set[str] = set()
        stack = [stage_id]
        while stack:
            for parent in self._by_id[stack.pop()].depends_on:
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return tuple(name for name in self._order if name in seen)

    # ------------------------------------------------------------------ contrato
    def to_dict(self) -> dict:
        return {"schema": SCHEMA_ID, "stages": [spec.to_dict() for spec in self.stages]}

    def digest(self) -> Digest:
        """Identidade do grafo: independe da ordem de declaração."""
        return hash_canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> "StageGraph":
        errors = _validator().validate(data, SCHEMA_NAME)
        if errors:
            raise GraphError(INVALID_GRAPH, "fora do contrato stage-graph: " + "; ".join(errors[:5]))
        return cls(StageSpec.from_dict(item) for item in data["stages"])

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StageGraph) and self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        return hash(self.digest().digest)


@functools.lru_cache(maxsize=1)
def _validator() -> Validator:
    return Validator(Registry())


def graph_from_agent_workflow(document: object) -> StageGraph:
    """Grafo de estágios a partir de um registro `agent-workflow` da Foundation (um único conceito de fluxo).

    Cada passo (`steps[]`) vira um estágio com o mesmo id. Sem `depends_on`, o passo depende do anterior
    (a sequência declarada continua valendo); `depends_on: []` torna o passo uma raiz; `depends_on` explícito
    vale como escrito. Contratos de entrada e saída ficam vazios: quem executa os declara em um `RunConfig`.
    """
    errors = _validator().validate(document, "agent-workflow")
    if errors:
        raise GraphError(INVALID_GRAPH, "fora do contrato agent-workflow: " + "; ".join(errors[:5]))
    specs: list[StageSpec] = []
    previous: str | None = None
    for step in document["steps"]:
        if "depends_on" in step:
            depends = tuple(step["depends_on"])
        else:
            depends = (previous,) if previous is not None else ()
        specs.append(StageSpec(step["id"], depends_on=depends))
        previous = step["id"]
    return StageGraph(specs)
