---
name: database-analysis
description: Analisa o esquema do banco de dados (tabelas, colunas, chaves, restrições, relações) como evidência de estrutura e de regras, sem ler dados.
version: 0.1.0
status: contract
requires: [adapters/sources/database-schema.adapter.json, schemas/evidence.schema.json, schemas/fact.schema.json]
---

# Database Analysis

> Status: contrato estrutural.

## Intenção

Usar o esquema como fonte de estrutura e de evidência de regras, ligando-o a telas e campos quando houver base para isso.

## Entradas

- Fonte `database-schema` inventariada, com autorização humana registrada.

## Saídas

- `evidence` do tipo `structure` e `metadata`.
- `fact` sobre entidades, relações e restrições.
- Ligações candidatas entre colunas e campos, sempre `INFERRED`.

## Fronteiras

- Somente metadados e esquema. Nunca lê linhas de dados.
- Conexão somente leitura, em cópia ou ambiente de teste.
- Nome de coluna não prova significado de negócio.
- Cadeias de conexão e segredos não entram em registros.

## Depende de

`policies/safety-and-isolation.md`; adaptador `database-schema`.

## Perguntas em aberto

- Registro dedicado de entidade (hoje adiado).
- Mapeamento de colunas para campos de tela.
- Tratamento de bancos sem metadados de chaves estrangeiras.
