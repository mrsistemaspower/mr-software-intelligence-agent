# 0001. Capacidades do agente são separadas do conhecimento de software

- Status: aceita
- Data: 2026-10-14
- Decisores: MR Sistemas (Agent Foundation 1.0)

## Contexto

O primeiro laboratório do agente foi o projeto SYSCOM. Ele provou um método valioso: separar o que foi visto, o que foi inferido e o que uma pessoa confirmou; manter a confiança como eixo à parte; preservar versões em conflito; só chamar humanos quando necessário; processar lotes de forma determinística e retomável.

A auditoria (`docs/AGENT_FOUNDATION_AUDIT.md`) mostrou que esse método está fundido ao software em três camadas: um modelo de dados centrado em tela de formulário, tabelas de regras escritas à mão por pergunta e por rodada, e caminhos, datas, ids e prefixos de versão espalhados pelo código. Extrair isso por cópia de scripts levaria o SYSCOM para dentro do agente.

O produto não é um agente do SYSCOM. É um agente de alta inteligência para compreender e dar suporte a softwares complexos e legados. Ele deve funcionar também para o ERP Dolphins, outros ERPs da MR, sistemas sem documentação ou sem código-fonte e softwares de terceiros, quando permitido. Deve também funcionar com Claude, ChatGPT, Kiro, modelos locais e modelos futuros.

## Decisão

Separamos rigidamente duas camadas:

1. **Agent Core e capacidades** (`core/`, `skills/`, `schemas/`, `adapters/`, `workflows/`, `policies/`, `tests/`): método, contratos e mecanismos. Não conhecem nenhum software nem nenhum provedor de modelo.
2. **Software Knowledge Pack** (`software-packs/<software-id>/`): tudo que se sabe sobre um software específico, ou a referência para onde esse conhecimento vive.

Consequências operacionais, todas verificadas por testes:

- Cada pacote declara os termos que o identificam (`isolation.leak_terms`). Esses termos não podem aparecer nas camadas genéricas nem em outros pacotes.
- O núcleo não ramifica por `software_id` e não contém ids de software como constante.
- Nomes de provedores de modelo só aparecem em `adapters/llm/`.
- O conhecimento do SYSCOM permanece no workspace dele. O pacote `software-packs/syscom/` guarda apenas manifesto, metadados e a referência de integração.
- O SYSCOM é o caso de treino e validação número 1. A prova de generalização é aplicar a Foundation a um segundo software sem reescrever o núcleo. Um pacote sintético (`evaluations/fixtures/`) já exercita essa propriedade em teste.

## Alternativas consideradas

- **Copiar o pipeline do SYSCOM para o novo repositório e generalizar depois.** Rejeitada: leva o acoplamento junto e dificulta saber o que é método e o que é conhecimento.
- **Um pacote por software dentro do próprio núcleo, com regras condicionais por `software_id`.** Rejeitada: cada novo software exigiria mexer no núcleo, que é exatamente o que a prova de generalização proíbe.
- **Um grande prompt de instruções com o conhecimento embutido.** Rejeitada: amarra o produto a um modelo e a um tamanho de contexto, e impede teste automático.
- **Copiar o conhecimento do SYSCOM para o pacote.** Rejeitada: duplicaria centenas de artefatos e criaria duas fontes de verdade. O pacote referencia o workspace.

## Consequências

Positivas:

- O mesmo núcleo atende vários softwares, e novos softwares entram sem alterar o núcleo.
- A propriedade intelectual da MR fica em metodologia, skills, workflows, schemas, ferramentas, avaliação, políticas e contratos de conhecimento, e não em um modelo ou prompt.
- Isolamento e contratos são testáveis.

Custos aceitos:

- A extração dos motores do SYSCOM exige reescrita contra os contratos genéricos, não cópia.
- Todo novo software precisa de um pacote com termos de isolamento coerentes.
- Os schemas genéricos ainda herdam a ideia de "tela" como superfície principal. Entidades e esquemas de banco ficaram para uma próxima versão (`schemas/README.md`).

## Verificação

`tests/test_isolation.py`, `tests/test_second_pack_ready.py`, `tests/test_manifests.py` e `py -m core check-isolation`.
