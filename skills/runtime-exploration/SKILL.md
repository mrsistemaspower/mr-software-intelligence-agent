---
name: runtime-exploration
description: Observa o software em execução, com autorização humana e sem efeito colateral, para transformar comportamento inferido em comportamento observado.
version: 0.1.0
status: contract
requires: [adapters/sources/runtime.adapter.json, schemas/interaction.schema.json, policies/safety-and-isolation.md]
---

# Runtime Exploration

> Status: contrato estrutural.

## Intenção

Fechar lacunas de comportamento e de destino de lookups acionando elementos em ambiente seguro, registrando a sessão como fonte.

## Entradas

- Autorização humana registrada para a sessão e o ambiente de teste.
- Lista de elementos a acionar, vinda de revisões `SOURCE_GAP` com `needed_source.kind = runtime`.

## Saídas

- Fonte `runtime` (log da sessão) com hash.
- Evidências `behavior`.
- Interações atualizadas para `OBSERVED` com `runtime_tested = true`.

## Fronteiras

- Nunca em produção. Nunca acionar gravação, exclusão, emissão ou envio.
- Cada sessão registra o que foi acionado e o contexto (versão, configuração).
- Um efeito observado uma vez não prova que ocorre sempre.
- Não é a única forma de aprender: vem depois do reuso de evidência e da nova leitura, na escada.

## Depende de

`policies/human-escalation.md`; adaptador `runtime`.

## Perguntas em aberto

- Mecanismos de automação de interface e como registrar a sessão de forma reproduzível.
- Como saber que uma ação é segura antes de acioná-la.
- Isolamento de dados de teste.
