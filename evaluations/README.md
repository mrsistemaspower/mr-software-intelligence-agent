# evaluations/

Como saber se o agente continua correto e continua genérico.

| Item | Para quê |
|---|---|
| `escalation-cases.json` | casos do roteamento de revisão: sinais de entrada e rota esperada. Executados por `tests/test_escalation.py` |
| `fixtures/` | pacotes sintéticos completos, um por subdiretório. Não representam nenhum software real. Servem de segundo software em teste |

## O teste de generalização

Critério de aceitação da fundação: aplicar o mesmo agente a um segundo software **sem reescrever o núcleo**.

O pacote sintético em `fixtures/` já é esse segundo software. Os testes provam que:

1. ele valida com as mesmas regras de qualquer pacote;
2. nenhum arquivo do núcleo, dos schemas, das skills, dos adaptadores, dos workflows ou das políticas muda para recebê-lo;
3. os termos dele e os de qualquer outro pacote não vazam para as camadas genéricas nem um para o outro;
4. as capacidades de impacto de mudança e de roteamento funcionam sobre ele.

A prova definitiva é a mesma coisa com um software real da MR. Esse marco não analisa nenhum segundo software real.

## Como estender

- Novo caso de roteamento: acrescente um item em `escalation-cases.json` com `signals`, `expected_route` e `expected_reason`.
- Novo pacote sintético: copie `software-packs/_template/` para `fixtures/<id>/` e preencha. Ele precisa validar.
- Novo ambiente de modelo: depois de criar o perfil, rode os casos no ambiente real e só então marque o perfil como `verified`.

## Métrica a acompanhar

**Dependência humana**: fração de itens examinados que chegaram a `HUMAN_DECISION` ou `HUMAN_VALIDATION`. A tendência esperada é de queda a cada passada, à medida que o conhecimento validado é reaproveitado.
