# schemas/

Contratos de dados do agente. São independentes de qualquer modelo de linguagem e de qualquer software alvo.

Todos seguem JSON Schema 2020-12 e têm `$id` no formato `urn:mr-software-intelligence:schema:<nome>:<versão>`. O nome do arquivo é `<nome>.schema.json`. Uma mudança incompatível cria `<nome>:2`; nunca se altera a semântica de uma versão publicada.

## Conhecimento (o que um pacote guarda)

| Schema | Registro | Prefixo do id | Ideia central |
|---|---|---|---|
| `common` | definições compartilhadas | n/a | bloco `epistemic`: status, confiança e proveniência |
| `software` | Software | n/a | o que se sabe sobre as fontes disponíveis, não sobre o software |
| `source` | Source | `src` | fonte só localizada, nunca copiada; hash como ponto de integridade |
| `evidence` | Evidence | `evd` | trecho concreto de uma fonte; sem status, só qualidade de extração |
| `fact` | Fact | `fct` | afirmação; OBSERVED exige evidência |
| `screen` | Screen | `scr` | superfície de interação; captura não é tela |
| `interaction` | Interaction | `int` | gatilho e efeito; sem teste em execução nunca é OBSERVED |
| `lookup` | Lookup | `lku` | destino desconhecido não admite referência |
| `workflow` | Workflow | `wfl` | fluxo de negócio do software alvo |
| `business-rule` | BusinessRule | `rul` | regra com `impact` explícito |
| `contradiction` | Contradiction | `ctr` | posições preservadas; quarentena enquanto aberta |
| `human-review` | HumanReview | `rev` | quatro rotas e registro do que já foi tentado |
| `release-change` | ReleaseChange | `rel` | mudança entre versões e seu impacto |

## Estado derivado (o que o núcleo calcula)

| Schema | Registro | Ideia central |
|---|---|---|
| `knowledge-snapshot` | KnowledgeSnapshot | descreve o conhecimento consumido por uma operação, por caminho lógico e hash; a identidade deriva só do conteúdo, nunca de relógio ou máquina. Ver `core/snapshot.py` |
| `stage-graph` | StageGraph | estágios nomeados com dependências explícitas; a ordem no arquivo é a topológica determinística. Ver `core/stage_graph.py` |
| `stage-checkpoint` | StageCheckpoint | prova de que um estágio concluído pode ser pulado: amarra o resultado a definição, configuração, snapshot, entradas, dependências e saídas. Ver `core/checkpoint.py` |
| `run-state` | RunState | andamento de uma execução: identidade determinística, status, estado de cada estágio, proteção das fontes e violações. Ver `core/run_state.py` |
| `run-config` | RunConfig | configuração portátil e com hash de uma execução: grafo, raízes por nome com classe de proteção, entradas nomeadas e parâmetros por estágio. Ver `core/run_config.py` |

## Modelo e AI_RECHECK (contratos entre o agente e um modelo)

| Schema | Registro | Ideia central |
|---|---|---|
| `model-task` | ModelTask | pedido estruturado e neutro de provedor: evidência autorizada presa por hash, único contexto permitido, contrato de saída, capacidades e fronteira de dados exigidas. Define os blocos compartilhados. Ver `core/model_task.py` |
| `model-result` | ModelResult | resposta de um adaptador: saída estruturada OU falha técnica, nunca as duas; quem respondeu; uso opcional. Ver `core/model_adapter.py` |
| `ai-recheck-request` | AIRecheckRequest | segunda análise de evidência existente: assunto genérico, ambiguidade, afirmações, evidência autorizada, interpretação existente. Ver `core/ai_recheck.py` |
| `ai-recheck-result` | AIRecheckResult | resultado aceito (`RESOLVED`, `SOURCE_GAP`, `HUMAN_DECISION`, `INCONCLUSIVE`, `FAILED`) com rota e proveniência; todo achado é `INFERRED`. Define também `modelOutput`, a resposta bruta exigida do modelo |

## Roteamento de revisão (quando criar uma releitura, uma lacuna de fonte ou uma decisão humana)

| Schema | Registro | Ideia central |
|---|---|---|
| `analysis-signal` | AnalysisSignal | observação portátil de uma análise: família aberta e qualificador, criticidade e confiança como eixos separados, classe de impacto derivável de efeitos, evidência por referência e hash. Define os blocos compartilhados. Ver `core/analysis_signal.py` |
| `review-decision` | ReviewDecision | a rota de um assunto (`AUTO_PASS`, `SAMPLE_QA`, `AI_RECHECK`, `SOURCE_GAP`, `HUMAN_DECISION`) com sinais considerados, motivos, sinais secundários, referência da política e snapshot de evidência; `validation_effect` é sempre `NONE`. Ver `core/review_routing.py` |
| `source-gap-requirement` | SourceGapRequirement | o que falta obter, por quê, tipos genéricos de fonte, impacto e se bloqueia |
| `human-decision-candidate` | HumanDecisionCandidate | a fronteira com a revisão humana: o que decidir, por que a IA não decide, evidência, posições em conflito e o que não se pode presumir; não carrega resposta |

A política que alimenta o roteamento é dado, em `policies/review-routing.policy.json`, validada por `core/review_policy.py` (sem schema próprio: as regras entre campos, como monotonia e `decision_kind`, só o código confere). O formato gravado de falha em `run-state` ganhou `retryable`, presente somente quando é `false`.

## Contratos de configuração (como o agente se organiza)

| Schema | Valida |
|---|---|
| `software-pack-manifest` | `software-packs/<id>/manifest.json` |
| `integration-reference` | `software-packs/<id>/integration.reference.json` |
| `source-adapter` | `adapters/sources/*.adapter.json` |
| `llm-profile` | `adapters/llm/*.profile.json` |
| `agent-workflow` | `workflows/*.workflow.json` |
| `escalation-policy` | `policies/escalation.policy.json` |

## Regras que os schemas impõem

- Status e confiança são campos separados. `confidence` vai de 0 a 1.
- Toda afirmação carrega proveniência (`epistemic.provenance`, ao menos uma entrada).
- `INFERRED` exige `basis`. `VALIDATED` exige `validation` (quem, quando, como). `OBSERVED` e `INFERRED` não podem ter `validation`.
- Um registro em quarentena não pode ser `VALIDATED`.
- Lookup com destino `UNKNOWN` não tem referência. `KNOWN` exige `OBSERVED` ou `VALIDATED`. `PROBABLE` exige `INFERRED`.
- Interação com `runtime_tested = false` nunca é `OBSERVED`.
- Revisão por rota humana exige `attempted_before` e, ao ser respondida, resposta de pessoa.
- Caminhos de fonte são relativos ao workspace. Letra de unidade, barra inicial e `..` são rejeitados.

Regras que dependem de mais de um registro (referências existentes, hash fixado igual ao da fonte, contradição aberta com posições em quarentena) são verificadas por `core/pack.py`, não pelos schemas.

## Avaliado e adiado

`Entity` (entidade de negócio) foi avaliada e adiada. Ela aparece hoje como assunto de fatos e destino de lookups. Será reavaliada quando a primeira extração precisar de esquema de banco. Ver `docs/AGENT_FOUNDATION_AUDIT.md`, seção 7.
