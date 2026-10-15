"""Apoio dos testes de orquestração: um construtor de fluxos sintéticos, genérico e sem nenhum software real.

`Scenario` monta, num diretório temporário, uma raiz de fonte (`src`) e uma raiz de saída/estado (`work`), uma configuração
e tratadores determinísticos: cada estágio lê as entradas declaradas e as saídas das dependências e escreve saídas que
dependem de tudo isso. Assim, mudar uma entrada muda as saídas de quem depende dela e só delas.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterable

from core.orchestrator import Orchestrator, RunResult
from core.protection import Protection, RootPolicy
from core.run_config import InputBinding, InterruptionPolicy, RunConfig
from core.snapshot import KnowledgeSnapshot
from core.stage_graph import RetryPolicy, StageGraph, StageSpec

SRC = "src"
WORK = "work"


def spec(stage_id: str, deps: Iterable[str] = (), inputs: Iterable[str] = (), outputs: Iterable[str] = ("result",), **extra: object) -> StageSpec:
    return StageSpec(stage_id, depends_on=tuple(deps), input_contracts=tuple(inputs), output_contracts=tuple(outputs), **extra)


def retrying(stage_id: str, attempts: int, **kwargs: object) -> StageSpec:
    return spec(stage_id, retry=RetryPolicy(attempts), **kwargs)


class Scenario:
    def __init__(
        self,
        tmp: Path,
        specs: Iterable[StageSpec],
        input_files: dict[str, str] | None = None,
        *,
        tag: str = "ws",
        software_id: str | None = "synthetic-one",
        workflow_id: str = "synthetic-flow",
        workflow_version: str = "1",
        run_key: str = "default",
        params: dict | None = None,
        src_protection: Protection = Protection.READ_ONLY,
        src_verify: str | None = None,
        interruption: InterruptionPolicy = InterruptionPolicy.REQUEUE,
        handlers: dict[str, Callable] | None = None,
        snapshot: KnowledgeSnapshot | None = None,
    ) -> None:
        self.base = Path(tmp) / tag
        self.src = self.base / SRC
        self.work = self.base / WORK
        # exist_ok: um segundo Scenario com a mesma `tag` reaproveita o espaço de trabalho (muda a definição, mantém o estado).
        self.src.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []
        self.graph = StageGraph(list(specs))
        self.software_id = software_id
        self.workflow_id = workflow_id
        self.workflow_version = workflow_version
        self.run_key = run_key
        self.params = params or {}
        self.src_protection = src_protection
        self.src_verify = src_verify
        self.interruption = interruption
        self.custom = dict(handlers or {})
        self.snapshot = snapshot
        for name, text in (input_files or {}).items():
            self.write_input(name, text)

    # ------------------------------------------------------------------ arquivos
    def write_input(self, name: str, text: str) -> None:
        (self.src / f"{name}.txt").write_text(text, encoding="utf-8", newline="")

    def read_input(self, name: str) -> str:
        return (self.src / f"{name}.txt").read_text(encoding="utf-8")

    def output_file(self, stage_id: str, name: str = "result") -> Path:
        return self.work / "outputs" / stage_id / name

    @property
    def state_file(self) -> Path:
        return self.work / "state" / "run-state.json"

    def checkpoint_file(self, stage_id: str) -> Path:
        return self.work / "state" / "checkpoints" / f"{stage_id}.json"

    # ------------------------------------------------------------------ montagem
    def config(self) -> RunConfig:
        declared = sorted({name for item in self.graph.stages for name in item.input_contracts})
        return RunConfig(
            workflow_id=self.workflow_id,
            workflow_version=self.workflow_version,
            software_id=self.software_id,
            run_key=self.run_key,
            graph=self.graph,
            roots=(
                RootPolicy(SRC, self.src_protection, self.src_verify),
                RootPolicy(WORK, Protection.MUTABLE_OUTPUT),
            ),
            state_root=WORK,
            output_root=WORK,
            inputs={name: InputBinding(SRC, f"{name}.txt") for name in declared},
            stage_parameters=self.params,
            interruption_policy=self.interruption,
        )

    def default_handler(self, item: StageSpec) -> Callable:
        def handler(ctx) -> None:
            payload = {
                "stage": item.stage_id,
                "parameters": ctx.parameters,
                "inputs": {name: ctx.read_input(name).decode("utf-8") for name in item.input_contracts},
                "upstream": {},
            }
            for dep in item.depends_on:
                for out in self.graph.get(dep).output_contracts:
                    payload["upstream"][f"{dep}/{out}"] = hashlib.sha256(ctx.read_upstream(dep, out)).hexdigest()
            for out in item.output_contracts:
                ctx.write_output_json(out, payload)

        return handler

    def handlers(self) -> dict[str, Callable]:
        table = {}
        for item in self.graph.stages:
            inner = self.custom.get(item.stage_id)
            base = self.default_handler(item)

            def make(stage_id: str, custom: Callable | None, default: Callable) -> Callable:
                def handler(ctx) -> None:
                    self.calls.append(stage_id)
                    if custom is None:
                        default(ctx)
                    else:
                        custom(ctx, default)

                return handler

            table[item.stage_id] = make(item.stage_id, inner, base)
        return table

    def orchestrator(self, **overrides: object) -> Orchestrator:
        options: dict = {"snapshot": self.snapshot}
        options.update(overrides)
        return Orchestrator(
            self.config(), self.handlers(), {SRC: self.src, WORK: self.work}, **options  # type: ignore[arg-type]
        )

    def run(self, **overrides: object) -> RunResult:
        self.calls.clear()
        return self.orchestrator(**overrides).run()

    def states(self, result: RunResult) -> dict[str, str]:
        return {record.stage_id: record.state.value for record in result.state.stages}


def linear_specs() -> list[StageSpec]:
    """inventory -> discover -> analyze -> validate -> document."""
    return [
        spec("inventory", inputs=["inventory-source"]),
        spec("discover", deps=["inventory"]),
        spec("analyze", deps=["discover"], inputs=["analysis-input"]),
        spec("validate", deps=["analyze"]),
        spec("document", deps=["validate"]),
    ]


LINEAR_INPUTS = {"inventory-source": "item-1\nitem-2\n", "analysis-input": "rules v1\n"}


def branched_specs() -> list[StageSpec]:
    """inventory -> ui-analysis -> documentation ; inventory -> database-analysis."""
    return [
        spec("inventory", inputs=["inventory-source"]),
        spec("ui-analysis", deps=["inventory"], inputs=["ui-input"]),
        spec("documentation", deps=["ui-analysis"]),
        spec("database-analysis", deps=["inventory"], inputs=["db-input"]),
    ]


BRANCHED_INPUTS = {"inventory-source": "base\n", "ui-input": "ui v1\n", "db-input": "db v1\n"}


def diamond_specs() -> list[StageSpec]:
    """a -> b -> c ; a -> d (o exemplo da especificação de propagação)."""
    return [spec("a", inputs=["seed"]), spec("b", deps=["a"]), spec("c", deps=["b"]), spec("d", deps=["a"])]


DIAMOND_INPUTS = {"seed": "seed v1\n"}
