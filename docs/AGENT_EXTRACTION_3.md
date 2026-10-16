# Agent Extraction 3: execução genérica de AI_RECHECK

Terceira extração de capacidades comprovadas no SYSCOM para o Agent Core, feita contra os contratos da Foundation e das extrações [1](AGENT_EXTRACTION_1.md) e [2](AGENT_EXTRACTION_2.md), sem recriar a arquitetura do projeto de origem.

Escopo: transformar `AI_RECHECK` de uma fila e de um conceito em uma capacidade executável, genérica e independente de provedor. O agente passa a saber O QUE rechecar, com que evidência AUTORIZADA, que forma a resposta deve ter, se a resposta é aceitável e para onde ela segue. A única parte que fala com um modelo é um adaptador substituível; o núcleo não conhece SDK, rede, credencial, provedor nem software.

## Definição de AI_RECHECK

`AI_RECHECK` é uma SEGUNDA análise focada, por IA, de evidência JÁ disponível, usada quando uma ambiguidade pode ser resolvida sem coletar fonte nova, sem perguntar a uma pessoa e sem alterar o sistema de origem.

```
AMBIGUIDADE -> AI_RECHECK -> tarefa focada de modelo -> RESOLVED | SOURCE_GAP | HUMAN_DECISION | INCONCLUSIVE | FAILED
```

O que pode fazer: reler a evidência autorizada, resolver as afirmações em dúvida com achados citáveis, dizer que falta fonte (e qual), dizer que a escolha não é da evidência (e quais alternativas ela sustenta), ou admitir que não conclui.

O que NÃO pode fazer:

- usar evidência que o pedido não autorizou (nem "só um pouco", nem de fonte externa);
- coletar fonte nova, abrir o sistema de origem ou escrever fora da raiz de saídas;
- perguntar a uma pessoa. Quando a rota é humana, entrega um CANDIDATO; a capacidade de revisão humana (futura) decide como perguntar;
- criar conhecimento `VALIDATED`, nem promover um nível porque uma segunda passada concordou com a primeira;
- repetir a si mesmo. Cada pedido é rechecado uma vez; `INCONCLUSIVE` nunca roteia de volta para `AI_RECHECK`.

## Fontes estudadas

O `mr-syscom` foi tratado como somente leitura. Os quatro arquivos abaixo foram lidos sem importá-los nem executá-los de dentro do workspace de origem.

| Arquivo (commit `4c652a3`) | Blob | O que contribuiu |
|---|---|---|
| `tools/review/derive_review_class.py` | `054cc76a81d78746673ed206e7f482201fe74118` | árvore de decisão da classe de revisão; `AI_RECHECK` só para dúvida de leitura em item crítico; limite físico da evidência é `SOURCE_GAP`; recheck concluído é "resolvido ou escalado, nunca repetido"; item crítico não resolvido vira `HUMAN_DECISION` |
| `tools/batch/batch_run.py` | `f1457ba2ffb9dc838d0e790acbb2397c2ebe8186` | `op_review` registra `ai_recheck = {status: pending, unresolved_critical_items: []}` e `op_recheck` registra o resultado À MÃO: há estado e registro, não há executor |
| `tools/batch/k4_2_review.py` | `bb2144f2c3b683c41b4fa5dec0ba18f4088a00db` | política SF6: "`AI_RECHECK` exige evidência que possa conter a resposta"; sem ela a classe cai para `SOURCE_GAP` |
| `tools/batch/k4_2_correction.py` | `f89f1571a33afdd7c62c0015b6e351c0ba09abb7` | `ai_recheck_queue` com `executed: False` e a lista de evidências existentes por item: uma fila preservada e nunca executada |

O workspace de referência tinha alterações locais não relacionadas (arquivos modificados e não rastreados) antes e depois desta extração, e os quatro arquivos eram idênticos ao commit. O estado completo foi registrado no início e comparado no fim; nada foi alterado.

Achado principal: o SYSCOM tem a fila, o estado e o registro de `AI_RECHECK`, mas nenhum executor. O resultado é digitado por quem opera o lote. Esta extração entrega o executor; não copia o mecanismo antigo.

## Invariantes genéricos extraídos

1. `AI_RECHECK` trata dúvida de LEITURA ou de interpretação sobre evidência que existe. Quando a informação não está na evidência (limite físico, ilegível, ausente), a rota é `SOURCE_GAP`, não recheck.
2. Um recheck precisa de evidência que possa conter a resposta; sem evidência, não há o que reler.
3. Um recheck acontece uma vez: concluído, ele resolve ou escala; nunca se repete em laço.
4. Um recheck que não resolve um item crítico escala para uma rota humana; não volta para a IA.
5. A classificação é função pura: mesma entrada, mesma saída.
6. Rotear não promove nada: a classificação nunca preenche quem validou, não fecha conflito e não autoriza publicação.

## Acoplamento do SYSCOM rejeitado

Resumo; o detalhe, item a item, está em [`extraction/EXTRACTION_003.json`](extraction/EXTRACTION_003.json).

- Nenhum nome de tela, id, aba, grid, botão, caminho, fase ou contagem fixa entrou no núcleo. Um teste varre os arquivos novos atrás desses termos, dos termos de vazamento de todos os pacotes e de nomes de provedores de modelo.
- A unidade de trabalho "tela" virou um assunto genérico (`subject.kind` aberto: código, documentação, esquema de banco, rastro de execução, API, configuração, captura de tela).
- O vocabulário de "item crítico" e de "flags secundárias" (`critical_item_review`, `tab_label_truncated`...) não foi levado; o pedido carrega as afirmações em dúvida como dados.
- O resultado digitado por operador (`op_recheck --status`) foi substituído por um resultado produzido e validado pelo executor.
- A fila `ai_recheck_queue` com `executed: False` não foi copiada: um pedido é um artefato com identidade e é executado por um estágio.
- Data de relógio no estado (`at`) e a decisão "recheck esgotado" guardada como lista solta de ids não foram levadas.

## Fronteira do adaptador de modelo

`core/model_adapter.py`, `core/model_task.py` e `schemas/model-task.schema.json`, `schemas/model-result.schema.json`.

O contrato mínimo é `execute(task) -> ModelResult`, mais um `descriptor`. Nada além disto é exigido: nenhum SDK, rede, credencial ou contagem de tokens.

| Quem decide | O quê |
|---|---|
| O motor (agente) | o que rechecar; que evidência pode ser usada; que forma a resposta deve ter; se a resposta é aceitável; para onde segue |
| O adaptador | enviar a tarefa estruturada a um modelo capaz e devolver uma resposta estruturada (ou uma falha) |

`AdapterDescriptor`: `adapter_id`, `adapter_version`, `capabilities`, `data_boundary`, `deterministic` e, opcionalmente, `model_id`, `config_fingerprint` (hash da configuração; a configuração em si nunca é guardada) e `profile_id` (referência ao perfil de ambiente em `adapters/llm/`).

`ModelResult`: ou `output` (JSON canônico) ou `failure` (`TIMEOUT`, `UNAVAILABLE`, `REFUSED`, `ERROR`, com `retryable`), mais a identidade do modelo que respondeu e `usage` opcional. O adaptador é, para o núcleo, código de terceiros: o que ele devolve é validado, nunca confiado.

`adapters/models/base.py` traz uma base opcional (`BaseModelAdapter`) que normaliza o retorno e traduz tempo esgotado e indisponibilidade em falhas repetíveis sem copiar a mensagem da exceção. `adapters/models/scripted.py` traz os adaptadores sintéticos e determinísticos de teste.

## Tarefa de modelo

`ModelTask` carrega `task_type`, `objective`, `instructions`, `evidence_refs`, `allowed_context`, `output_contract`, `required_capabilities`, `max_boundary`, `risk_level` e `attempt`. As instruções são frases curtas e genéricas, em dados; nenhum prompt de provedor mora no núcleo.

- `task_id` é uma `artifact_identity` de tipo `model.task`, calculada do que importa: o hash de cada evidência, seus metadados, as instruções, o contrato, as capacidades, a fronteira e o contexto SEM o conteúdo bruto (que já está preso pelo hash). Não depende de relógio, máquina, caminho, tentativa, provedor ou modelo.
- O conteúdo de cada evidência vai em `allowed_context.evidence` e é conferido contra o hash de `evidence_refs`. Uma tarefa não consegue carregar conteúdo de evidência não autorizada, nem conteúdo diferente do hash.
- Texto vai em UTF-8; imagem e binário, em base64. A modalidade `image` impõe a capacidade `vision`.
- A tarefa é transitória: o agente a entrega ao adaptador e não a grava.

## Pedido

`core/ai_recheck.py` (`AIRecheckRequest`) e `schemas/ai-recheck-request.schema.json`. O pedido identifica:

| Campo | Papel |
|---|---|
| `subject` (`ref`, `kind`, `title`) | o artefato em dúvida; `kind` é aberto |
| `ambiguity` | o que está ambíguo |
| `claims` | as afirmações ou itens críticos em dúvida; cada uma precisa ser tratada pela resposta |
| `evidence` | a evidência AUTORIZADA, presa por hash, com tipo de fonte, modalidade, sensibilidade e `locator` |
| `prior_interpretation` | a interpretação existente (`OBSERVED` ou `INFERRED`; nunca `VALIDATED`) |
| `reason` | por que rechecar |
| `expected_output` | o contrato de saída exigido do modelo |
| `required_capabilities`, `data_policy`, `risk_level`, `round` | exigências sobre o adaptador, até onde o dado pode ir, risco e a passada |

`request_id` deriva do conteúdo (`art:ai-recheck.request:...`) e é conferido ao construir e ao carregar. O pedido não carrega o conteúdo das evidências nem caminho local. Um pedido sem evidência é recusado: sem o que reler, a rota é `SOURCE_GAP`.

## Resultado e estados

`AIRecheckResult` e `schemas/ai-recheck-result.schema.json`. Vocabulário mínimo, cinco estados:

| Estado | Significa | Quem pode afirmá-lo |
|---|---|---|
| `RESOLVED` | todas as afirmações têm achado com evidência citada | o modelo |
| `SOURCE_GAP` | a evidência existente não basta; registra o que falta, por quê e o tipo de fonte, se souber | o modelo |
| `HUMAN_DECISION` | a evidência sustenta alternativas e não escolhe; registra a pergunta e as opções | o modelo |
| `INCONCLUSIVE` | resultado válido sem conclusão; lista as afirmações não resolvidas | o modelo, ou o executor ao rebaixar um `RESOLVED` fraco |
| `FAILED` | falha técnica; não é conhecimento | só o executor |

Um `RESOLVED` carrega: os achados (afirmação, texto, `evidence_refs`, `confidence`, `basis`), `evidence_used`, uma justificativa curta e auditável, a confiança (a MENOR entre os achados) e a proveniência. Raciocínio interno não é guardado, nem o conteúdo bruto das evidências.

Falhas do executor (`FAILED`). O tipo é fechado e nenhum deles é conhecimento. Quando o recheck roda como estágio, a falha vira `StageFailure` com a categoria do motor de estágios da tabela:

| Tipo | Quando | `retryable` | Categoria no estágio |
|---|---|---|---|
| `ROUND_LIMIT` | o pedido é uma passada acima de `max_rounds` | não | `HANDLER_ERROR` |
| `ADAPTER_INELIGIBLE` | capacidade ausente, fronteira de dados excedida ou evidência sensível a um adaptador externo | não | `HANDLER_ERROR` |
| `EVIDENCE_UNAVAILABLE` | a evidência autorizada não pôde ser lida | sim | `INPUT_UNAVAILABLE` |
| `EVIDENCE_CHANGED` | o conteúdo atual difere do hash do pedido | não | `INPUT_MUTATED` |
| `EVIDENCE_UNDECODABLE` | evidência de texto que não é UTF-8 | não | `INPUT_UNAVAILABLE` |
| `ADAPTER_FAILURE` | o adaptador devolveu uma falha técnica (`TIMEOUT`, `UNAVAILABLE`, `REFUSED`, `ERROR`) | o que o adaptador informar | `HANDLER_ERROR` |
| `ADAPTER_EXCEPTION` | o adaptador levantou uma exceção | só para tempo esgotado e rede | `HANDLER_ERROR` |
| `MALFORMED_RESPONSE` | resposta fora do contrato, de outra tarefa ou que não é um `ModelResult` | só se o adaptador não é determinístico | `HANDLER_ERROR` |
| `UNAUTHORIZED_EVIDENCE` | a resposta cita evidência que o pedido não autorizou | só se o adaptador não é determinístico | `HANDLER_ERROR` |

`result_id` deriva do corpo aceito (sem rota nem proveniência) e é conferido ao carregar: quem produziu, quando e com qual modelo é metadado ao lado do artefato, nunca dentro da identidade.

## Regras de evidência

1. Só a evidência listada no pedido pode ser citada. Uma referência fora da lista, em `evidence_used` ou em qualquer achado, invalida a resposta INTEIRA (`UNAUTHORIZED_EVIDENCE`). Nada é aceito pela metade, porque um modelo que cita o que não foi autorizado mostra que usou o que não devia.
2. O conteúdo ATUAL de cada evidência é lido e conferido contra o hash do pedido antes de chamar o modelo. Diferente: `EVIDENCE_CHANGED`; ilegível: `EVIDENCE_UNAVAILABLE`.
3. O modelo só recebe o conteúdo autorizado e conferido. A tarefa não carrega o `locator`.
4. Como estágio, a evidência é lida pelo `ctx` do orquestrador: só entrada declarada do estágio ou saída de dependência direta. Por construção ela entra no checkpoint, e mudar a evidência invalida o recheck.

## Validação da resposta

`validate_model_output` é determinística e não tem efeito colateral. A resposta do modelo nunca é confiada:

- schema estrito (`$defs/modelOutput`): nenhum campo desconhecido; campos exigidos por estado; `RESOLVED` sem `unresolved`; `SOURCE_GAP` e `HUMAN_DECISION` sem achados; `HUMAN_DECISION` com ao menos duas opções distintas;
- confiança numérica de 0 a 1 (booleano, texto e fora do intervalo são recusados);
- referências de evidência só as autorizadas;
- cobertura: `RESOLVED` trata todas as afirmações; `INCONCLUSIVE` acomoda todas (resolvidas ou listadas); nenhuma afirmação desconhecida ou duplicada;
- nenhum texto que pareça caminho local;
- justificativa não vazia e curta.

Resposta malformada vira `FAILED` (`MALFORMED_RESPONSE`); com referência não autorizada, `UNAUTHORIZED_EVIDENCE`. Nunca vira conhecimento. O resultado montado ainda passa de novo pelo schema e por regras entre campos (identidade, rota coerente com o estado, confiança, teto epistêmico).

## Teto epistêmico

`AI_RECHECK` nunca cria validação humana. Todo achado aceito é `INFERRED`:

- o modelo não tem campo para escolher status; `status`, `validation` ou `validated_by` na resposta são campos desconhecidos e a recusam;
- o executor grava `status: INFERRED` e o schema do resultado o fixa (`const`); cada achado ainda passa por `core.epistemics.check_epistemic`;
- uma interpretação anterior `OBSERVED` não é promovida, nem uma interpretação `INFERRED` é promovida porque a segunda passada concordou;
- um pedido cuja interpretação existente é `VALIDATED` é recusado (`VALIDATED_PRIOR`);
- a proveniência registra `epistemic_ceiling: INFERRED`.

## Roteamento

`route_for` é pura. A rota vai dentro do resultado (`routing`); quem consome decide o que fazer. `proceeds` só é verdadeiro para `REUSE`.

| Estado | Rota | Destino | Continua? |
|---|---|---|---|
| `RESOLVED` | `REUSE` | `ai_result` | sim |
| `SOURCE_GAP` | `SOURCE_GAP` | `source_backlog` | não |
| `HUMAN_DECISION` | `HUMAN_DECISION` | `human_review_candidates` | não |
| `INCONCLUSIVE` | contingência da política | `source_backlog` ou `human_review_candidates` | não |
| `FAILED` | `FAILURE` | `failure_handling` | não |

Contingência de `INCONCLUSIVE`: a política (`RecheckPolicy.inconclusive_route`) escolhe entre `SOURCE_GAP`, `HUMAN_DECISION` e `HUMAN_VALIDATION`. Sem escolha, vale a escada da Foundation (`core.escalation.decide`, com recheck feito e não resolvido): `HUMAN_VALIDATION` se há uma interpretação a confirmar; senão `SOURCE_GAP`. `AI_RECHECK` não é uma contingência válida (`RECHECK_LOOP`).

Duas visões puras para quem consome a rota: `source_gap_entry` (o que falta, por que a evidência não basta, o tipo de fonte recomendado) e `human_review_candidate` (registro `OPEN` com pergunta, opções, hipótese e o que a IA já tentou, no formato do passo `ai-recheck` de `attempted_before`). Nenhuma delas pergunta nem responde por ninguém.

## Elegibilidade do adaptador: capacidades e fronteira de dados

`check_eligibility` roda ANTES de ler a evidência e ANTES de chamar o adaptador. Um adaptador inelegível não é chamado, e o dado não chega a ser lido (`ADAPTER_INELIGIBLE`, com os códigos de recusa).

- Capacidades: o vocabulário reaproveita `vision`, `structured-output` e `long-context` da Foundation e acrescenta `tool-use` (só para modelos). O executor sempre exige `structured-output`; a modalidade `image` impõe `vision`; o pedido pode exigir qualquer uma. Não há lista de provedores nem de modelos.
- Fronteira de dados, em ordem de exposição: `LOCAL` (não sai do ambiente), `PRIVATE` (rede controlada por quem usa), `EXTERNAL` (terceiro). O adaptador declara onde o dado vai; o pedido declara até onde pode ir (campo obrigatório: quem pede decide, o núcleo não presume); o ambiente pode apertar por cima (`RecheckPolicy.boundary_ceiling`). Vale o menor.
- Sensibilidade: `contains_sensitive_data` é tri-estado, como em `source`. Só `false` explícito prova que a evidência não é sensível. Evidência sensível ou não conferida (`null`) nunca vai a um adaptador `EXTERNAL`.

Isto não é um sistema de DLP: é a estrutura mínima para que um ambiente diga "esta fonte não pode ir a um provedor externo".

## Semântica de repetição

Falha técnica e `INCONCLUSIVE` válido são coisas diferentes e não se confundem:

| Situação | O que é | O que acontece |
|---|---|---|
| Falha do adaptador, exceção, resposta malformada, evidência indisponível | técnica; `FAILED` | o estágio falha e o motor de estágios repete conforme a `RetryPolicy`, em uma NOVA retomada, nunca em laço interno; esgotadas as tentativas, o estágio fica `FAILED` e o adaptador não é chamado de novo |
| `INCONCLUSIVE`, `SOURCE_GAP`, `HUMAN_DECISION` | epistêmica; resultado válido | o estágio conclui (`COMPLETED`), o checkpoint sela, e NADA é repetido |

`retryable` no resultado `FAILED` diz se uma nova tentativa com a mesma entrada poderia dar certo: sim para tempo esgotado e indisponibilidade; para resposta malformada, só se o adaptador não é determinístico (um adaptador determinístico responderia igual). Desde a Extração 4 o motor de estágios respeita esse sinal: uma falha `retryable` falsa fica `FAILED` na primeira tentativa e a retomada não gasta outra (ver [`AGENT_EXTRACTION_4.md`](AGENT_EXTRACTION_4.md), seção Semântica de repetição). Quando a Extração 3 foi entregue, o motor repetia até esgotar a política sem consultar esse sinal.

O executor nunca chama o adaptador duas vezes por conta própria. Não há espera nem recuo exponencial.

## Estágio de orquestração

`core/recheck_stage.py`. `AIRecheckStage` é um tratador comum do orquestrador da Extração 2; não é outro framework. O estágio:

1. confere que os PARÂMETROS do estágio declaram este adaptador e esta política (`stage_parameters()`). Assim trocar de adaptador, de versão, de modelo ou de política invalida o recheck já concluído (`CONFIG_CHANGED`), em vez de pular em silêncio o resultado de outro modelo;
2. carrega o pedido (de uma entrada declarada ou da saída de uma dependência) e o confere;
3. lê a evidência pelo `ctx`; mudança durante o estágio é `INPUT_MUTATED`, evidência não declarada é `INPUT_UNAVAILABLE`;
4. chama `run_recheck`; falha técnica vira `StageFailure`; resultado válido é gravado como saída declarada, de forma atômica, e o orquestrador grava o checkpoint.

Retomada: segunda execução pula tudo e não chama o modelo; evidência mudada refaz análise, recheck e dependentes; adaptador ou política diferentes refazem só o recheck e dependentes; estágio interrompido (`KeyboardInterrupt` dentro do adaptador) é recolocado na fila e a tentativa é devolvida; saída adulterada invalida o estágio.

## Trilha de auditoria e determinismo

A proveniência de cada resultado registra: o executor (`ai-recheck/1`), a identidade da tarefa (`task_id`), o adaptador (id, versão, modelo, impressão digital da configuração, capacidades, fronteira, determinismo, perfil), o modelo que de fato respondeu, o hash SHA-256 da resposta canônica, o uso (quando conhecido), o snapshot da evidência (ref, hash e tamanho) e seu hash, se a evidência foi verificada, a tentativa, a passada, a reprodutibilidade e o teto epistêmico. Não há relógio, máquina nem caminho local.

O modelo real não é determinístico, mas a orquestração e a identidade dos artefatos são auditáveis. `reproducibility` só diz `DETERMINISTIC_ADAPTER` quando o adaptador se declara determinístico; caso contrário, `NOT_GUARANTEED`. Com um adaptador determinístico, a mesma entrada produz o mesmo resultado, byte a byte, em qualquer pasta ou máquina.

O mesmo pedido, por dois adaptadores de identidades diferentes, produz achados idênticos e o mesmo `result_id`; só a proveniência difere. O motor não muda.

## Perfis de provedor e fronteiras futuras

Os perfis de ambiente continuam em `adapters/llm/` (a única camada que pode nomear um provedor). `AdapterDescriptor.profile_id` aponta para um deles. As fronteiras para os adaptadores futuros:

- adaptador de provedor remoto (Claude, OpenAI e similares): declara `data_boundary: EXTERNAL`, implementa `_invoke` sobre `BaseModelAdapter`, vive em `adapters/llm/<perfil>/` e traz o SDK como dependência PRÓPRIA, nunca do núcleo;
- adaptador de modelo local: declara `data_boundary: LOCAL` ou `PRIVATE` e é o único que pode receber evidência sensível;
- ambos informam as capacidades reais e, se quiserem, `usage`.

Nada disso foi implementado: nenhuma chamada de rede, nenhuma credencial, nenhum SDK instalado. Esta extração entrega o contrato portátil primeiro.

## Compatibilidade de runtime

| Item | Valor |
|---|---|
| Mínimo pretendido | Python 3.10 (o mesmo do projeto) |
| Auditoria de sintaxe | os módulos novos são analisados com `ast.parse(feature_version=(3, 9))`; é verificação de sintaxe, não execução |
| APIs | revisadas e varridas por nome contra APIs posteriores ao 3.9 (lista parcial); anotações são lazy (`from __future__ import annotations`) |
| Versões efetivamente testadas | somente Python 3.15.0rc2, no Windows. É uma versão candidata a lançamento (RC), a única instalada |
| Linux e macOS | a suíte não foi executada em Linux nem em macOS |
| Outras versões do Python | não havia outra instalação local; nada foi instalado. A compatibilidade com 3.10 a 3.14 não foi testada em execução |

## Limitações conhecidas

- Não existe adaptador real. A suíte prova o contrato com adaptadores sintéticos; o comportamento com um modelo de verdade (respostas fora do contrato com mais frequência, latência, custo, recusas, formatos) não foi exercitado.
- (Corrigido na Extração 4.) Nesta extração o motor de estágios repetia uma falha técnica até esgotar a `RetryPolicy` sem consultar `retryable`, e recusas determinísticas (adaptador inelegível, evidência mudada) falhavam de novo da mesma forma até acabarem as tentativas. Agora uma falha não repetível para na primeira tentativa; o que continua valendo é que o `retryable` é uma decisão do executor, não do motor.
- Uma resposta que cita referência não autorizada é recusada inteira. É a escolha conservadora; um modelo real que erre uma referência perde todo o resultado.
- O texto da resposta não pode conter algo que pareça caminho local (regra de portabilidade dos artefatos), mesmo quando o caminho é conteúdo legítimo da evidência. Quem precisar disso deve pedir ao modelo para não reproduzi-lo.
- Evidência cabe inteira em memória e dentro da tarefa; não há paginação, resumo nem limite próprio de tamanho. O adaptador declara `long-context` quando precisa.
- O conteúdo de imagem vai em base64; o núcleo não interpreta imagens. `media_type` de imagem deve ser informado por quem cria o pedido, ou será `application/octet-stream`.
- A sensibilidade é um sinal declarado por quem cria o pedido (`contains_sensitive_data`). O núcleo não detecta dado sensível no conteúdo.
- Um rebaixamento `RESOLVED` -> `INCONCLUSIVE` por confiança descarta os achados fracos (ficam só no hash da resposta). Uma resolução parcial só é guardada quando o próprio modelo a declara `INCONCLUSIVE`.
- A entrada de revisão humana e o backlog de fontes são registros derivados em memória (`human_review_candidate`, `source_gap_entry`); nenhum deles é gravado em um schema persistente ainda. A capacidade de revisão humana é uma extração futura.
- O executor lê e processa a evidência no processo do orquestrador, sem isolamento nem tempo limite; um adaptador lento bloqueia o estágio.

## Fora do escopo

Por decisão, não foram implementados: portal ou fluxo de revisão humana, ingestão de resposta humana, implementação do adaptador de DokuWiki, observador de mudanças (daemon), a etapa de conhecimento seguinte do SYSCOM, analisador de telas, engenharia reversa de banco, interface web, qualquer adaptador real de provedor, instalação de SDK, chamada de API e armazenamento de credencial.

## Rastreabilidade

Cada capacidade extraída tem origem, classificação, invariante genérico, destino, acoplamento rejeitado e testes registrados em [`extraction/EXTRACTION_003.json`](extraction/EXTRACTION_003.json) (`docs/extraction/EXTRACTION_003.json`), junto com o que foi adiado (e para onde), o que foi rejeitado (e por quê) e a lista do que não foi extraído. Testes verificam que o arquivo é consistente com o código: os destinos e os símbolos existem, os testes citados existem, todo módulo e todo schema novo estão mapeados e o artefato não carrega caminho local.

Módulos novos: `core/model_task.py`, `core/model_adapter.py`, `core/ai_recheck.py`, `core/recheck_engine.py` e `core/recheck_stage.py`; `adapters/models/base.py` e `adapters/models/scripted.py`. Schemas novos: `schemas/model-task.schema.json`, `schemas/model-result.schema.json`, `schemas/ai-recheck-request.schema.json` e `schemas/ai-recheck-result.schema.json`. A versão de `core` passou de 1.2.0 para 1.3.0.

Relação com a auditoria: [`AGENT_FOUNDATION_AUDIT.md`](AGENT_FOUNDATION_AUDIT.md), seção 8 (executor de `AI_RECHECK`). Arquitetura: [`ARCHITECTURE.md`](ARCHITECTURE.md). Fronteira de adaptadores: [`../adapters/models/README.md`](../adapters/models/README.md).
