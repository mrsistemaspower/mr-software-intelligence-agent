"""Orquestrador determinístico: executa um grafo de estágios, grava o andamento e retoma com segurança.

O orquestrador sabe O QUE deve rodar (grafo), QUANDO pode rodar (dependências concluídas), O QUE já concluiu (estado
e checkpoints) e SE a conclusão ainda vale (`core.resume.reconcile`). Ele NÃO chama modelo de linguagem, não analisa imagem,
não consulta banco e não pergunta a pessoas: cada estágio é um tratador (`handler`) fornecido por quem executa, que pode ser
de teste ou sintético. Nada aqui lê relógio, ambiente ou rede; `now` e as sondas de ambiente são injetados.

Ciclo de `run()` (iniciar ou retomar):

1. valida a configuração e os tratadores (um por estágio) ANTES de executar qualquer coisa;
2. carrega o estado (nunca substitui em silêncio um estado ilegível) e abre a proteção das raízes de fonte;
3. `reconcile`: pula o que vale, invalida o que mudou e os dependentes, recupera estágios interrompidos;
4. executa, em ordem topológica, os estágios prontos. Para cada um: grava RUNNING, roda o tratador, confere que as saídas
   declaradas (e só elas) existem, que as entradas e as fontes protegidas não mudaram, grava o checkpoint e só então COMPLETED;
5. falha de estágio obrigatório interrompe a execução (sem continuar em silêncio). Falha de estágio opcional bloqueia só os
   seus dependentes. Mutação inesperada de fonte protegida marca a execução como UNSAFE; nada é revertido;
6. fecha o status e grava.

Cada gravação de estado e de checkpoint é atômica. O checkpoint é gravado ANTES de o estado dizer COMPLETED: uma queda entre os
dois deixa o estágio em RUNNING no disco, e a retomada o refaz (não presume sucesso). Um estágio em RUNNING que o processo
deixou para trás (queda, `KeyboardInterrupt`, qualquer `BaseException`) é recuperado pela política da configuração.

Limites: um único escritor por diretório de estado (não há trava entre processos); a proteção de fontes detecta, não impede
escrita feita pelo próprio tratador; o tratador roda no processo do orquestrador (sem isolamento nem tempo limite).
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .atomic import atomic_write_bytes
from .checkpoint import (
    CheckpointError,
    CheckpointStore,
    OutputRecord,
    StageCheckpoint,
    execution_identity,
    output_logical_path,
    parameters_digest,
)
from .hashing import Digest, hash_bytes, hash_file
from .paths import PathPortabilityError, to_fs_path
from .protection import POINT_END, POINT_STAGE, ProtectionError, ProtectionGuard, Violation
from .resume import ReconcileInputs, ResumePlan, reconcile
from .run_config import BASELINE_DIR, CHECKPOINT_DIR, RUN_STATE_FILE, ConfigError, RunConfig
from .run_state import (
    MAX_VIOLATIONS,
    RunState,
    RunStatus,
    compute_input_identity,
    compute_run_id,
    load_run_state,
    save_run_state,
)
from .serialization import canonical_pretty_bytes
from .snapshot import KnowledgeSnapshot
from .stage_graph import StageSpec
from .stage_state import (
    CHECKPOINT_ABSENT,
    HANDLER_ERROR,
    INPUT_MUTATED,
    INPUT_UNAVAILABLE,
    OUTPUT_CONTRACT,
    SOURCE_MUTATION,
    UPSTREAM_FAILED,
    Cause,
    FailureInfo,
    StageRecord,
    StageState,
    failure_from_exception,
    sanitize_message,
)

UNAVAILABLE = "unavailable"


class StageFailure(Exception):
    """Falha controlada de um estágio. Um tratador pode levantá-la para escolher a categoria (padrão: HANDLER_ERROR)."""

    category = HANDLER_ERROR

    def __init__(self, message: str, category: str | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class InputMutatedError(StageFailure):
    category = INPUT_MUTATED


class OutputContractError(StageFailure):
    category = OUTPUT_CONTRACT


class RunMismatchError(RuntimeError):
    """O estado gravado pertence a outra execução (fluxo, software, versão ou chave diferentes). Nada foi alterado."""


class EnvironmentProbe(Protocol):
    """Capacidade opcional do ambiente (por exemplo, estado do Git). O núcleo não depende de nenhuma."""

    def capture(self) -> dict: ...

    def compare(self, before: dict, after: dict) -> list: ...


StageHandler = Callable[["StageContext"], None]


class StageContext:
    """O que um tratador pode ver e fazer. Só nomes declarados no estágio; escrita só na raiz de saídas."""

    def __init__(self, runner: "Orchestrator", spec: StageSpec, attempt: int, inputs: Mapping[str, str]) -> None:
        self._runner = runner
        self._spec = spec
        self._attempt = attempt
        self._inputs = dict(inputs)
        self._written: dict[str, Digest] = {}
        self._upstream_records: dict[str, StageCheckpoint] = {}

    @property
    def stage(self) -> StageSpec:
        return self._spec

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def parameters(self) -> dict:
        """Cópia dos parâmetros do estágio (alterá-la não afeta a configuração)."""
        return copy.deepcopy(self._runner.config.parameters_of(self._spec.stage_id))

    def input_path(self, name: str) -> Path:
        if name not in self._spec.input_contracts:
            raise StageFailure(f"entrada não declarada pelo estágio: {name}")
        binding = self._runner.config.inputs[name]
        return self._runner.guard.read_path(binding.root, binding.path)

    def read_input(self, name: str) -> bytes:
        """Bytes da entrada. Se não forem os que foram identificados na partida do estágio, a leitura falha."""
        path = self.input_path(name)
        try:
            with open(to_fs_path(path), "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise InputMutatedError(f"a entrada {name} deixou de ser legível ({type(exc).__name__})") from exc
        if str(hash_bytes(data)) != self._inputs[name]:
            raise InputMutatedError(f"a entrada {name} mudou durante o estágio")
        return data

    def read_upstream(self, stage_id: str, name: str) -> bytes:
        """Bytes de uma saída de dependência direta, conferidos contra o checkpoint dela."""
        if stage_id not in self._spec.depends_on:
            raise StageFailure(f"{stage_id} não é dependência do estágio {self._spec.stage_id}")
        if name not in self._runner.config.graph.get(stage_id).output_contracts:
            raise StageFailure(f"{stage_id} não declara a saída {name}")
        record = self._upstream_records.get(stage_id)
        if record is None:
            try:
                record = self._runner.store.load(stage_id)
            except CheckpointError as exc:
                raise InputMutatedError(f"o checkpoint de {stage_id} está ilegível") from exc
            if record is None:
                raise InputMutatedError(f"o checkpoint de {stage_id} não existe")
            self._upstream_records[stage_id] = record
        expected = next(item for item in record.outputs if item.name == name)
        path = self._runner.guard.read_path(self._runner.config.output_root, output_logical_path(stage_id, name))
        try:
            with open(to_fs_path(path), "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise InputMutatedError(f"a saída {name} de {stage_id} deixou de ser legível ({type(exc).__name__})") from exc
        if hash_bytes(data).digest != expected.sha256:
            raise InputMutatedError(f"a saída {name} de {stage_id} mudou depois do checkpoint")
        return data

    def write_output(self, name: str, data: bytes) -> Digest:
        """Grava uma saída declarada, de forma atômica, na raiz de saídas. Escrever fora das declaradas é recusado."""
        if name not in self._spec.output_contracts:
            raise OutputContractError(f"saída não declarada pelo estágio {self._spec.stage_id}: {name}")
        if not isinstance(data, (bytes, bytearray)):
            raise OutputContractError("write_output exige bytes")
        target = self._runner.guard.output_path(self._runner.config.output_root, output_logical_path(self._spec.stage_id, name))
        digest = atomic_write_bytes(target, bytes(data), skip_unchanged=False).digest
        self._written[name] = digest
        return digest

    def write_output_json(self, name: str, value: object) -> Digest:
        """Grava `value` em JSON canônico legível (ver `core.serialization`)."""
        return self.write_output(name, canonical_pretty_bytes(value))

    @property
    def written(self) -> frozenset:
        return frozenset(self._written)


@dataclass(frozen=True)
class RunResult:
    state: RunState
    plan: ResumePlan
    executed: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]
    violations: tuple[Violation, ...] = ()

    @property
    def status(self) -> RunStatus:
        return self.state.status


def _cap(violations: tuple[Violation, ...]) -> tuple[tuple[Violation, ...], int]:
    return tuple(violations[:MAX_VIOLATIONS]), len(violations)


class Orchestrator:
    def __init__(
        self,
        config: RunConfig,
        handlers: Mapping[str, StageHandler],
        roots: Mapping[str, "str | os.PathLike[str]"],
        *,
        snapshot: KnowledgeSnapshot | None = None,
        probes: Mapping[str, EnvironmentProbe] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(config, RunConfig):
            raise ConfigError("config deve ser RunConfig")
        graph_names = set(config.graph.order)
        missing = sorted(graph_names - set(handlers))
        extra = sorted(set(handlers) - graph_names)
        if missing or extra:
            raise ConfigError(f"tratadores não conferem com o grafo (sem tratador: {missing}; tratador sem estágio: {extra})")
        if not all(callable(handler) for handler in handlers.values()):
            raise ConfigError("todo tratador deve ser chamável")
        if snapshot is not None:
            if not isinstance(snapshot, KnowledgeSnapshot):
                raise ConfigError("snapshot deve ser KnowledgeSnapshot")
            if config.software_id is not None and snapshot.software_id is not None and snapshot.software_id != config.software_id:
                raise ConfigError("o snapshot é de outro software: um software por vez")
        if now is not None and not callable(now):
            raise ConfigError("now deve ser chamável")
        self.config = config
        self._handlers = dict(handlers)
        self._snapshot = snapshot
        self._probes = dict(probes or {})
        self._now = now
        try:
            self.guard = ProtectionGuard(
                config.roots, roots, baseline_dir=self._state_path(roots, BASELINE_DIR), software_id=config.software_id
            )
        except ProtectionError as exc:
            raise ConfigError(str(exc)) from exc
        self._state_file = self.guard.output_path(config.state_root, RUN_STATE_FILE)
        self.store = CheckpointStore(self.guard.output_path(config.state_root, CHECKPOINT_DIR))
        self._output_local = self.guard.local_root(config.output_root)
        self._parameter_digests = {name: parameters_digest(config.parameters_of(name)) for name in config.graph.order}
        self.run_id = compute_run_id(config.workflow_id, config.software_id, config.workflow_version, config.run_key)

    def _state_path(self, roots: Mapping[str, "str | os.PathLike[str]"], logical: str) -> Path:
        base = Path(roots[self.config.state_root]).resolve() if self.config.state_root in roots else Path(".")
        return base.joinpath(*logical.split("/"))

    # ------------------------------------------------------------------ entradas
    def _hash_input(self, name: str) -> str | None:
        binding = self.config.inputs[name]
        try:
            return str(hash_file(self.guard.read_path(binding.root, binding.path)))
        except (OSError, PathPortabilityError):
            return None

    def _hash_inputs(self) -> dict:
        return {name: self._hash_input(name) for name in self.config.inputs}

    @property
    def snapshot_id(self) -> str | None:
        return self._snapshot.snapshot_id if self._snapshot is not None else None

    # ------------------------------------------------------------------ estado
    def _timestamp(self) -> str | None:
        return self._now() if self._now is not None else None

    def _new_state(self, inputs: Mapping[str, "str | None"]) -> RunState:
        return RunState(
            run_id=self.run_id,
            workflow_id=self.config.workflow_id,
            workflow_version=self.config.workflow_version,
            run_key=self.config.run_key,
            graph_digest=str(self.config.graph.digest()),
            config_digest=str(self.config.digest()),
            input_identity=self._input_identity(inputs),
            status=RunStatus.PENDING,
            stages=tuple(StageRecord(name) for name in self.config.graph.order),
            software_id=self.config.software_id,
            knowledge_snapshot_id=self.snapshot_id,
            started_at=self._timestamp(),
        )

    def _input_identity(self, inputs: Mapping[str, "str | None"]) -> str:
        return compute_input_identity(
            str(self.config.graph.digest()),
            str(self.config.digest()),
            self.snapshot_id,
            {name: (value if value is not None else UNAVAILABLE) for name, value in inputs.items()},
        )

    def load_state(self) -> RunState | None:
        """Estado gravado, ou `None` se ainda não há. Estado ilegível levanta `RunStateError`; nunca é descartado em silêncio."""
        try:
            state = load_run_state(self._state_file)
        except FileNotFoundError:
            return None
        if state.run_id != self.run_id:
            raise RunMismatchError("o estado gravado pertence a outra execução (fluxo, software, versão ou run_key diferentes)")
        return state

    def _save(self, state: RunState) -> None:
        save_run_state(self._state_file, state)

    # ------------------------------------------------------------------ API
    def start(self) -> RunResult:
        """Começa uma execução nova. Recusa se já existe estado: retomar é `resume()`."""
        if self.load_state() is not None:
            raise RunMismatchError("já existe estado para esta execução; use resume() ou run()")
        return self.run()

    def resume(self) -> RunResult:
        """Retoma uma execução existente. Sem estado gravado, falha em vez de começar do zero em silêncio."""
        if self.load_state() is None:
            raise FileNotFoundError("não há estado gravado para retomar; use start() ou run()")
        return self.run()

    def run(self) -> RunResult:
        """Inicia ou retoma: se há estado, retoma; senão, começa."""
        inputs = self._hash_inputs()
        existing = self.load_state()
        state = existing if existing is not None else self._new_state(inputs)
        protection, begin_violations = self.guard.begin()
        environment = self._probe_start()

        context = ReconcileInputs(
            config=self.config,
            snapshot_id=self.snapshot_id,
            input_digests=inputs,
            parameter_digests=self._parameter_digests,
            output_root=self._output_local,
            store=self.store,
        )
        state = state.with_changes(
            graph_digest=str(self.config.graph.digest()),
            config_digest=str(self.config.digest()),
            knowledge_snapshot_id=self.snapshot_id,
            input_identity=self._input_identity(inputs),
            resume_count=state.resume_count + (1 if existing is not None else 0),
            protection=protection,
            violations=(),
            violation_total=0,
            environment=environment,
            status=RunStatus.RUNNING,
        )
        if begin_violations:
            # Fonte congelada alterada: nada roda e nada é reconciliado. Se a pessoa restaurar a evidência, a próxima
            # execução encontra o estado como estava e pula o que vale.
            return self._finish_unsafe(state, ResumePlan(()), begin_violations, (), ())
        state, plan = reconcile(state, context)
        self._save(state)

        executed: list[str] = []
        failed: list[str] = []
        unsafe: tuple[Violation, ...] = ()
        for name in self.config.graph.order:
            spec = self.config.graph.get(name)
            record = state.stage(name)
            if record.state not in (StageState.PENDING, StageState.INVALIDATED):
                continue
            if any(state.stage(dep).state is not StageState.COMPLETED for dep in spec.depends_on):
                continue
            state, outcome, found = self._run_stage(state, spec)
            if outcome == "OK":
                executed.append(name)
                continue
            failed.append(name)
            if found:
                unsafe = found
                break
            if spec.optional:
                state = self._block_descendants(state, spec)
                continue
            break

        if unsafe:
            return self._finish_unsafe(state, plan, unsafe, tuple(executed), tuple(failed))
        end_violations = self.guard.verify(POINT_END)
        if end_violations:
            return self._finish_unsafe(state, plan, end_violations, tuple(executed), tuple(failed))
        state = state.with_changes(status=self._final_status(state), environment=self._probe_end(state))
        self._save(state)
        return RunResult(state, plan, tuple(executed), plan.skipped, tuple(failed))

    # ------------------------------------------------------------------ execução de um estágio
    def _upstream_digests(self, spec: StageSpec) -> dict | None:
        found: dict[str, str] = {}
        for dep in spec.depends_on:
            try:
                checkpoint = self.store.load(dep)
            except CheckpointError:
                return None
            if checkpoint is None:
                return None
            found[dep] = checkpoint.outputs_digest
        return found

    def _run_stage(self, state: RunState, spec: StageSpec) -> "tuple[RunState, str, tuple[Violation, ...]]":
        name = spec.stage_id
        inputs = {input_name: self._hash_input(input_name) for input_name in spec.input_contracts}
        unavailable = sorted(key for key, value in inputs.items() if value is None)
        upstream = self._upstream_digests(spec)
        identity: str | None = None
        if not unavailable and upstream is not None:
            identity = execution_identity(
                workflow_id=self.config.workflow_id,
                software_id=self.config.software_id,
                spec=spec,
                snapshot_id=self.snapshot_id,
                parameters_digest=self._parameter_digests[name],
                inputs=inputs,
                upstream=upstream,
            ).artifact_id
        self.store.delete(name)
        state = state.with_stage(state.stage(name).transition(StageState.RUNNING, last_identity=identity))
        self._save(state)
        attempt = state.stage(name).attempts

        if unavailable:
            return self._fail(state, spec, INPUT_UNAVAILABLE, "entradas indisponíveis: " + ", ".join(unavailable))
        if upstream is None:
            return self._fail(state, spec, INPUT_UNAVAILABLE, "o checkpoint de uma dependência está ausente ou ilegível")

        context = StageContext(self, spec, attempt, inputs)
        try:
            self._handlers[name](context)
        except StageFailure as exc:
            return self._fail(state, spec, exc.category, str(exc) or type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - qualquer erro do tratador vira falha registrada; BaseException propaga
            failure = failure_from_exception(name, exc, attempt)
            return self._fail(state, spec, failure.category, failure.message)

        found = self.guard.verify(POINT_STAGE)
        if found:
            state, _, _ = self._fail(state, spec, SOURCE_MUTATION, f"{len(found)} violação(ões) em fonte protegida durante o estágio")
            return self._mark_unsafe(state, found), "FAILED", found
        changed = sorted(key for key in inputs if self._hash_input(key) != inputs[key])
        if changed:
            return self._fail(state, spec, INPUT_MUTATED, "entradas mudaram durante o estágio: " + ", ".join(changed))
        declared = set(spec.output_contracts)
        if context.written != declared:
            missing = sorted(declared - context.written)
            return self._fail(state, spec, OUTPUT_CONTRACT, "saídas declaradas não produzidas: " + (", ".join(missing) or "(nenhuma)"))

        try:
            outputs = [
                OutputRecord.of(name, out, hash_file(self.guard.read_path(self.config.output_root, output_logical_path(name, out))))
                for out in sorted(declared)
            ]
            checkpoint = StageCheckpoint.create(
                workflow_id=self.config.workflow_id,
                software_id=self.config.software_id,
                spec=spec,
                snapshot_id=self.snapshot_id,
                parameters_digest=self._parameter_digests[name],
                inputs={key: value for key, value in inputs.items()},
                upstream=upstream,
                outputs=outputs,
                attempt=attempt,
            )
        except (OSError, CheckpointError, PathPortabilityError) as exc:
            return self._fail(state, spec, OUTPUT_CONTRACT, f"não foi possível selar as saídas: {type(exc).__name__}")
        self.store.save(checkpoint)
        record = state.stage(name).transition(
            StageState.COMPLETED, checkpoint_id=checkpoint.checkpoint_id, last_identity=checkpoint.execution_identity
        )
        state = state.with_stage(record)
        self._save(state)
        return state, "OK", ()

    def _fail(self, state: RunState, spec: StageSpec, category: str, message: str) -> "tuple[RunState, str, tuple[Violation, ...]]":
        record = state.stage(spec.stage_id)
        failure = FailureInfo(spec.stage_id, category, sanitize_message(message), record.attempts, CHECKPOINT_ABSENT)
        state = state.with_stage(record.transition(StageState.FAILED, failure=failure))
        self._save(state)
        return state, "FAILED", ()

    def _block_descendants(self, state: RunState, spec: StageSpec) -> RunState:
        for name in self.config.graph.descendants(spec.stage_id):
            record = state.stage(name)
            if record.state in (StageState.PENDING, StageState.INVALIDATED):
                state = state.with_stage(record.transition(StageState.BLOCKED, cause=Cause(UPSTREAM_FAILED, spec.stage_id)))
        self._save(state)
        return state

    # ------------------------------------------------------------------ fechamento
    def _mark_unsafe(self, state: RunState, violations: tuple[Violation, ...]) -> RunState:
        capped, total = _cap(violations)
        return state.with_changes(status=RunStatus.UNSAFE, violations=capped, violation_total=total)

    def _finish_unsafe(
        self, state: RunState, plan: ResumePlan, violations: tuple[Violation, ...], executed: tuple, failed: tuple
    ) -> RunResult:
        state = self._mark_unsafe(state, violations)
        self._save(state)
        return RunResult(state, plan, executed, plan.skipped, failed, tuple(violations))

    def _final_status(self, state: RunState) -> RunStatus:
        specs = {name: self.config.graph.get(name) for name in self.config.graph.order}
        required_failed = any(
            record.state is StageState.FAILED and not specs[record.stage_id].optional for record in state.stages
        )
        if required_failed:
            return RunStatus.FAILED
        for record in state.stages:
            if record.state is StageState.COMPLETED:
                continue
            if specs[record.stage_id].optional and record.state in (StageState.FAILED, StageState.BLOCKED):
                continue
            return RunStatus.FAILED if record.state in (StageState.FAILED, StageState.BLOCKED) else RunStatus.RUNNING
        return RunStatus.COMPLETED

    # ------------------------------------------------------------------ sondas de ambiente
    def _probe_start(self) -> dict | None:
        if not self._probes:
            return None
        result: dict = {}
        for name in sorted(self._probes):
            try:
                result[name] = {"at_start": self._probes[name].capture()}
            except Exception as exc:  # noqa: BLE001 - capacidade opcional nunca derruba a execução
                result[name] = {"probe_error": type(exc).__name__}
        return result

    def _probe_end(self, state: RunState) -> dict | None:
        if not self._probes or state.environment is None:
            return None
        result = {name: dict(value) for name, value in state.environment.items()}
        for name in sorted(self._probes):
            entry = result.get(name, {})
            if "at_start" not in entry:
                continue
            try:
                end = self._probes[name].capture()
                entry["at_end"] = end
                entry["drift"] = sorted(self._probes[name].compare(entry["at_start"], end))
            except Exception as exc:  # noqa: BLE001
                entry["probe_error"] = type(exc).__name__
            result[name] = entry
        return result
