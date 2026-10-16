"""Base opcional para adaptadores de modelo: normaliza o que um adaptador devolve, sem conhecer nenhum provedor.

O contrato do agente é só `descriptor` e `execute(task) -> ModelResult` (ver `core.model_adapter.ModelAdapter`). Esta base
existe para que cada adaptador futuro (de um provedor, de um modelo local) escreva apenas `_invoke` e herde:

- a identidade (`descriptor`) validada na construção;
- a conversão do que `_invoke` devolve em `ModelResult` (um mapeamento vira saída; um `ModelFailure` vira falha);
- a tradução de tempo esgotado e de indisponibilidade de rede em falhas técnicas repetíveis, SEM copiar a mensagem da exceção
  (ela pode citar caminho, endereço ou credencial).

O núcleo não depende desta base: um adaptador que cumpra o contrato por conta própria é igualmente válido, e o executor
valida o que qualquer adaptador devolve. Exceções inesperadas NÃO são engolidas aqui; o executor as registra.
"""
from __future__ import annotations

import abc
from typing import Mapping

from core.model_adapter import AdapterDescriptor, ModelFailure, ModelResult
from core.model_task import ModelTask


class BaseModelAdapter(abc.ABC):
    def __init__(self, descriptor: AdapterDescriptor) -> None:
        if not isinstance(descriptor, AdapterDescriptor):
            raise TypeError("descriptor deve ser AdapterDescriptor")
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @abc.abstractmethod
    def _invoke(self, task: ModelTask) -> "Mapping | ModelFailure | ModelResult":
        """Envia a tarefa ao modelo e devolve a saída estruturada (mapeamento), uma falha ou um resultado pronto."""

    def success(self, task: ModelTask, output: Mapping, usage: "dict | None" = None) -> ModelResult:
        return ModelResult.success(task.task_id, self._descriptor.model_identity(), dict(output), usage)

    def fail(self, task: ModelTask, kind: str, message: str, retryable: bool = False) -> ModelResult:
        return ModelResult.failed(task.task_id, self._descriptor.model_identity(), ModelFailure(kind, message, retryable))

    def _wrap(self, task: ModelTask, produced: object) -> object:
        if isinstance(produced, ModelResult):
            return produced
        if isinstance(produced, ModelFailure):
            return ModelResult.failed(task.task_id, self._descriptor.model_identity(), produced)
        if isinstance(produced, Mapping):
            return self.success(task, produced)
        raise TypeError("_invoke deve devolver Mapping, ModelFailure ou ModelResult")

    def execute(self, task: ModelTask) -> ModelResult:
        if not isinstance(task, ModelTask):
            raise TypeError("execute exige ModelTask")
        try:
            produced = self._invoke(task)
        except TimeoutError:
            return self.fail(task, "TIMEOUT", "tempo esgotado ao executar a tarefa", retryable=True)
        except ConnectionError:
            return self.fail(task, "UNAVAILABLE", "o modelo não está acessível", retryable=True)
        return self._wrap(task, produced)  # type: ignore[return-value]
