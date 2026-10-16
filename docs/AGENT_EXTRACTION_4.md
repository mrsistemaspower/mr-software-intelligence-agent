# Agent Extraction 4: roteamento genérico de revisão

Escopo: o agente já sabia COMO executar um `AI_RECHECK` (Extração 3). Esta extração entrega QUANDO criar um: um motor genérico, determinístico e sem conhecimento de software que converte sinais de análise em uma de cinco rotas (`AUTO_PASS`, `SAMPLE_QA`, `AI_RECHECK`, `SOURCE_GAP`, `HUMAN_DECISION`), mais a correção da semântica de repetição do motor de estágios (`retryable`).

## Definição do roteamento de revisão

Roteamento de revisão é uma função pura: `sinais + política (+ histórico de releitura) -> ReviewDecision`. Ele decide em que FILA DE TRABALHO um assunto entra. Não decide a verdade de nada.

- Entrada: `AnalysisSignal` (o que a análise observou), `ClaimAssessment` opcional (afirmações com efeitos declarados), a política (`ReviewPolicy`) e os registros de releituras por IA já concluídas.
- Saída: uma `ReviewDecision` com a classe, os motivos primários, os sinais secundários, se exige pessoa nomeada, a próxima ação, a referência da política e o snapshot de evidência. Conforme a classe, a decisão carrega também o plano para montar um `AIRecheckRequest`, os requisitos de fonte (`SourceGapRequirement`) ou os candidatos à decisão humana (`HumanDecisionCandidate`).
- Não faz: rede, leitura de arquivo, execução de modelo, Git, relógio. Só `load_policy` lê um arquivo, e o roteador nunca a chama.

Módulos: `core/analysis_signal.py` (o contrato do sinal e o vocabulário), `core/review_policy.py` (a política como dado), `core/review_routing.py` (o roteador e os artefatos), `core/review_recheck.py` (a ponte com o executor de `AI_RECHECK`). Schemas: `schemas/analysis-signal.schema.json`, `schemas/review-decision.schema.json`, `schemas/source-gap-requirement.schema.json`, `schemas/human-decision-candidate.schema.json`. Política: `policies/review-routing.policy.json`. Não há camada de orquestração nova: o roteamento é uma função que um estágio comum do orquestrador da Extração 2 pode chamar.

## Fontes estudadas

O `mr-syscom` foi tratado como somente leitura. Dois arquivos foram lidos, sem importá-los nem executá-los de dentro do workspace de origem:

| Arquivo | O que se estudou |
|---|---|
| `docs/HUMAN_REVIEW_POLICY.md` (política 2.1.0) | as cinco classes, a árvore de decisão de seis posições, o item crítico, o princípio "`confidence: low` não é revisão humana automática", a diferença entre cobertura de fonte e falha de IA, a privacidade sistêmica, a ordem de trabalho em escala e as taxas iniciais de amostragem |
| `tools/review/derive_review_class.py` | a derivação da classe como função pura, a ordem exata dos gatilhos, o que vira apenas flag, o registro de `AI_RECHECK` esgotado |

Também foram lidos, no próprio agente, `core/escalation.py` (a escada da Fundação), `core/ai_recheck.py`, `core/recheck_engine.py`, `core/recheck_stage.py` e o motor de estágios (`core/orchestrator.py`, `core/resume.py`, `core/stage_state.py`). O código de roteamento do K4.2 e os demais arquivos de `tools/batch/` não foram consultados: as duas fontes acima bastaram.

Achado principal: no SYSCOM, a árvore de decisão é validada e a classe é derivada por código, mas os sinais que a alimentam são vocabulário de tela, e um gatilho (comportamento de alto impacto afirmado pela documentação) era marcado à mão porque "não é derivável por código". Esta extração leva a árvore e os princípios, troca os sinais por um contrato genérico e torna esse gatilho derivável.

## Princípios genéricos extraídos

1. A classe é DERIVADA por código a partir de sinais, nunca atribuída por opinião.
2. `HUMAN_DECISION` não é o padrão. Só uma pendência que a evidência e a IA não decidem com segurança chama uma pessoa.
3. Falta de evidência é lacuna de fonte, não falha de IA. Invariante: SOURCE_GAP nunca vira HUMAN_DECISION por si só.
4. `AI_RECHECK` vem antes da pessoa quando a evidência existente pode resolver a dúvida. Uma releitura feita e não resolvida, num ponto crítico, escala (para pessoa, por padrão).
5. Baixa confiança sozinha não leva à pessoa; confiança média sozinha nem chega a rotear.
6. Limitação da fonte não se confunde com incerteza do modelo.
7. Conflito pode exigir decisão humana, mas nem toda contradição exige.
8. A classe nunca promove conhecimento. `AUTO_PASS` quer dizer "nada pede julgamento agora", não "correto".
9. Observações informativas (relação opcional desconhecida, estrutura sem dados) não definem classe; viram sinal secundário.
10. As taxas de amostragem são configuração operacional, não verdade estatística.

## Acoplamento do SYSCOM rejeitado

Resumo; o detalhe, item a item, está em [`extraction/EXTRACTION_004.json`](extraction/EXTRACTION_004.json).

- O vocabulário de item crítico (identidade, caminho de menu, campos, botões, abas, colunas de grid) e as marcações de auditoria por componente. O sinal genérico fala de um assunto qualquer e de um ponto dentro dele (`target_ref`).
- As 28 flags secundárias. A taxonomia mínima tem 11 famílias e um qualificador opcional (ver Sinal de análise).
- Os códigos de limite físico de captura e os nomes de fonte de dado do projeto de origem. Os tipos de fonte são genéricos; nomes de adaptadores moram nos adaptadores.
- As classes de privacidade por campo e a razão social sistêmica do acervo. Ficam o escopo (`subject_specific` ou `systemic_handled`) e a restrição de fronteira de dados.
- A biblioteca de padrões de interação e as contradições de padrão com nomes próprios.
- Raízes congeladas, pasta de telas padrão, nomes de política e de schema de triagem, a gravação de campos no arquivo de validação de entrada e a data de geração fixa no código.
- O enum histórico de três classes.

## Sinal de análise

`AnalysisSignal` (`core/analysis_signal.py`, `schemas/analysis-signal.schema.json`) é uma observação portátil. O assunto pode ser qualquer artefato: código, documento, objeto de banco, rastro de execução, configuração, API, fluxo, regra, nota de versão, captura de tela ou um objeto desconhecido.

| Campo | Papel |
|---|---|
| `family`, `qualifier` | o que foi observado. A família é aberta; o `qualifier` a refina sem criar outra |
| `subject`, `target_ref` | o artefato e o ponto dentro dele |
| `summary` | texto curto do que está em dúvida |
| `criticality` | `low`, `medium`, `high` ou `critical`: o quanto o ponto importa |
| `confidence` | `low`, `medium` ou `high`: só registrada |
| `impact_class`, `effects` | o que a afirmação faz se estiver errada (ver Impacto de afirmações) |
| `evidence` | referência e hash da evidência que EXISTE e foi consultada; nunca o conteúdo |
| `source_need` | o que falta, por quê e que tipos de fonte poderiam trazê-lo |
| `positions` | as posições de um conflito |
| `boundary_limit` | até onde a evidência deste assunto pode ir |
| `recommended_role` | papel humano recomendado, quando conhecido |

`signal_id` deriva do conteúdo (`core/identity.py`) e é conferido ao construir e ao carregar. O sinal não carrega caminho local nem relógio.

A taxonomia padrão, em `BASE_FAMILIES`, tem onze famílias:

| Família | Natureza | Rota padrão (baixa / média / alta / crítica) |
|---|---|---|
| `LOW_CONFIDENCE` | dúvida de leitura | `SAMPLE_QA` / `SAMPLE_QA` / `AI_RECHECK` / `AI_RECHECK` |
| `AMBIGUOUS_INTERPRETATION` | dúvida de leitura | `OBSERVE` / `SAMPLE_QA` / `AI_RECHECK` / `AI_RECHECK` |
| `SELF_AUDIT_REVIEW` | a própria análise pediu conferência | `SAMPLE_QA` / `SAMPLE_QA` / `AI_RECHECK` / `AI_RECHECK` |
| `MISSING_EVIDENCE` | a evidência não existe | `OBSERVE` / `SOURCE_GAP` / `SOURCE_GAP` / `SOURCE_GAP` |
| `TRUNCATED_EVIDENCE` | a evidência foi cortada pela fonte | `OBSERVE` / `SOURCE_GAP` / `SOURCE_GAP` / `SOURCE_GAP` |
| `UNKNOWN_RELATION` | destino de relação não observado | `OBSERVE` / `OBSERVE` / `SOURCE_GAP` / `SOURCE_GAP` |
| `STRUCTURE_ONLY` | estrutura sem dados (resultado correto) | sempre `OBSERVE` |
| `CONFLICTING_EVIDENCE` | evidências se contradizem | conforme o qualificador (ver Contradições) |
| `POLICY_CONFLICT` | uma política dura impede continuar | `HUMAN_DECISION` em qualquer criticidade |
| `UNVERIFIED_HIGH_IMPACT_CLAIM` | inferência de alto impacto | `SAMPLE_QA` / `SAMPLE_QA` / `HUMAN_DECISION` / `HUMAN_DECISION` |
| `PRIVACY_OR_SECURITY_CONCERN` | privacidade ou segurança | conforme o escopo (ver Privacidade, segurança e fronteira de dados) |

Estender a taxonomia é dado, não código: uma família que a política não conhece cai na regra padrão (`default`); uma família nova ganha o seu comportamento acrescentando uma regra ao arquivo da política. Um teste garante que nenhum nome de família é constante do código de roteamento.

## Classes de roteamento

| Classe | Significado | Quem resolve | Custo humano |
|---|---|---|---|
| `AUTO_PASS` | nenhum sinal pede outra rota | ninguém | zero |
| `SAMPLE_QA` | incerteza fora do que bloqueia: elegível à amostragem de qualidade | amostra | baixo |
| `AI_RECHECK` | dúvida de LEITURA sobre evidência que existe, ainda não relida | IA | zero |
| `SOURCE_GAP` | a informação não existe na evidência disponível | backlog de aquisição de fonte | zero agora |
| `HUMAN_DECISION` | a evidência e a IA não decidem com segurança | pessoa nomeada | alto |

Cada sinal pede uma AÇÃO (`OBSERVE`, `SAMPLE_QA`, `AI_RECHECK`, `SOURCE_GAP` ou `HUMAN_DECISION`). `OBSERVE` só registra: o sinal vira secundário, sem efeito de rota. A classe da decisão é a do sinal de maior precedência.

## Precedência

A ordem entre as classes é FIXA no código (`_CLASS_OF_TIER` em `core/review_routing.py`) e a política não reordena as classes:

| Posição | Classe | Observação |
|---|---|---|
| 1 | `HUMAN_DECISION` | conflito não resolvido, política dura, preocupação específica, alto impacto não verificado, releitura que não resolveu um ponto crítico |
| 2 | `SOURCE_GAP` bloqueante | falta evidência num ponto de criticidade `blocking_from` ou acima |
| 3 | `AI_RECHECK` | só existe com evidência para reler |
| 4 | `SOURCE_GAP` não bloqueante | falta evidência num ponto menos crítico |
| 5 | `SAMPLE_QA` | incerteza fora do que bloqueia |
| 6 | `AUTO_PASS` | nenhum sinal pede outra rota |

A forma conceitual do pedido (decisão humana, lacuna crítica, releitura, incerteza não crítica, nada) foi comparada com a política validada do projeto de referência, e o resultado difere em um ponto: lá a lacuna de fonte aparece DUAS vezes, porque a lacuna num item crítico bloqueia mais que uma dúvida de leitura, e a lacuna de conteúdo bloqueia menos. A forma genérica mantém isso com `blocking_from` (padrão `high`): lacuna bloqueante passa à frente de `AI_RECHECK`; lacuna não bloqueante vem depois.

Por que fixa: uma política configurável poderia esconder uma decisão humana atrás de uma amostra, ou fazer uma lacuna virar decisão. O que a política configura é a AÇÃO de cada sinal por criticidade; um teste percorre os 31 subconjuntos não vazios dos cinco gatilhos e confere que a classe é sempre a de maior precedência, qualquer que seja a ordem dos sinais.

Os sinais que perdem não somem: ficam em `secondary_signals` (observabilidade), e os pedidos de fonte continuam sendo gerados. A releitura por IA não é planejada quando uma classe mais alta já convoca a pessoa.

## Criticidade e confiança

São eixos separados; nenhum é derivado do outro.

- Criticidade declarada: `low`, `medium`, `high`, `critical`.
- Criticidade EFETIVA: a maior entre a declarada e o piso da classe de impacto. É ela que escolhe a ação na tabela da regra.
- Confiança: registrada para observabilidade e nunca usada para decidir. Baixa confiança não é revisão humana: um `LOW_CONFIDENCE` crítico com evidência vai para `AI_RECHECK`, e sem evidência vai para `SOURCE_GAP`; nunca direto para a pessoa. Alta confiança não é segurança: um conflito direto, uma lacuna ou um conflito de política roteiam do mesmo jeito com confiança alta.
- A faixa de confiança é calculada de um número por `core/epistemics.py` (`band`), nunca armazenada no núcleo.

## Disponibilidade de evidência

A invariante central. Três situações, três destinos:

| Situação | Destino |
|---|---|
| A) a evidência existe e a leitura é incerta | candidata a `AI_RECHECK` |
| B) a evidência necessária não existe ou foi cortada | `SOURCE_GAP` |
| C) a evidência existe, se contradiz e a IA não decide com segurança | `HUMAN_DECISION` |

O roteador impõe a fronteira A/B: uma regra que pede `AI_RECHECK` para um sinal SEM evidência vira `SOURCE_GAP` (motivo `no-evidence-to-reread`), porque sem o que reler não há releitura (é a mesma recusa do pedido de `AI_RECHECK`). A decisão registra a ação que a regra pedia (`rule_action`) e a final (`action`), para o ajuste ser auditável. Evidência parcial de uma lacuna (`MISSING_EVIDENCE`, `TRUNCATED_EVIDENCE`) nunca vira releitura. E uma lacuna, mesmo de alto impacto, pede fonte, não pessoa.

## Impacto de afirmações

A Fundação registrou que as afirmações de alto impacto do projeto de referência (efeito fiscal, financeiro, exclusão, transmissão) eram marcadas à mão. Aqui o impacto é derivado de forma genérica:

1. O adaptador de domínio declara EFEITOS genéricos (`deletes-data`, `transmits-externally`, `computes-monetary-value`...) e, se quiser, uma `impact_class` explícita. Ele não decide a rota nem nomeia conceito de negócio.
2. A política mapeia efeito para classe de impacto (`impact.effects`) e ordena as classes por gravidade (`impact.order`): `informational`, `operational`, `data_integrity`, `financial`, `fiscal`, `security`, `external_transmission`, `irreversible_action`. A classe do sinal é a MAIOR entre a declarada e as derivadas. Um efeito que a política não conhece NÃO é inofensivo: cai em `default_effect_impact` (`operational`). Uma classe declarada que a política não conhece é recusada (`UNKNOWN_IMPACT`).
3. Cada classe tem um piso de criticidade (`impact.floors`, monótono com a gravidade). Uma dúvida pequena sobre uma ação irreversível pesa como crítica.
4. `ClaimAssessment` (afirmação, status epistêmico, efeitos) vira o sinal `UNVERIFIED_HIGH_IMPACT_CLAIM` quando a classe de impacto está em `impact.high_impact` e o status está em `impact.unverified_statuses` (padrão: `INFERRED`). `VALIDATED` nunca é "não verificado" (a política recusa essa configuração). O sinal é DERIVADO; ninguém o marca.

O destino de uma afirmação de alto impacto não verificada é o que a política configurar: por padrão, `HUMAN_DECISION` do tipo `validate_claim`. Trocar a política (por exemplo, tirar uma classe de `high_impact`) muda o destino sem mudar código.

## Lacuna de fonte

`SourceGapRequirement` (`schemas/source-gap-requirement.schema.json`) diz o que falta obter para fechar a dúvida:

| Campo | Conteúdo |
|---|---|
| `missing_information`, `why_needed` | a informação ausente e o motivo; vêm do `source_need` do sinal, da releitura que concluiu `SOURCE_GAP` ou de um texto genérico |
| `recommended_source_types` | tipos GENÉRICOS: `documentation`, `runtime`, `database`, `source-code`, `screenshot`, `filesystem`, `release-notes`, `human-expert`, `external-system`; sem indicação, os da política (`human-expert`) |
| `impact` | classe de impacto e criticidade efetiva |
| `blocking` | verdadeiro quando a criticidade efetiva chega a `blocking_from` |
| `origin` | `signal` ou `ai-recheck` |
| `signal_ids`, `evidence_consulted` | de onde veio |

Nomes de adaptadores e de produtos não aparecem: pertencem aos adaptadores. `source_kind_for_type` e `source_type_for_kind` convertem entre estes tipos e `common#/$defs/sourceKind` (`database` corresponde a `database-schema`; `external-system` ainda não tem tipo registrado).

O `requirement_id` não depende da decisão que o gerou: o mesmo pedido, vindo de reavaliações diferentes, tem o mesmo id e se deduplica no backlog. Aqui só se gera o artefato; não há backlog persistente.

## Decisão humana

`HumanDecisionCandidate` (`schemas/human-decision-candidate.schema.json`) é só o artefato de fronteira para a futura revisão humana:

- `decision_kind`: `resolve_conflict`, `validate_claim`, `apply_policy`, `assess_concern` ou `resolve_residue`;
- `question` e `why_ai_cannot_resolve`;
- `evidence_available`, `conflicting_claims` e `options` (as alternativas que a evidência sustenta);
- `impact`, `recommended_role` (só quando conhecido) e `attempted_before` (no vocabulário de `human-review`, por exemplo o passo `ai-recheck`);
- `prohibited_assumptions`: o que nenhuma automação pode presumir, definido pela política por tipo de decisão (por exemplo: não escolher uma das posições em conflito, não tratar a afirmação como validada, não enviar a evidência além da fronteira de dados).

Não carrega resposta, não pergunta a ninguém e não valida nada. Candidatos são agrupados por alvo e tipo. Não existe portal de revisão neste marco.

## Amostragem de qualidade

`SAMPLE_QA` é controle de qualidade por amostragem; não é insuficiência de evidência. A política define as taxas (padrão: 0,2 para `SAMPLE_QA` e 0,05 para a auditoria de `AUTO_PASS`) e uma semente.

A seleção é determinística, sem acaso e sem relógio: `selected = sample_bucket(seed, decision_id) < rate`, onde o bucket é o hash da semente com a identidade da decisão, em `[0, 1)`. Mesma decisão, mesma seleção; outra política (outra identidade) pode sortear de novo. Ser sorteado nunca muda a classe: a decisão continua `SAMPLE_QA` ou `AUTO_PASS` e só a próxima ação passa a `SAMPLE_REVIEW`. As taxas são configuração operacional, não resultado estatístico validado. As demais classes não são amostradas (a pessoa decide 100%, a IA relê 100%, a lacuna vira backlog).

## AUTO_PASS não é validação

`AUTO_PASS` não é `VALIDATED`. Significa: nenhum sinal pede outra rota. A classe nunca promove conhecimento: toda decisão grava `validation_effect = NONE` (constante no schema, conferida ao carregar), e nenhum campo da decisão pode guardar um validador. O teto epistêmico do `AI_RECHECK` continua: todo achado é `INFERRED`, e um `RESOLVED` apenas trata o sinal que pediu a releitura.

## Contradições

Nem toda contradição pede pessoa. Casos genéricos, via `qualifier` de `CONFLICTING_EVIDENCE` (e `POLICY_CONFLICT` para política dura):

| Caso | Qualificador | Rota padrão (baixa / média / alta / crítica) |
|---|---|---|
| tensão compatível | `compatible_tension` | `OBSERVE` / `OBSERVE` / `SAMPLE_QA` / `SAMPLE_QA` |
| ambiguidade resolvível | `resolvable_ambiguity` | `OBSERVE` / `SAMPLE_QA` / `AI_RECHECK` / `AI_RECHECK` |
| conflito direto de evidências | `direct_conflict` (também o padrão sem qualificador) | `SAMPLE_QA` / `HUMAN_DECISION` / `HUMAN_DECISION` / `HUMAN_DECISION` |
| pessoa nomeada x observado | `human_vs_observed` | `HUMAN_DECISION` em qualquer criticidade |
| conflito com política | família `POLICY_CONFLICT` | `HUMAN_DECISION` em qualquer criticidade |

Um qualificador que a política não conhece cai na regra da família. As posições do conflito ficam no candidato; nada é descartado.

## Privacidade, segurança e fronteira de dados

Sem construir conformidade, o roteamento preserva a capacidade de rotear com segurança:

- `PRIVACY_OR_SECURITY_CONCERN:subject_specific`: preocupação específica deste assunto; `HUMAN_DECISION` (`assess_concern`) em qualquer criticidade. Sem qualificador, o caso conservador: específica, a partir de `medium`.
- `PRIVACY_OR_SECURITY_CONCERN:systemic_handled`: tratada uma vez no nível do acervo; só observada. Nunca é gatilho por assunto.
- `boundary_limit` (`LOCAL`, `PRIVATE` ou `EXTERNAL`) em qualquer sinal restringe até onde a evidência pode ir. A mais estrita entre os sinais vira `constraints.max_boundary` da decisão e limita o `max_boundary` do plano de releitura. A fronteira só APERTA: o padrão da política é `LOCAL`; um limite `EXTERNAL` num sinal não afrouxa; o teto do ambiente passado à ponte também só aperta.
- A elegibilidade do adaptador continua sendo do executor (`core/model_adapter.py`): capacidade ausente, fronteira excedida e evidência sensível ou não conferida com adaptador externo recusam a execução, sem chamar o modelo. Isso não é um sistema de DLP.

## Política como dado e versionamento

`policies/review-routing.policy.json` é a política padrão, validada por `core/review_policy.py` (`ReviewPolicy`) ao carregar. Qualquer violação recusa a política inteira:

- forma estrita (chave desconhecida ou ausente);
- monotonia: criticidade maior nunca recebe ação MENOS severa (`OBSERVE < SAMPLE_QA < AI_RECHECK = SOURCE_GAP < HUMAN_DECISION`); o mesmo vale para os pisos de impacto e para a escalada de releitura inconclusiva;
- uma regra que pode pedir `HUMAN_DECISION` declara o `decision_kind`;
- a escalada de uma releitura inconclusiva nunca é `AI_RECHECK` (laço) nem `OBSERVE` (esconderia a falta de conclusão);
- `max_rounds` de 1 a 5, fronteira e risco em vocabulário fechado, taxas de 0 a 1, tipos de fonte genéricos, suposições proibidas para todos os tipos de decisão.

Identidade: `policy.ref()` devolve `{policy_id, version, digest}`; o `digest` é o hash canônico de TODO o conteúdo, então mudar a política (mesmo sem trocar a versão) muda a referência. Toda decisão grava essa referência e ela entra na identidade da decisão. Portanto: outra versão ou outro conteúdo, outro `decision_id`; `ReviewRouter.is_current` diz se uma decisão foi calculada com esta política; `reproduces` recalcula a decisão dos mesmos sinais e confere. Para o motor de estágios, `policy.stage_parameters()` entra na `RunConfig` do estágio de roteamento, e trocar a política refaz o estágio (`CONFIG_CHANGED`), como já ocorre com o adaptador de modelo.

## Decisão de revisão

`ReviewDecision` (`schemas/review-decision.schema.json`) é um dado validado e imutável. Construir confere o schema, a identidade e a coerência interna; nada inválido existe.

| Campo | Conteúdo |
|---|---|
| `decision_class`, `subject` | a classe e o assunto |
| `signals`, `assessments` | os sinais considerados e, para cada um, a regra que valeu (`rule`), a criticidade declarada e a efetiva, a ação pedida (`rule_action`) e a final (`action`), `blocking`, o motivo do ajuste (`why`) e o papel (`primary`, `secondary` ou `addressed`) |
| `primary_reasons`, `secondary_signals` | o que definiu a classe e o que ficou observável |
| `requires_named_human` | verdadeiro exatamente em `HUMAN_DECISION` |
| `next_action` | `PROCEED`, `SAMPLE_REVIEW`, `RUN_AI_RECHECK`, `ACQUIRE_SOURCE` ou `REQUEST_HUMAN_DECISION` |
| `policy`, `engine` | a referência da política e a versão do motor (`review-router/1`) |
| `evidence_snapshot` | a evidência consultada, por referência e hash, com o digest |
| `recheck_history` | releituras por IA concluídas, que limitam o laço |
| `validation_effect` | sempre `NONE` |
| `constraints`, `recheck_plan`, `source_gap_requirements`, `human_decision_candidates`, `quality_sampling` | conforme a classe |

Códigos de `why`: `rule` (a política mandou), `no-evidence-to-reread`, `recheck-resolved`, `recheck-source-gap`, `recheck-human-decision`, `recheck-inconclusive` e `recheck-another-round`.

`decision_id` deriva do assunto, da referência da política, dos ids dos sinais, do histórico de releitura e da versão do motor; a amostragem e a próxima ação são derivadas dele e também conferidas ao carregar. Mesmos insumos e mesma política, mesma decisão, em qualquer processo e com qualquer semente de hash do interpretador (há teste com outro processo).

## Integração com AI_RECHECK

Quando a classe é `AI_RECHECK`, a decisão carrega um `recheck_plan` (assunto, afirmações em dúvida, evidência por referência e hash, rodada, `ambiguity`, `reason`, fronteira e risco). `core/review_recheck.py` faz as duas setas:

    sinais -> ReviewRouter -> ReviewDecision(AI_RECHECK) --build_recheck_request--> AIRecheckRequest
                                                                                         |
                                                                   executor da Extração 3 (run_recheck / AIRecheckStage)
                                                                                         |
    nova ReviewDecision <--follow_up-- AIRecheckResult <---------------------------------+

Ida: `build_recheck_request` monta o `AIRecheckRequest` a partir do plano e de um catálogo de evidência autorizada fornecido por quem chama; cada hash precisa conferir com o do plano (`EVIDENCE_CHANGED`) e cada referência precisa existir (`EVIDENCE_NOT_AUTHORIZED`). Só uma decisão `AI_RECHECK` gera pedido (`NOT_A_RECHECK`). Nada é executado. `executor_policy` alinha a `RecheckPolicy` do executor à política de roteamento (mesmas rodadas, mesmo teto de fronteira).

Volta: `follow_up` confere que o pedido é o do plano e que o resultado é daquele pedido, transforma o resultado em registros de releitura (`RecheckRecord`) e reavalia a decisão (`ReviewFollowUp.kind = REEVALUATED`, com a decisão nova e os registros aplicados):

| Estado do resultado | O que acontece |
|---|---|
| `RESOLVED` | os sinais que pediram a releitura ficam TRATADOS (`addressed`) e o que sobrar é roteado normalmente |
| `SOURCE_GAP` | o ponto vira lacuna de fonte (`origin = ai-recheck`) com o que falta e o tipo de fonte indicado; continua `SOURCE_GAP` mesmo em ponto não bloqueante |
| `HUMAN_DECISION` | o ponto vira candidato (`resolve_conflict`) com a pergunta e as alternativas do modelo |
| `INCONCLUSIVE` | com rodadas sobrando, nova rodada (`recheck-another-round`); sem rodadas, a escalada de `recheck.on_inconclusive` por criticidade efetiva (padrão: `HUMAN_DECISION` do tipo `resolve_residue` de `high` para cima). Parte resolvida dentro de um `INCONCLUSIVE` é tratada; só o resto escala |
| `FAILED` | falha TÉCNICA: nenhuma decisão nova (`ReviewFollowUp.kind = TECHNICAL_FAILURE`), nenhum registro, nenhuma rodada gasta |

Laço limitado: um registro vale para um alvo enquanto a evidência atual do alvo estiver contida na relida. Existe um orçamento de `recheck.max_rounds` (padrão 1) por alvo e evidência; um desfecho definitivo (`RESOLVED`, `SOURCE_GAP`, `HUMAN_DECISION`) encerra o laço de uma vez, e `INCONCLUSIVE` nunca roteia de volta para `AI_RECHECK` depois da última rodada. Evidência nova ou alterada reabre o direito a uma releitura. Se o orçamento do roteamento for maior que o do executor, a rodada extra falha com `ROUND_LIMIT` (não repetível) e vira `TECHNICAL_FAILURE`.

## Semântica de repetição

A Extração 3 deixou uma limitação registrada: o motor de estágios repetia toda falha técnica até `max_attempts`, ignorando `result.retryable`. Corrigido aqui, sem tocar na semântica epistêmica.

- `StageFailure(message, category, retryable=True)`: o tratador que SABE que a falha é determinística levanta `retryable=False`. Qualquer outra exceção segue repetível.
- `FailureInfo.retryable` (padrão `True`) é gravado no estado SOMENTE quando é `false`; o formato anterior fica idêntico byte a byte (`schemas/run-state.schema.json` aceita `retryable` só como `false`).
- `core/resume.py`: depois da checagem de "as entradas mudaram desde a falha", uma falha `retryable=False` fica `FAILED` na hora (`EXHAUSTED`, motivo `NON_RETRYABLE_FAILURE`) e a retomada não gasta outra tentativa. Entradas novas, ou um adaptador ou política diferentes, recomeçam a contagem, como antes.
- `core/recheck_stage.py` repassa o `retryable` que o executor decidiu. Não repetíveis: `ROUND_LIMIT`, `ADAPTER_INELIGIBLE` (capacidade ausente, fronteira excedida, evidência sensível com adaptador externo), `EVIDENCE_CHANGED`, `EVIDENCE_UNDECODABLE`, recusa do modelo, pedido inválido e parâmetros do estágio que não declaram o adaptador (configuração estática malformada). Repetíveis: tempo esgotado, indisponibilidade, `EVIDENCE_UNAVAILABLE`.
- Uma falha técnica não gasta o orçamento epistêmico. Repetir por falha técnica (motor de estágios, `RetryPolicy`) e repetir a análise por dúvida (rodadas do roteamento) são coisas diferentes: um recheck válido, mesmo `INCONCLUSIVE`, nunca é repetido pelo motor.

Regressão em `tests/test_retryable_semantics.py`: o repetível repete; o não repetível para na primeira tentativa (rejeição da política de provedor, capacidade ausente, fronteira excedida, configuração malformada, limite de rodadas); o tempo esgotado temporário ainda repete; uma exceção comum de tratador segue repetível; entradas novas dão vida nova; o formato gravado não muda no caso padrão.

## Relação com a escada da Fundação

`core/escalation.py` (`decide`) continua como estava: uma escada de um item, com as quatro rotas da Fundação, incluindo `HUMAN_VALIDATION`. O roteamento por sinais é uma generalização sobre ela, não uma duplicata. Nas situações que ambos conhecem o destino coincide (há teste): sem evidência é `SOURCE_GAP`; confiança baixa com evidência é `AI_RECHECK`; mais de uma alternativa é `HUMAN_DECISION`; releitura feita e não resolvida escala para pessoa. A diferença de vocabulário: o que a Fundação chama `HUMAN_VALIDATION` (confirmar uma inferência de alto impacto) é aqui `HUMAN_DECISION` do tipo `validate_claim`. `core/ai_recheck.route_for` continua usando a escada da Fundação para o `INCONCLUSIVE` de um resultado isolado; dentro do roteamento por sinais, quem decide a escalada é a política (`recheck.on_inconclusive`).

## Compatibilidade de runtime

| Item | Valor |
|---|---|
| Mínimo pretendido | Python 3.10 (o mesmo do projeto) |
| Auditoria de sintaxe | os módulos novos e os alterados são analisados com `ast.parse(feature_version=(3, 9))`; é verificação de sintaxe, não execução |
| APIs | revisadas e varridas por nome contra APIs posteriores ao 3.9 (lista parcial); anotações são lazy (`from __future__ import annotations`) |
| Versões efetivamente testadas | somente Python 3.15.0rc2, no Windows. É uma versão candidata a lançamento (RC), a única instalada |
| Linux e macOS | a suíte não foi executada em Linux nem em macOS |
| Outras versões do Python | não havia outra instalação local; nada foi instalado. A compatibilidade com 3.10 a 3.14 não foi testada em execução |

## Limitações conhecidas

- Não há emissor de sinais. Quem transforma uma análise de código, de banco ou de documentação em `AnalysisSignal` é o adaptador de domínio, que ainda não existe. A suíte prova o roteamento com sinais sintéticos de dois softwares sem relação.
- Não há portal nem fluxo de revisão humana, e nada ingere a resposta de uma pessoa. O `HumanDecisionCandidate` é só a fronteira.
- Não persiste: decisões, requisitos de fonte e candidatos são artefatos em memória que um estágio pode gravar como saída. Não há backlog nem fila de candidatos em disco, e a deduplicação é por identidade, sem armazenamento.
- A política padrão e as taxas de amostragem são um ponto de partida razoável, não calibrado. Não houve medição de acerto, nem auditoria de amostra, nem ajuste por resultado; nenhuma das taxas é verdade estatística.
- Os mapas de efeitos para classes de impacto cobrem um vocabulário pequeno e genérico. Efeitos que o adaptador de domínio inventar caem no padrão conservador (`operational`) até a política ser estendida; a derivação não adivinha o efeito de uma afirmação, só usa os efeitos declarados.
- Um sinal só vale para o que foi emitido. Conhecimento já `VALIDATED` por pessoa não deve gerar sinais de incerteza; o roteador não consulta o status de um sinal comum (só o de um `ClaimAssessment`).
- A deduplicação de pedidos de fonte e de candidatos é pela identidade do conteúdo; não há agrupamento semântico de perguntas parecidas.
- O ajuste de uma releitura inconclusiva usa a criticidade efetiva dos sinais que pediram a releitura. Uma política que mande `INCONCLUSIVE` para `SAMPLE_QA` em ponto crítico é válida e é responsabilidade de quem a escreve.
- O roteamento não executa o recheck e não repete falha técnica; quem repete é o motor de estágios, e só quando `retryable` for verdadeiro.

## Fora do escopo

Por decisão, não foram implementados: interface de revisão humana, portal de perguntas, ingestão de resposta humana, implementação do adaptador de DokuWiki, observador de mudanças (daemon), a etapa K4.3 do SYSCOM, qualquer adaptador real de provedor de modelo, chamada de API de modelo, analisador de telas, engenharia reversa de banco e persistência do backlog de fontes.

## Rastreabilidade

Cada capacidade extraída tem origem, classificação, invariante genérico, destino, acoplamento rejeitado e testes registrados em [`extraction/EXTRACTION_004.json`](extraction/EXTRACTION_004.json) (`docs/extraction/EXTRACTION_004.json`), junto com a correção do motor de estágios, o que foi adiado (e para onde), o que foi rejeitado (e por quê) e a lista do que não foi extraído. Testes verificam que o arquivo é consistente com o código: os destinos e os símbolos existem, os testes citados existem, todo módulo, schema e a política estão mapeados e o artefato não carrega caminho local.
