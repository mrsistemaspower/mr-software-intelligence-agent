"""Adaptadores sintéticos e determinísticos, para provar o contrato sem modelo, sem rede e sem credencial.

`ScriptedModelAdapter` responde conforme um roteiro, na ordem das chamadas. Cada passo do roteiro é:

- um mapeamento: a saída estruturada do modelo (resposta boa, ou malformada de propósito);
- um `ModelFailure`: falha técnica devolvida pelo adaptador;
- uma exceção: levantada de dentro do adaptador (`TimeoutError` e `ConnectionError` viram falha repetível pela base;
  qualquer outra propaga e o executor a registra como `ADAPTER_EXCEPTION`);
- `RawReturn(valor)`: devolve `valor` CRU, sem passar pelo contrato (simula um adaptador que viola o contrato);
- um chamável `f(task)`: devolve qualquer um dos itens acima a partir da tarefa recebida.

`RuleBasedModelAdapter` deriva a resposta da própria tarefa por uma função pura, o que permite executar o MESMO pedido por dois
adaptadores de identidades diferentes e comparar os resultados.

Ambos guardam as tarefas recebidas em `tasks` (a evidência de que o adaptador foi, ou não, chamado). Nenhum importa SDK nem abre rede.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping

from core.model_adapter import AdapterDescriptor, ModelFailure
from core.model_task import ModelTask

from .base import BaseModelAdapter


class RawReturn:
    """Devolve `value` exatamente como está, sem validação, para simular um adaptador fora do contrato."""

    def __init__(self, value: object) -> None:
        self.value = value


def _descriptor(**fields: object) -> AdapterDescriptor:
    return AdapterDescriptor(**fields)  # type: ignore[arg-type]


class _SyntheticAdapter(BaseModelAdapter):
    def __init__(
        self,
        *,
        adapter_id: str,
        adapter_version: str,
        model_id: "str | None",
        capabilities: Iterable[str],
        data_boundary: str,
        deterministic: bool,
        profile_id: "str | None",
        config_fingerprint: "str | None",
        usage: "dict | None",
    ) -> None:
        super().__init__(_descriptor(
            adapter_id=adapter_id, adapter_version=adapter_version, capabilities=tuple(capabilities), data_boundary=data_boundary,
            deterministic=deterministic, model_id=model_id, config_fingerprint=config_fingerprint, profile_id=profile_id,
        ))
        self.tasks: list[ModelTask] = []
        self._usage = usage

    @property
    def calls(self) -> int:
        return len(self.tasks)

    def _wrap(self, task: ModelTask, produced: object) -> object:
        if isinstance(produced, RawReturn):
            return produced.value
        if isinstance(produced, Mapping) and self._usage is not None:
            return self.success(task, produced, self._usage)
        return super()._wrap(task, produced)


class ScriptedModelAdapter(_SyntheticAdapter):
    def __init__(
        self,
        script: Iterable[object],
        *,
        adapter_id: str = "scripted",
        adapter_version: str = "1",
        model_id: "str | None" = "scripted-model",
        capabilities: Iterable[str] = ("structured-output",),
        data_boundary: str = "LOCAL",
        deterministic: bool = True,
        profile_id: "str | None" = None,
        config_fingerprint: "str | None" = None,
        usage: "dict | None" = None,
    ) -> None:
        super().__init__(
            adapter_id=adapter_id, adapter_version=adapter_version, model_id=model_id, capabilities=capabilities,
            data_boundary=data_boundary, deterministic=deterministic, profile_id=profile_id,
            config_fingerprint=config_fingerprint, usage=usage,
        )
        self._script = list(script)
        self._index = 0

    def _invoke(self, task: ModelTask) -> object:  # type: ignore[override]
        self.tasks.append(task)
        if self._index >= len(self._script):
            return ModelFailure("ERROR", "roteiro esgotado: nenhuma resposta prevista para esta chamada")
        step = self._script[self._index]
        self._index += 1
        if callable(step) and not isinstance(step, (Mapping, ModelFailure, RawReturn, BaseException)):
            step = step(task)
        if isinstance(step, BaseException):
            raise step
        return step


class RuleBasedModelAdapter(_SyntheticAdapter):
    def __init__(
        self,
        rule: Callable[[ModelTask], object],
        *,
        adapter_id: str = "rule-based",
        adapter_version: str = "1",
        model_id: "str | None" = "rule-model",
        capabilities: Iterable[str] = ("structured-output",),
        data_boundary: str = "LOCAL",
        deterministic: bool = True,
        profile_id: "str | None" = None,
        config_fingerprint: "str | None" = None,
        usage: "dict | None" = None,
    ) -> None:
        super().__init__(
            adapter_id=adapter_id, adapter_version=adapter_version, model_id=model_id, capabilities=capabilities,
            data_boundary=data_boundary, deterministic=deterministic, profile_id=profile_id,
            config_fingerprint=config_fingerprint, usage=usage,
        )
        self._rule = rule

    def _invoke(self, task: ModelTask) -> object:  # type: ignore[override]
        self.tasks.append(task)
        return self._rule(task)
