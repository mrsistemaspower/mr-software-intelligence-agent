---
name: workflow-reconstruction
description: Reconstrói fluxos de negócio compondo telas, interações, lookups e regras em sequências com lacunas explícitas.
version: 0.1.0
status: contract
requires: [schemas/workflow.schema.json, schemas/interaction.schema.json, schemas/lookup.schema.json]
---

# Workflow Reconstruction

> Status: contrato estrutural.

## Intenção

Ligar o que já foi descoberto em passos ordenados que correspondam a uma tarefa de negócio, mostrando onde a ligação é evidência e onde é suposição.

## Entradas

- Telas, interações, lookups e regras registrados no pacote.

## Saídas

- `workflow` com passos ordenados que apontam para registros existentes.
- Lacunas entre passos registradas como revisões (`HUMAN_VALIDATION` para ordem ou ramificação ambígua).

## Fronteiras

- Um fluxo composto é `INFERRED` até confirmação.
- Não inventa passos para fechar buracos.
- Não confunde o `workflow` do software com os workflows do agente (`workflows/`).

## Depende de

`ui-reverse-engineering`, `business-rule-discovery`; `policies/evidence-discipline.md`.

## Perguntas em aberto

- Representação de ramificações, laços e passos opcionais.
- Fluxos que atravessam vários módulos ou integrações externas.
- Granularidade adequada de um passo.
