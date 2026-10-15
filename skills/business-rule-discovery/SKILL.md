---
name: business-rule-discovery
description: Descobre regras de negócio a partir de mensagens, restrições, código, documentação e especialistas, classificando o impacto de cada uma.
version: 0.1.0
status: contract
requires: [schemas/business-rule.schema.json, schemas/human-review.schema.json, policies/escalation.policy.json]
---

# Business Rule Discovery

> Status: contrato estrutural.

## Intenção

Registrar regras com a fonte de cada uma e com o impacto explícito, para que regras que podem causar dano não sustentem respostas sem validação humana.

## Entradas

- Evidências de mensagens de validação, restrições de banco, código, documentação e declarações de especialistas.

## Saídas

- `business-rule` com `kind`, `impact` e `applies_to`.
- `HUMAN_VALIDATION` para regras `high` ainda `INFERRED`.

## Fronteiras

- Impacto alto é decidido por conteúdo (efeito fiscal, financeiro, exclusão de dados, transmissão externa), não por confiança.
- Uma regra declarada por especialista vale para o que foi revisado; em outro registro entra como `INFERRED`.
- Não deduz regra a partir do nome de um campo.

## Depende de

`policies/human-escalation.md`; `core/escalation.py`.

## Perguntas em aberto

- Representação de fórmulas e cálculos.
- Dependência entre regras e precedência.
- Critérios objetivos adicionais para classificar `impact`.
