"""Retomada segura: decide, estágio a estágio, o que pode ser pulado e o que precisa rodar de novo.

`reconcile(estado, entradas)` recebe o estado gravado e o confronta com o mundo de agora (grafo, configuração, snapshot de
conhecimento, entradas, saídas em disco, checkpoints). Devolve o novo estado, já coerente, e o plano da retomada.
É a única função que decide "este estágio concluído ainda vale?". Ela só toca o estado e os checkpoints do próprio agente
(remove o checkpoint de um estágio interrompido ou fora do grafo); nunca toca fontes nem saídas.

Regras, aplicadas em ordem topológica (dependências primeiro):

1. Alinhamento com o grafo atual: estágio novo entra PENDING; estágio que saiu do grafo é descartado, com o checkpoint.
2. Interrupção: um estágio em RUNNING é um processo que sumiu. NUNCA se presume que terminou. Segue a política da
   configuração: REQUEUE (volta a PENDING e devolve a tentativa) ou FAIL (vira FAILED/INTERRUPTED e a tentativa conta). O
   checkpoint dele, se existir, é descartado: não há prova de que pertence a esta tentativa.
3. COMPLETED só é pulado (`SKIP`) se TODAS as condições valem: as dependências estão COMPLETED e válidas; o checkpoint existe
   e está íntegro; é o registrado no estado; e `check_checkpoint` não acha divergência (definição, snapshot, configuração,
   entradas, identidade das dependências, saídas no disco). Qualquer falha invalida o estágio (INVALIDATED, com a causa).
4. Propagação: um estágio invalidado invalida os dependentes concluídos (causa UPSTREAM_INVALIDATED, com o estágio de
   origem). Ramos que não dependem do invalidado ficam intactos.
5. FAILED: se as entradas mudaram desde a falha, ou a dependência está sendo refeita, a falha antiga não vale mais e a
   contagem de tentativas recomeça; senão, se a falha foi declarada NÃO repetível (`retryable=False`: determinística, as
   mesmas entradas dariam a mesma falha) o estágio fica FAILED na hora (`EXHAUSTED`, motivo `NON_RETRYABLE_FAILURE`), sem
   gastar outra tentativa; senão, com tentativas sobrando volta a PENDING (`RETRY`); sem tentativas fica FAILED
   (`EXHAUSTED`). Falha por mutação de fonte (SOURCE_MUTATION) não gasta tentativa: não foi culpa do estágio. Se uma
   dependência falhou de vez, o estágio fica BLOCKED.
6. PENDING, INVALIDATED e BLOCKED viram BLOCKED se alguma dependência está FAILED ou BLOCKED, ou voltam a PENDING se o
   bloqueio acabou.

Quem executa os estágios é o orquestrador; aqui só se decide.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from .checkpoint import CheckpointError, CheckpointStore, check_checkpoint, execution_identity
from .run_config import InterruptionPolicy, RunConfig
from .run_state import RunState, RunStatus
from .stage_graph import StageSpec
from .stage_state import (
    CHECKPOINT_ABSENT,
    INTERRUPTED,
    SOURCE_MUTATION,
    UPSTREAM_FAILED,
    UPSTREAM_INVALIDATED,
    Cause,
    FailureInfo,
    StageRecord,
    StageState,
)


class Decision(str, Enum):
    SKIP = "SKIP"            # concluído e checkpoint válido: não roda
    RUN = "RUN"              # precisa rodar (nunca rodou, foi invalidado ou foi desbloqueado)
    RETRY = "RETRY"          # falhou antes e ainda tem tentativas
    REQUEUE = "REQUEUE"      # estava em RUNNING quando o processo sumiu; volta à fila
    BLOCKED = "BLOCKED"      # uma dependência falhou de vez
    EXHAUSTED = "EXHAUSTED"  # falhou e não tem mais tentativas


@dataclass(frozen=True)
class StagePlan:
    stage_id: str
    decision: Decision
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class ResumePlan:
    stages: tuple[StagePlan, ...]

    def of(self, stage_id: str) -> StagePlan:
        for item in self.stages:
            if item.stage_id == stage_id:
                return item
        raise KeyError(stage_id)

    def with_decision(self, *decisions: Decision) -> tuple[str, ...]:
        return tuple(item.stage_id for item in self.stages if item.decision in decisions)

    @property
    def skipped(self) -> tuple[str, ...]:
        return self.with_decision(Decision.SKIP)

    @property
    def to_run(self) -> tuple[str, ...]:
        return self.with_decision(Decision.RUN, Decision.RETRY, Decision.REQUEUE)


@dataclass(frozen=True)
class ReconcileInputs:
    config: RunConfig
    snapshot_id: str | None
    input_digests: Mapping[str, "str | None"]
    parameter_digests: Mapping[str, str]
    output_root: "str | os.PathLike[str]"
    store: CheckpointStore


def _visible_inputs(spec: StageSpec, digests: Mapping[str, "str | None"]) -> dict:
    return {name: digests.get(name) for name in spec.input_contracts}


def _identity_now(spec: StageSpec, context: ReconcileInputs, upstream: Mapping[str, str]) -> str | None:
    inputs = _visible_inputs(spec, context.input_digests)
    if any(value is None for value in inputs.values()):
        return None
    return execution_identity(
        workflow_id=context.config.workflow_id,
        software_id=context.config.software_id,
        spec=spec,
        snapshot_id=context.snapshot_id,
        parameters_digest=context.parameter_digests[spec.stage_id],
        inputs=inputs,
        upstream={dep: upstream[dep] for dep in spec.depends_on},
    ).artifact_id


def reconcile(state: RunState, context: ReconcileInputs) -> tuple[RunState, ResumePlan]:
    """Aplica as regras do docstring do módulo. Devolve (estado coerente, plano). Não executa estágio algum."""
    config = context.config
    graph = config.graph
    records: dict[str, StageRecord] = {item.stage_id: item for item in state.stages}
    for gone in sorted(set(records) - set(graph.order)):
        context.store.delete(gone)
        del records[gone]
    for name in graph.order:
        records.setdefault(name, StageRecord(name))

    interrupted: set[str] = set()
    for name in graph.order:
        record = records[name]
        if record.state is not StageState.RUNNING:
            continue
        context.store.delete(name)
        if config.interruption_policy is InterruptionPolicy.REQUEUE:
            records[name] = record.transition(StageState.PENDING, recovery=True, refund_attempt=True)
            interrupted.add(name)
        else:
            failure = FailureInfo(
                name, INTERRUPTED, "o processo foi interrompido durante a tentativa; a tentativa conta", record.attempts, CHECKPOINT_ABSENT
            )
            records[name] = record.transition(StageState.FAILED, failure=failure)

    plans: dict[str, StagePlan] = {}
    upstream_digest: dict[str, str] = {}

    def origin_of(dep: str) -> str:
        cause = records[dep].cause
        return cause.stage_id if cause is not None and cause.stage_id is not None else dep

    for name in graph.order:
        spec = graph.get(name)
        record = records[name]
        not_done = [dep for dep in spec.depends_on if records[dep].state is not StageState.COMPLETED]
        failed_deps = [dep for dep in spec.depends_on if records[dep].state in (StageState.FAILED, StageState.BLOCKED)]

        if record.state is StageState.COMPLETED:
            cause: Cause | None = None
            if not_done:
                cause = Cause(UPSTREAM_INVALIDATED, origin_of(not_done[0]), not_done[0])
            else:
                cause = _check_completed(record, spec, context, upstream_digest)
            if cause is None:
                plans[name] = StagePlan(name, Decision.SKIP, "CHECKPOINT_VALID")
                continue
            record = records[name] = record.transition(StageState.INVALIDATED, cause=cause)

        if failed_deps:
            if record.state is StageState.FAILED:
                record = record.transition(StageState.BLOCKED, cause=Cause(UPSTREAM_FAILED, failed_deps[0]))
            elif record.state in (StageState.PENDING, StageState.INVALIDATED):
                record = record.transition(StageState.BLOCKED, cause=Cause(UPSTREAM_FAILED, failed_deps[0]))
            records[name] = record
            plans[name] = StagePlan(name, Decision.BLOCKED, UPSTREAM_FAILED, failed_deps[0])
            continue

        if record.state is StageState.BLOCKED:
            record = record.transition(StageState.PENDING)
            records[name] = record
            plans[name] = StagePlan(name, Decision.RUN, "UNBLOCKED")
            continue

        if record.state is StageState.FAILED:
            if not_done:
                record = record.transition(StageState.PENDING, reset_attempts=True)
                records[name] = record
                plans[name] = StagePlan(name, Decision.RUN, "UPSTREAM_REBUILDING", not_done[0])
                continue
            if record.failure is not None and record.failure.category == SOURCE_MUTATION:
                # Alguém mexeu na fonte durante o estágio: não foi falha do estágio, então não gasta tentativa. A violação
                # ficou registrada na execução; a nova execução recapta (READ_ONLY) ou confere (FROZEN) as fontes.
                records[name] = record.transition(StageState.PENDING, reset_attempts=True)
                plans[name] = StagePlan(name, Decision.RETRY, "SOURCE_MUTATION_NOT_COUNTED")
                continue
            identity = _identity_now(spec, context, upstream_digest)
            if record.last_identity != identity:
                records[name] = record.transition(StageState.PENDING, reset_attempts=True)
                plans[name] = StagePlan(name, Decision.RUN, "INPUTS_CHANGED_SINCE_FAILURE")
            elif record.failure is not None and not record.failure.retryable:
                # Falha determinística declarada pelo tratador: as mesmas entradas dariam a mesma falha. Não gasta outra tentativa.
                plans[name] = StagePlan(name, Decision.EXHAUSTED, "NON_RETRYABLE_FAILURE")
            elif record.attempts < spec.retry.max_attempts:
                records[name] = record.transition(StageState.PENDING)
                plans[name] = StagePlan(name, Decision.RETRY, "ATTEMPTS_REMAIN", str(spec.retry.max_attempts - record.attempts))
            else:
                plans[name] = StagePlan(name, Decision.EXHAUSTED, "ATTEMPTS_EXHAUSTED")
            continue

        if record.state is StageState.INVALIDATED:
            plans[name] = StagePlan(name, Decision.RUN, record.cause.code, record.cause.detail or record.cause.stage_id)
        elif name in interrupted:
            plans[name] = StagePlan(name, Decision.REQUEUE, "INTERRUPTED")
        else:
            plans[name] = StagePlan(name, Decision.RUN, "NOT_RUN_YET")

    # O estado devolvido é o de uma execução que vai (re)começar: RUNNING, sem estágio em curso. O orquestrador
    # fecha o status quando termina; um estado COMPLETED com estágio invalidado violaria os invariantes de RunState.
    rebuilt = state.with_changes(
        stages=tuple(records[name] for name in graph.order),
        current_stage=None,
        status=RunStatus.RUNNING,
    )
    return rebuilt, ResumePlan(tuple(plans[name] for name in graph.order))


def _check_completed(
    record: StageRecord, spec: StageSpec, context: ReconcileInputs, upstream_digest: dict
) -> "Cause | None":
    """Causa da invalidação de um estágio COMPLETED cujas dependências estão válidas, ou `None` se o checkpoint vale."""
    try:
        checkpoint = context.store.load(spec.stage_id)
    except CheckpointError:
        return Cause("CHECKPOINT_CORRUPT", spec.stage_id)
    if checkpoint is None or checkpoint.checkpoint_id != record.checkpoint_id:
        return Cause("CHECKPOINT_MISSING", spec.stage_id)
    mismatches = check_checkpoint(
        checkpoint,
        workflow_id=context.config.workflow_id,
        software_id=context.config.software_id,
        spec=spec,
        snapshot_id=context.snapshot_id,
        parameters_digest=context.parameter_digests[spec.stage_id],
        inputs=_visible_inputs(spec, context.input_digests),
        upstream={dep: upstream_digest[dep] for dep in spec.depends_on},
        output_root=Path(context.output_root),
    )
    if mismatches:
        first = mismatches[0]
        return Cause(first.code, spec.stage_id, first.detail)
    upstream_digest[spec.stage_id] = checkpoint.outputs_digest
    return None
