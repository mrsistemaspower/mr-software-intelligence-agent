---
name: ui-reverse-engineering
description: Reconstrói superfícies de interação (telas, diálogos, páginas), seus elementos e estados a partir de capturas, execução ou código.
version: 0.1.0
status: contract
requires: [schemas/screen.schema.json, schemas/evidence.schema.json, schemas/interaction.schema.json, adapters/sources/screenshots.adapter.json]
---

# UI Reverse Engineering

> Status: contrato estrutural. A intenção e as fronteiras estão fixadas; o método detalhado ainda não foi escrito.

## Intenção

Descrever com fidelidade o que está visível e o que cada elemento aciona, sem confundir estado de captura com comportamento, nem captura com tela.

## Entradas

- Fontes do tipo captura, execução ou código já inventariadas.
- Pacote com `source` registrados.

## Saídas

- `evidence` de elementos visuais, com confiança de leitura.
- `screen` com `elements` e `grouping` (`DECIDED` ou `UNRESOLVED`).
- `interaction` com `runtime_tested` explícito.
- Revisões `AI_RECHECK`, `SOURCE_GAP` e `HUMAN_DECISION` (agrupamento).

## Fronteiras

- Não infere regra de negócio (ver `business-rule-discovery`).
- Não decide agrupamento ambíguo; registra `UNRESOLVED`.
- Não preenche conteúdo de região não capturada.
- Não incorpora imagens a registros.

## Depende de

`policies/evidence-discipline.md`; adaptador `screenshots`; opcionalmente `runtime`.

## Perguntas em aberto

- Vocabulário de `surface_kind` e de `element.kind` para superfícies que não são janelas (linha de comando, API).
- Estratégia para estados repetidos (a mesma tela com dados diferentes).
- Critérios objetivos de "rótulo truncado" para disparar `SOURCE_GAP`.
