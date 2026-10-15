# workflows/

A interface do agente tem **seis verbos**. Quem usa não precisa conhecer a arquitetura interna: escolhe o verbo, informa o software e o que quer.

| Verbo | Pergunta que responde | Exemplo de uso |
|---|---|---|
| `learn` | Aprenda este software (ou aprenda mais) a partir das fontes. | `learn <software> [--source <fonte>]` |
| `explain` | Explique esta tela, fluxo ou regra. | `explain <software> <registro>` |
| `ask` | Estou nesta tela e aconteceu isto. O que faço? | `ask <software> "<pergunta>" [--screen <id>] [--version <v>]` |
| `find-gaps` | O que ainda não sabemos e quem precisa responder? | `find-gaps <software>` |
| `compare` | O que mudou entre duas versões e o que precisa ser reanalisado? | `compare <software> <versão-de> <versão-para>` |
| `document` | Gere a documentação desta funcionalidade. | `document <software> <alvo> [--profile compact\|detailed]` |

Os exemplos são a forma conceitual do pedido. Cada ambiente de modelo expõe os verbos do seu jeito (comando, frase, atalho); o contrato é o arquivo `<verbo>.workflow.json`, validado por `schemas/agent-workflow.schema.json`.

## O que cada workflow define

- `inputs`: o que o verbo precisa.
- `skills`: quais skills de `skills/` ele usa.
- `steps`: os passos, com portões (`gate`) onde uma verificação precisa passar.
- `outputs`: o que entrega.
- `mutates_knowledge`: se grava no pacote. Verbos que não gravam (`explain`, `ask`, `document`) nunca alteram registros.
- `escalates_to_human`: se pode abrir revisões de rota humana.
- `policies`: as políticas obrigatórias.

## Workflow do agente não é workflow do software

Aqui "workflow" é um procedimento do agente. O fluxo de negócio do software alvo é o registro `workflow` (`schemas/workflow.schema.json`), que mora no pacote do software.

## Regras comuns a todos os verbos

1. Cada pedido nomeia um software e usa o pacote dele. Nunca mistura pacotes.
2. Respostas citam status, confiança e proveniência.
3. O que o pacote não sustenta é dito como "não sei", com o que faltaria para saber.
4. Nada é escrito no workspace do software.
