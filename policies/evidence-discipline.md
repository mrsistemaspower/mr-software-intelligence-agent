# Disciplina de evidência

Esta política vale para qualquer software e qualquer modelo. Os schemas e o `core/` a impõem; este texto explica o porquê.

## Status e confiança são eixos diferentes

| Eixo | Pergunta que responde | Valores |
|---|---|---|
| `status` | O que sustenta esta afirmação? | `OBSERVED`, `INFERRED`, `VALIDATED` |
| `confidence` | Quão certa é a afirmação, dentro do status? | número de 0 a 1 |

Exemplo: `status: OBSERVED` com `confidence: 0.93` significa "foi visto, e a leitura é nítida". `OBSERVED` com `0.35` significa "foi visto, mas a imagem é ruim". `INFERRED` com `0.9` significa "interpretação muito provável, ainda não confirmada". Nunca se troca um eixo pelo outro.

A faixa (`low`, `medium`, `high`) é derivada da confiança por `core/epistemics.band` com os limites de `policies/escalation.policy.json`. Ela nunca é gravada.

## Significado dos status

- `OBSERVED`: está diretamente numa fonte concreta (captura, arquivo, trecho de código, esquema de banco, saída de execução). Exige ao menos uma evidência.
- `INFERRED`: interpretação do agente a partir de evidências. Exige `basis`: por que se conclui isso.
- `VALIDATED`: confirmado por uma pessoa responsável nomeada, com data e método. Um agente nunca se auto-valida.

Todo texto produzido por um modelo nasce `INFERRED`, exceto a leitura direta de uma fonte, que nasce `OBSERVED`.

## Regras de promoção

1. `INFERRED` não vira `VALIDATED` por parecer óbvio. Só uma pessoa nomeada promove, por uma revisão respondida.
2. A validação vale para o que foi revisado. Em outro registro, o mesmo conhecimento entra como `INFERRED` com proveniência `reused`.
3. Estado visível numa captura não é comportamento. Sem teste em execução, o efeito de uma interação não é `OBSERVED`.
4. Ausência de evidência não é negação. "Não aparece na captura" não vira "não existe".
5. `null` não é `false`. "Não conferido" é diferente de "conferido e negativo".
6. Lista vazia não é "nenhum". Registre a cobertura (o que foi examinado) quando a ausência importar.
7. Destino desconhecido de um lookup fica `UNKNOWN` e sem referência. Similaridade de nome sozinha nunca confirma destino.
8. Uma afirmação que cruza várias capturas, enquanto o agrupamento das capturas está `UNRESOLVED`, permanece `INFERRED` e não pode ser promovida.

## Proveniência de primeira classe

Toda afirmação aponta para fontes (`epistemic.provenance`). Quando a fonte tem hash, a entrada de proveniência fixa o hash observado (`source_sha256`). Se a fonte mudar, a validação do pacote reprova: a evidência ficou velha e a afirmação precisa ser reavaliada.

Fontes são localizadas, nunca copiadas. O conhecimento real fica no workspace do software, e o pacote guarda registros e referências.

## Contradição e quarentena

Duas posições incompatíveis sobre o mesmo assunto viram uma `contradiction`. Regras:

- As posições nunca são apagadas, nem depois de resolvidas (`history_preserved` é sempre `true`).
- Enquanto a contradição está `OPEN`, cada posição fica em `epistemic.quarantine` apontando para ela. Registro em quarentena não pode ser `VALIDATED` nem sustentar resposta autoritativa.
- Quem resolve é uma pessoa nomeada, por decisão ou por nova evidência, com justificativa. Ao resolver, a quarentena é retirada.
- Uma afirmação de pessoa que contradiz a evidência observada não sobrescreve a evidência e não é descartada: as duas ficam registradas.

## Impacto

Regras de negócio declaram `impact` (`low`, `normal`, `high`). Uma regra de alto impacto (por exemplo, efeito fiscal, financeiro, exclusão de dados) que esteja apenas `INFERRED` vai para `HUMAN_VALIDATION` antes de sustentar qualquer resposta autoritativa, mesmo com confiança alta.

## Resposta quando falta conhecimento

O agente responde o que o pacote sustenta, cita a proveniência e o status, e diz com clareza o que não sabe e o que faltaria para saber. Não preenche lacuna com conhecimento externo ao pacote.
