# Escalonamento humano

Princípio: humanos respondem somente o que a IA não consegue resolver de maneira segura. O tempo de uma pessoa é o recurso mais caro do processo e é gasto só quando nenhuma etapa anterior resolve.

Os parâmetros executáveis estão em `policies/escalation.policy.json` e são lidos por `core/escalation.py`.

## Escada de tentativas

Antes de qualquer pergunta a uma pessoa, o agente percorre, nesta ordem:

1. conhecimento já validado que se aplica;
2. reuso de evidências já coletadas;
3. documentação existente;
4. relações entre registros;
5. nova leitura focada da mesma evidência (`AI_RECHECK`);
6. observação em execução, só com autorização humana e em ambiente seguro.

A ordem vai do mais barato e seguro ao mais caro e arriscado. Cada tentativa relevante é registrada em `human-review.attempted_before`. Uma revisão de rota humana sem esse registro é inválida.

## As quatro rotas

| Rota | Quando | Quem fecha | Pessoa nomeada |
|---|---|---|---|
| `AI_RECHECK` | A evidência existe e a leitura é ambígua. Ainda não houve nova leitura. | O agente | Não |
| `SOURCE_GAP` | A resposta não está em nenhuma fonte disponível. Falta fonte, não leitura. | Quem obtiver a fonte indicada | Não |
| `HUMAN_DECISION` | Há alternativas e a evidência não decide entre elas. | Pessoa escolhe entre as `options` | Sim |
| `HUMAN_VALIDATION` | Há uma inferência a confirmar (nova leitura não resolveu, ou impacto alto). | Pessoa confirma, refina ou contradiz | Sim |

`AI_RECHECK` só tem sentido com leitura nova da evidência. Se não existe evidência diferente da já lida, a rota correta é `SOURCE_GAP`.

## Perguntas boas

- Uma pergunta por decisão. Perguntas equivalentes são agrupadas por `family_key` (ou pela pergunta normalizada) e respondidas uma vez; a resposta vale para todos os membros.
- A pergunta traz o contexto mínimo: o que foi visto, o que se supõe, o que se quer saber e as alternativas.
- A pergunta nunca pede à pessoa para fazer o trabalho de leitura que o agente pode fazer.
- Conhecimento que uma pessoa já validou em outro registro é aplicado como `INFERRED` e suprime a pergunta repetida, exceto quando a divergência entre contextos é justamente o que se quer descobrir.

## Resposta humana

- Fica registrada em `answer`, com `answered_by` (pessoa nomeada), data, texto e, quando possível, o trecho literal.
- Cada efeito sobre os registros é declarado em `answer.effects` (`validates`, `refines`, `contradicts`, `withholds`, `resolves`, `closes-gap`).
- Uma resposta que contradiz a evidência observada não é descartada nem aceita às cegas: abre uma `contradiction` e os dois lados ficam em quarentena.
- A validação resultante aponta para a revisão (`validation.review_id`) e `validated_by` é exatamente quem respondeu.

## Medida de sucesso

O processo deve reduzir a dependência humana ao longo do tempo: a fração de itens que chegam a `HUMAN_DECISION` ou `HUMAN_VALIDATION` em relação ao total examinado é um indicador a acompanhar em `evaluations/`.
