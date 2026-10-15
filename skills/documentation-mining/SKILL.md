---
name: documentation-mining
description: Extrai afirmações de documentação existente (manuais, wikis, guias) com versão, e detecta divergências com o que foi observado.
version: 0.1.0
status: contract
requires: [schemas/evidence.schema.json, schemas/fact.schema.json, schemas/contradiction.schema.json, adapters/sources/documentation.adapter.json, adapters/sources/dokuwiki.adapter.json]
---

# Documentation Mining

> Status: contrato estrutural.

## Intenção

Tratar a documentação como uma fonte entre outras: o que ela afirma é evidência do que o documento diz, não do que o software faz.

## Entradas

- Fontes `documentation` e `wiki` inventariadas, com versão do documento quando identificável.

## Saídas

- `evidence` do tipo `text` ou `statement`, com localizador (seção, página).
- `fact` `OBSERVED` quanto ao que o documento afirma, com `valid_in_versions` quando o documento declara versão.
- `contradiction` quando o documento diverge do observado.

## Fronteiras

- Não promove afirmação de documento a `VALIDATED`.
- Não assume que o documento vale para a versão em uso.
- Não resume por conta própria: guarda o trecho e o localizador.

## Depende de

`policies/evidence-discipline.md`; adaptadores `documentation` e `dokuwiki`.

## Perguntas em aberto

- Representação de documento com várias versões e de páginas removidas.
- Tratamento de imagens e diagramas dentro de documentos.
- Heurística para estimar a idade provável de um documento sem versão.
