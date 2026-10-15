---
name: troubleshooting
description: Responde a "estou na tela X e aconteceu Y, o que faço?" combinando tela, fluxo, regras, erros conhecidos, dependências, versão e evidência, sem inventar o que falta.
version: 0.1.0
status: contract
requires: [schemas/screen.schema.json, schemas/business-rule.schema.json, schemas/contradiction.schema.json, policies/evidence-discipline.md]
---

# Troubleshooting

> Status: contrato estrutural. Capacidade de primeira classe do uso do pacote.

## Intenção

Dar suporte a quem usa o software, com respostas que o pacote sustenta e com incertezas visíveis.

## Entrada

| Campo | Obrigatório | Nota |
|---|---|---|
| software | sim | escolhe o pacote |
| relato | sim | o que a pessoa fez e o que aconteceu, nas palavras dela |
| tela | não | quando souber |
| versão | não | sem ela, a resposta avisa que o conhecimento pode não valer para a versão em uso |

## Registros usáveis como apoio

Entram como apoio autoritativo: registros `OBSERVED` ou `VALIDATED`, válidos para a versão, sem quarentena.
Entram só como hipótese, rotulados: registros `INFERRED`.
Nunca entram como apoio: registros em quarentena (mencionar a discordância), regras de alto impacto não validadas (mencionar a incerteza).

## Estrutura da resposta

1. **Situação entendida.** O que o agente entendeu do relato e em qual tela/fluxo.
2. **Causas prováveis**, ordenadas, cada uma com registros de apoio, status e confiança.
3. **O que fazer**, em passos, só com o que o pacote sustenta.
4. **O que não se sabe.** Lacunas que impedem uma resposta mais firme, e quem poderia fechá-las.
5. **Discordâncias.** Contradições abertas relevantes, com as duas posições.

## Fronteiras

- Não inventa mensagens de erro, passos ou regras ausentes do pacote.
- Não usa conhecimento externo para preencher lacunas.
- Não executa nada no software: só orienta.
- Não grava no pacote.

## Depende de

`documentation-generation` (mesma disciplina de rótulos); `policies/evidence-discipline.md`.

## Perguntas em aberto

- Registro de erros conhecidos (mensagem, causa, remédio) como coleção própria.
- Ranqueamento de causas quando há poucas evidências.
- Formato para respostas em canais curtos (chat, ticket).
