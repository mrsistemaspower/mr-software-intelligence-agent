# MR Software Intelligence Agent

Um agente de inteligência de software capaz de aprender, investigar, reconstruir, documentar e acompanhar sistemas complexos, especialmente ERPs e softwares legados, utilizando múltiplas fontes de evidência e intervenção humana mínima.

Repositório: https://github.com/mrsistemaspower/mr-software-intelligence-agent

## O problema que resolve

Softwares complexos e legados quase nunca têm documentação confiável. O conhecimento está espalhado em telas, manuais desatualizados, bancos de dados, código (quando existe) e na cabeça de poucas pessoas. Isso torna caro documentar, dar suporte, treinar e acompanhar mudanças de versão.

Um modelo de linguagem sozinho não resolve: ele inventa lacunas, mistura o que viu com o que supõe e esquece o que aprendeu. Este projeto coloca disciplina em volta do modelo:

- tudo que se afirma é rotulado como visto (`OBSERVED`), inferido (`INFERRED`) ou confirmado por uma pessoa (`VALIDATED`), com a confiança em um eixo à parte e a proveniência sempre registrada;
- contradições são preservadas e ficam em quarentena, nunca sobrescritas;
- pessoas só são chamadas depois que reuso de evidência, nova leitura e busca de fonte falharam, e só para decidir ou validar;
- o conhecimento fica em arquivos versionáveis, testáveis e independentes de modelo.

## Arquitetura em uma imagem

```
LLM  ->  Agent  ->  Skills + Tools  ->  Knowledge Pack  ->  Software
```

O modelo executa o agente; o agente usa skills e ferramentas; estas leem e gravam no pacote de conhecimento; o pacote descreve o software. Detalhes em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

| Pasta | O que há |
|---|---|
| [`AGENT.md`](AGENT.md) | contrato central: identidade, missão, disciplina de evidência, escalonamento, isolamento |
| [`core/`](core) | núcleo pequeno e testável, só biblioteca padrão |
| [`skills/`](skills) | métodos reutilizáveis |
| [`schemas/`](schemas) | contratos de dados |
| [`adapters/`](adapters) | fontes de evidência e perfis de ambientes de modelo |
| [`workflows/`](workflows) | a interface: seis verbos |
| [`policies/`](policies) | evidência, escalonamento humano, segurança e isolamento |
| [`software-packs/`](software-packs) | um pacote de conhecimento por software |
| [`evaluations/`](evaluations) | casos de avaliação e um pacote sintético de teste |
| [`tests/`](tests) | testes de contrato e de isolamento |
| [`docs/`](docs) | arquitetura, portabilidade, auditoria e decisões |

## Agente x conhecimento de software

Esta é a decisão central do projeto ([`docs/decisions/0001-agent-vs-software-knowledge.md`](docs/decisions/0001-agent-vs-software-knowledge.md)).

| Agente (genérico) | Conhecimento de software (específico) |
|---|---|
| `OBSERVED`, `INFERRED`, `VALIDATED` | nome de uma tela, título de uma janela |
| proveniência, confiança, revisão humana | uma regra fiscal ou de negócio específica |
| tratamento de contradição e quarentena | "o lookup X abre a tela Y" |
| agrupamento de capturas, raciocínio sobre lookups | os fluxos de um ERP em particular |
| descoberta de fluxos, escalonamento, impacto de mudança | os termos do domínio de um produto |

O núcleo não conhece nenhum software. Cada software tem um **pacote** em `software-packs/<id>/`. Trocar de software é trocar de pacote, sem reescrever o agente. Os testes impedem que o conhecimento de um software vaze para as camadas genéricas ou para outro pacote.

## Interface: seis verbos

| Verbo | Faz |
|---|---|
| `learn` | aprende ou amplia o conhecimento a partir das fontes |
| `explain` | explica uma tela, um fluxo ou uma regra |
| `ask` | responde "estou na tela X e aconteceu Y, o que faço?" |
| `find-gaps` | levanta o que falta saber e gera perguntas qualificadas |
| `compare` | compara versões e reanalisa só o que foi afetado |
| `document` | gera documentação derivada |

Ver [`workflows/README.md`](workflows/README.md).

## Principais skills

| Skill | Estado |
|---|---|
| [`erp-reverse-engineering`](skills/erp-reverse-engineering/SKILL.md) | especificada de ponta a ponta (do software desconhecido à atualização contínua) |
| `ui-reverse-engineering`, `documentation-mining`, `workflow-reconstruction`, `business-rule-discovery`, `database-analysis`, `runtime-exploration`, `change-intelligence`, `troubleshooting`, `documentation-generation` | contratos estruturais |

## Portabilidade entre modelos

As instruções do agente não dependem de Claude, ChatGPT/OpenAI, Kiro ou de qualquer outro provedor. Cada ambiente tem um perfil em `adapters/llm/` que só diz como carregar `AGENT.md` e como obter as ferramentas abstratas. Ver [`docs/MODEL_PORTABILITY.md`](docs/MODEL_PORTABILITY.md).

## Primeiro caso: SYSCOM

O SYSCOM é o **caso de treino e validação número 1**. O produto não é um agente do SYSCOM: ele foi o primeiro software usado para desenvolver e testar o método.

- A auditoria somente leitura do projeto SYSCOM está em [`docs/AGENT_FOUNDATION_AUDIT.md`](docs/AGENT_FOUNDATION_AUDIT.md): 25 capacidades confirmadas na implementação, 61 componentes classificados e uma ordem de extração.
- O pacote [`software-packs/syscom/`](software-packs/syscom) contém apenas manifesto, metadados e a referência de integração. O conhecimento real continua no workspace do projeto SYSCOM, que não é copiado.
- A prova de generalização será aplicar a Foundation a um segundo software sem reescrever o núcleo. Já existe um pacote sintético (`evaluations/fixtures/`) que exercita essa propriedade em teste.

## Visão de futuro

- Continuar a extração, contra os contratos desta fundação, dos motores comprovados no SYSCOM (hash, JSON canônico, escrita atômica, snapshot de conhecimento, a base de orquestração com checkpoints e retomada e o executor de `AI_RECHECK` já foram extraídos): roteamento de revisão, ingestão de respostas humanas.
- Aplicar o agente a outros ERPs da MR (por exemplo, o ERP Dolphins), a sistemas legados sem documentação e a softwares de terceiros, quando permitido.
- Implementar os adaptadores de fonte hoje descritos como contrato.
- Inteligência de mudança automática entre versões.

Fora do escopo deste marco: chat, interface web, banco vetorial, servidor de RAG, runtime autônomo, implantação em nuvem, observador contínuo, rastreador de DokuWiki, integração com o ERP Dolphins.

## Estado atual

**Agent Foundation 1.0.** Estão definidos: contrato do agente, disciplina de evidência, política de escalonamento, 12 conceitos de conhecimento e 6 contratos de configuração em JSON Schema, núcleo mínimo (validação de schema e de pacote, roteamento de revisão, impacto de mudança, verificação de isolamento), skill principal, nove contratos de skills, nove contratos de adaptadores de fonte, quatro perfis de ambiente de modelo (rascunho), seis workflows e dois pacotes (o do primeiro caso e um sintético) mais um modelo.

**Agent Extraction 1.** O núcleo ganhou as primitivas determinísticas extraídas do SYSCOM: JSON canônico, hash, escrita atômica, snapshot de conhecimento, verificação de integridade de fonte, identidade de artefato e portabilidade de caminhos, mais o schema `knowledge-snapshot`. Ver [`docs/AGENT_EXTRACTION_1.md`](docs/AGENT_EXTRACTION_1.md) e a rastreabilidade em [`docs/extraction/EXTRACTION_001.json`](docs/extraction/EXTRACTION_001.json).

**Agent Extraction 2.** O núcleo ganhou a base de orquestração determinística, genérica e sem conhecimento de software: grafo de estágios configurável (validado antes de executar), máquina de estados do estágio, checkpoints, retomada segura com invalidação só dos dependentes, recuperação de estágio interrompido, estado gravado de forma atômica, proteção de raízes de fonte (`READ_ONLY`, `FROZEN`, `MUTABLE_OUTPUT`) com detecção de mutação, e um adaptador opcional de estado do Git. Os estágios rodam tratadores fornecidos por quem executa; nada chama modelo de linguagem. Ver [`docs/AGENT_EXTRACTION_2.md`](docs/AGENT_EXTRACTION_2.md) e a rastreabilidade em [`docs/extraction/EXTRACTION_002.json`](docs/extraction/EXTRACTION_002.json).

**Agent Extraction 3.** O núcleo ganhou o executor genérico de `AI_RECHECK` e a fronteira do adaptador de modelo. `AI_RECHECK` é uma segunda análise focada, por IA, de evidência JÁ disponível: o agente decide o que rechecar, com que evidência autorizada, que forma a resposta tem, se ela é aceitável e para onde segue (reuso, lacuna de fonte, candidato a decisão humana, contingência ou falha); só um adaptador de modelo substituível fala com um modelo. A resposta nunca é confiada (validação determinística), todo achado é `INFERRED` (nenhuma segunda passada cria validação humana), um adaptador inelegível por capacidade ou por fronteira de dados não é chamado, e o recheck é um estágio comum do orquestrador, com o adaptador na identidade do checkpoint. Não há SDK, rede nem credencial: a suíte usa adaptadores sintéticos. Ver [`docs/AGENT_EXTRACTION_3.md`](docs/AGENT_EXTRACTION_3.md) e a rastreabilidade em [`docs/extraction/EXTRACTION_003.json`](docs/extraction/EXTRACTION_003.json).

**Agent Extraction 4.** O núcleo ganhou o roteamento genérico de revisão: o agente passou a saber QUANDO criar um `AI_RECHECK`. Sinais de análise portáteis (`AnalysisSignal`) e uma política versionada (`policies/review-routing.policy.json`) viram uma `ReviewDecision` determinística: `AUTO_PASS`, `SAMPLE_QA`, `AI_RECHECK`, `SOURCE_GAP` ou `HUMAN_DECISION`, com os motivos primários, os sinais secundários e a referência da política. Criticidade e confiança são eixos separados; evidência que existe e é incerta vai para releitura, evidência que falta vira lacuna de fonte (nunca decisão humana por si) e conflito que a IA não decide chama uma pessoa nomeada; afirmações de alto impacto são derivadas de efeitos genéricos, não marcadas à mão; a amostragem de qualidade é determinística; a classe nunca promove conhecimento (`AUTO_PASS` não é `VALIDATED`). Uma ponte entrega a decisão ao executor da Extração 3 e reroteia o resultado, com o laço epistêmico limitado. A extração também corrigiu o motor de estágios: uma falha marcada `retryable=false` falha na primeira tentativa e não gasta as demais. Não há portal de revisão humana, persistência do backlog nem emissores de sinais por software. Ver [`docs/AGENT_EXTRACTION_4.md`](docs/AGENT_EXTRACTION_4.md) e a rastreabilidade em [`docs/extraction/EXTRACTION_004.json`](docs/extraction/EXTRACTION_004.json).

Ainda não implementado: os adaptadores de fonte, a extração dos demais motores do SYSCOM (revisão humana e ingestão de respostas humanas), os emissores de sinais por tipo de software, qualquer adaptador real de provedor de modelo e qualquer execução autônoma com modelo de linguagem de verdade.

## Como usar e verificar

Sem dependências externas: só a biblioteca padrão do Python. Os testes foram executados com Python 3.15.0rc2. O código evita recursos posteriores ao Python 3.10, mas essa compatibilidade ainda não foi verificada em outras versões.

```
py -m unittest discover -s tests -t . -v      # testes
py -m core check-schemas                       # meta-checagem dos schemas
py -m core validate-packs                      # valida todos os pacotes
py -m core check-isolation                     # isolamento entre núcleo e software
py -m core validate-pack software-packs/<id>   # valida um pacote
```

No Windows use `py`; em outros sistemas, `python3`.

Para adicionar um software, copie `software-packs/_template/` (ver [`software-packs/README.md`](software-packs/README.md)).
