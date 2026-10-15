---
name: change-intelligence
description: Compara versões do software, detecta evidência alterada, calcula o conhecimento afetado, reanalisa só as regiões impactadas e revalida ou invalida fatos.
version: 0.1.0
status: contract
requires: [schemas/release-change.schema.json, core/impact.py, adapters/sources/release-notes.adapter.json, policies/escalation.policy.json]
---

# Change Intelligence

> Status: contrato estrutural. Capacidade de primeira classe: independe de qualquer software e é a base da atualização contínua.

## Intenção

Quando sai uma nova versão, não refazer tudo. Descobrir o que mudou, o que o conhecimento existente depende disso, e reanalisar só essa região.

## Fluxo

```
VERSÃO N  -->  VERSÃO N+1
   |
detectar evidência alterada
   |
calcular conhecimento impactado
   |
reanalisar só as regiões afetadas
   |
invalidar / revalidar fatos
   |
atualizar documentação
```

1. **Detectar evidência alterada.** Por notas de versão, por diferença de hash entre as fontes registradas e as novas, por observação em execução ou por relato humano. Cada mudança vira um `release-change` com o método de detecção.
2. **Calcular o conhecimento impactado.** `core.impact.plan_reanalysis` parte do assunto da mudança e das referências declaradas, e acrescenta os registros que dependem deles. O assunto e as referências recebem a ação declarada; dependentes recebem, no máximo, `REVALIDATE`. Registro `VALIDATED` afetado nunca fica abaixo de `REVALIDATE`.
3. **Reanalisar só as regiões afetadas.** Recoletar evidência das fontes afetadas. O resto do pacote não é tocado.
4. **Invalidar ou revalidar.** `INVALIDATE`: o registro deixa de valer na nova versão (registre com `valid_in_versions`, sem apagar o histórico). `REVALIDATE`: abrir revisão de rota humana quando a validação perdeu a base. `REANALYZE`: refazer a leitura.
5. **Atualizar a documentação.** Marcar como desatualizada a documentação derivada das regiões afetadas.

## Entradas

- Fontes da versão nova, inclusive notas de versão, e o pacote da versão anterior.

## Saídas

- `release-change`, plano de reanálise, revisões por perda de validação.

## Fronteiras

- Ausência de menção em notas de versão não prova ausência de mudança: complementar com diferença de evidência.
- Uma validação humana vale para a versão em que foi dada.
- Diferença entre versões não é contradição; é mudança.
- Não apaga registros de versões anteriores.

## Depende de

`documentation-mining`, `ui-reverse-engineering`; `core/impact.py`.

## Perguntas em aberto

- Granularidade da detecção de mudança em fontes visuais (diferença de imagem x diferença de leitura).
- Como tratar versões intermediárias puladas.
- Versionamento de `valid_in_versions` em registros com várias versões ativas.
