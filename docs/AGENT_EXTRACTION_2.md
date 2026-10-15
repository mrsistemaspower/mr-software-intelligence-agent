# Agent Extraction 2: orquestração determinística

Segunda extração de capacidades comprovadas no SYSCOM para o Agent Core, feita contra os contratos da Foundation e das primitivas da [Extração 1](AGENT_EXTRACTION_1.md), sem recriar a arquitetura do projeto de origem.

Escopo: a base de execução por etapas. O agente passa a saber O QUE deve rodar (grafo de estágios), QUANDO pode rodar (dependências concluídas), O QUE já foi concluído (estado e checkpoints) e SE a conclusão ainda vale (retomada). Tudo é código determinístico, só biblioteca padrão, sem chamada a modelo de linguagem. Os estágios executam tratadores fornecidos por quem executa; os testes usam tratadores sintéticos.

O núcleo novo não conhece nenhum software, fase, tela, quantidade de estágios, tamanho de lote, data ou caminho absoluto.

## Fontes estudadas

O `mr-syscom` foi tratado como somente leitura. Quatro arquivos foram lidos por inteiro, sem importá-los nem executá-los de dentro do workspace de origem. Nenhum chamador direto foi necessário: o comportamento está nas funções lidas.

| Arquivo (commit `4c652a3`) | Blob | Componente da auditoria |
|---|---|---|
| `tools/batch/batch_run.py` | `f1457ba2ffb9dc838d0e790acbb2397c2ebe8186` | C41, máquina de estado do lote |
| `tools/batch/k4_stage_state.py` | `aeaff1f94379349dcc9f1bf98cadf3fbd209df3f` | C42, checkpoints por estágio e plano de retomada |
| `tools/batch/k4_execution_record.py` | `da9ed47abb6d5d30ceeb5621ac7ce551d18e2f86` | C44, registro de execução e deriva de conhecimento |
| `tools/batch/k4_hardening_lib.py` | `3923259335b80264a10d2e1e8ffca5351c4a20b0` | C43, estado do Git e proteção de raízes (o resto já foi extraído na Extração 1) |

O workspace de referência tinha alterações locais não relacionadas (arquivos modificados e não rastreados) antes e depois desta extração, e os quatro arquivos eram idênticos ao commit. O estado completo foi registrado no início e comparado no fim; nada foi alterado.

O que o código de origem já faz bem, e foi preservado como comportamento: estágio só é confiável se o hash de cada artefato confere; existência de arquivo nunca basta; estágio invalidado invalida o que vem depois; tela interrompida volta à fila sem gastar a tentativa; a gravação do estado é troca de arquivo temporário; a deriva do conhecimento entre execuções é detectada por hash.

## Invariantes genéricos extraídos

1. Um fluxo é um conjunto de estágios nomeados com dependências explícitas; grafo inválido nunca executa.
2. A ordem de execução é determinística e não depende da ordem de declaração.
3. Um estágio concluído só é pulado se um checkpoint provar que o resultado ainda corresponde à definição, à configuração, ao snapshot de conhecimento, às entradas, às dependências e às saídas no disco.
4. Quando um estágio deixa de valer, só os seus descendentes no grafo deixam de valer.
5. Um processo que sumiu nunca é presumido bem-sucedido, e nenhuma saída é inventada.
6. Falha é dado portátil; falha crítica interrompe a execução, sem continuação silenciosa.
7. Estado e checkpoints são gravados de forma atômica; estado ilegível nunca é substituído em silêncio.
8. A identidade da execução não depende de relógio, máquina, caminho ou processo.
9. O agente só escreve em raízes declaradas como de saída; mutação inesperada de fonte protegida é detectada e registrada, nunca revertida.
10. Git é uma capacidade opcional do ambiente, nunca um requisito.

## Acoplamento do SYSCOM rejeitado

Resumo; o detalhe, item a item, está em [`extraction/EXTRACTION_002.json`](extraction/EXTRACTION_002.json).

- Nenhum nome de tela, id, caminho, fase, contagem fixa, data fixa ou nome de contrato entrou no núcleo. Um teste varre os arquivos novos atrás desses termos, dos termos de vazamento de todos os pacotes e de nomes de provedores de modelo.
- Os oito estágios fixos e os arquivos de cada um viraram um grafo recebido como configuração.
- As pastas protegidas fixas e a lista de raízes proibidas viraram uma política declarada por raiz.
- Data de relógio dentro do estado, identificador `run-NNN` pelo histórico do arquivo, temporário de nome fixo e caminho absoluto devolvido quando o arquivo está fora da raiz são acidentes históricos e não foram levados.
- O que depende de modelo ou de imagem (passo multimodal, `AI_RECHECK`, classe de revisão) ficou de fora (ver "Fora do escopo").

## Grafo de estágios

`core/stage_graph.py` e `schemas/stage-graph.schema.json`.

Cada `StageSpec` declara `stage_id`, `version` (versão da definição: mudar invalida o trabalho feito), `depends_on`, `input_contracts` (nomes lógicos das entradas externas consumidas), `output_contracts` (nomes lógicos das saídas obrigatórias), `optional` e `retry` (`max_attempts`). Os nomes são arbitrários, desde que sejam também componentes de caminho portátil, porque saídas viram arquivos.

Validação, antes de qualquer execução (`GraphError` com `code` estável): grafo vazio, identificador repetido, autodependência, dependência desconhecida, ciclo (a mensagem mostra um ciclo concreto) e estágio obrigatório que depende de estágio opcional. Essa última regra existe porque um opcional que falha deixaria o dependente obrigatório sem como rodar; a regra desfaz o impasse na origem.

Ordem determinística: ordenação topológica de Kahn em que, entre estágios prontos ao mesmo tempo, vence o menor `stage_id` (ponto de código Unicode). A ordem de declaração nunca influencia a execução, a serialização nem o hash do grafo; um teste confere as permutações. `StageGraph.digest()` é a identidade do grafo.

Um só conceito de fluxo. Os passos (`steps`) de um registro `agent-workflow` da Foundation viram estágios (`graph_from_agent_workflow`). Sem `depends_on`, cada passo depende do anterior, de modo que os seis workflows existentes continuam válidos e lineares. O schema `schemas/agent-workflow.schema.json` ganhou um campo opcional `depends_on` por passo, compatível com tudo o que já existia: lista vazia torna o passo uma raiz; lista explícita descreve ramos independentes. Não foi criado um segundo vocabulário de fluxo.

## Máquina de estados

`core/stage_state.py`. Seis estados:

| Estado | Significa |
|---|---|
| `PENDING` | ainda não executou, ou foi reposto para tentar de novo |
| `RUNNING` | uma tentativa está em curso; se o processo some, o registro fica assim no disco |
| `COMPLETED` | concluiu e tem checkpoint; só é pulado se o checkpoint ainda valer |
| `FAILED` | a última tentativa falhou; carrega a falha registrada |
| `BLOCKED` | não pode rodar porque uma dependência falhou de vez |
| `INVALIDATED` | estava concluído, mas o resultado deixou de valer; carrega a causa |

`READY` não é estado persistido, é derivado (`is_runnable`): o estágio está em `PENDING` ou `INVALIDATED` e todas as dependências estão em `COMPLETED`. Persistir o derivado criaria uma segunda verdade para manter em sincronia. `INVALIDATED` existe separado de `PENDING` porque guarda a causa e distingue "nunca rodou" de "rodou e deixou de valer".

Transições permitidas (qualquer outra levanta `TransitionError`):

```
PENDING     -> RUNNING | BLOCKED
RUNNING     -> COMPLETED | FAILED | PENDING (só com recovery=True: recuperação de interrupção)
COMPLETED   -> INVALIDATED
FAILED      -> PENDING | BLOCKED
BLOCKED     -> PENDING
INVALIDATED -> RUNNING | BLOCKED
```

Um estágio `COMPLETED` nunca volta direto a `PENDING` nem a `RUNNING`. Cada estado tem um invariante verificado na construção do registro (por exemplo, `COMPLETED` exige checkpoint e identidade; `FAILED` exige falha; `INVALIDATED` e `BLOCKED` exigem causa; `PENDING` é limpo). Um teste percorre todos os 36 pares de estados.

Tentativas: entrar em `RUNNING` conta uma tentativa. A recuperação de interrupção pode devolvê-la. `COMPLETED -> INVALIDATED` zera a contagem, porque o resultado antigo não pertence mais às entradas atuais.

Estados da execução (`RunStatus`, em `core/run_state.py`): `PENDING`, `RUNNING`, `COMPLETED`, `FAILED` e `UNSAFE`. Uma execução `COMPLETED` pode conter estágios opcionais `FAILED` ou `BLOCKED`.

## Contrato do checkpoint

`core/checkpoint.py` e `schemas/stage-checkpoint.schema.json`.

A identidade de execução (`execution_identity`) é calculada com `core.identity.artifact_identity` (tipo `stage.execution`) e depende SÓ de: fluxo e software; id, versão e `definition_digest` do estágio; `parameters_digest` (a configuração relevante do estágio); o `knowledge_snapshot_id`; o hash de cada entrada declarada; e o `outputs_digest` de cada dependência. Não depende de relógio, máquina, caminho absoluto, número da tentativa nem de modelo de linguagem. O checkpoint também guarda caminho lógico, hash e tamanho de cada saída. `checkpoint_id` deriva da identidade de execução e das saídas.

Um checkpoint é VÁLIDO só se tudo confere agora (`check_checkpoint`), e todos os motivos de divergência são relatados, em ordem de prioridade:

| Motivo | Quando |
|---|---|
| `WORKFLOW_CHANGED` | outro fluxo, software ou estágio |
| `DEFINITION_CHANGED` | versão, dependências, contratos ou flag `optional` mudaram (a política de repetição NÃO conta) |
| `SNAPSHOT_CHANGED` | o snapshot de conhecimento é outro, apareceu ou sumiu |
| `CONFIG_CHANGED` | os parâmetros do estágio mudaram (e só os dele) |
| `INPUT_CHANGED` | uma entrada declarada mudou ou ficou indisponível |
| `UPSTREAM_CHANGED` | as saídas de uma dependência são outras |
| `OUTPUT_SET_CHANGED`, `OUTPUT_MISSING`, `OUTPUT_MUTATED`, `OUTPUT_UNREADABLE` | as saídas no disco não são as registradas |

Ao carregar, `checkpoint_id`, `outputs_digest` e `execution_identity` são recalculados: adulterar qualquer campo é recusado (`CheckpointError`, código `CORRUPT`). Arquivo truncado, vazio, com BOM, com chave duplicada ou arquivado sob outro estágio também é recusado. O número da tentativa é metadado e não entra na identidade.

## Semântica da retomada

`core/resume.py` (`reconcile`) e `core/orchestrator.py` (`Orchestrator.run`, `start`, `resume`). A retomada compara o estado gravado com o mundo de agora, em ordem topológica:

1. Estágio novo no grafo entra `PENDING`; estágio que saiu do grafo é descartado, com o checkpoint.
2. Estágio em `RUNNING` é um processo que sumiu (ver abaixo).
3. `COMPLETED` com checkpoint válido e dependências válidas é pulado (`SKIP`). `COMPLETED` com checkpoint ausente, corrompido, diferente do registrado no estado ou divergente NÃO é pulado: vira `INVALIDATED` com a causa.
4. `FAILED` com as entradas mudadas, ou com a dependência sendo refeita, ganha vida nova (tentativas zeradas). Com tentativas sobrando volta a `PENDING` (`RETRY`); sem tentativas fica `FAILED` (`EXHAUSTED`). Falha por mutação de fonte não gasta tentativa: não foi culpa do estágio.
5. Se uma dependência falhou de vez, o estágio fica `BLOCKED`; se o bloqueio acabou, volta a `PENDING`.

Cada decisão do plano (`ResumePlan`) tem um motivo explícito. A repetição acontece em uma nova retomada, nunca em laço interno: não há espera nem recuo exponencial.

Recuperação de interrupção. Um estágio encontrado em `RUNNING` nunca é presumido concluído, mesmo que exista um checkpoint (uma queda pode ter ocorrido entre gravar o checkpoint e gravar o estado). O checkpoint dele é descartado e a política da configuração decide: `REQUEUE` (padrão) volta a `PENDING` e devolve a tentativa; `FAIL` vira `FAILED` com categoria `INTERRUPTED` e a tentativa conta. A saída parcial que ficou no disco não é confiada: nenhum checkpoint a sela.

## Semântica da invalidação

Quando um estágio deixa de valer, os descendentes concluídos são invalidados de imediato, com causa `UPSTREAM_INVALIDATED` e o estágio de origem registrado. É conservador de propósito: o descendente refaz mesmo que o estágio refeito produza saídas idênticas. A identidade de cada checkpoint também amarra o `outputs_digest` das dependências, o que protege em profundidade.

Para o grafo `A -> B -> C` e `A -> D`:

| O que mudou | Invalidados | Continuam válidos |
|---|---|---|
| a entrada ou a saída de `A` | `A`, `B`, `C`, `D` | nenhum |
| só `B` | `B`, `C` | `A`, `D` |
| só `C` | `C` | `A`, `B`, `D` |
| só `D` | `D` | `A`, `B`, `C` |

Um teste ainda confere que o checkpoint e a saída de um ramo válido nem são regravados (a data de modificação não muda). No exemplo sintético linear (`inventory -> discover -> analyze -> validate -> document`), mudar a entrada de `analyze` refaz `analyze`, `validate` e `document`, e `inventory` e `discover` continuam válidos. No exemplo ramificado, mudar só a entrada da interface invalida a análise da interface e a documentação; a análise do banco continua válida.

## Execução e falhas

Para cada estágio pronto, o orquestrador: hasheia as entradas declaradas; calcula a identidade; remove o checkpoint antigo; grava `RUNNING` (atômico); chama o tratador; confere as fontes protegidas, as entradas (hash de novo) e que as saídas declaradas, e só elas, foram escritas; hasheia as saídas do disco; grava o checkpoint e SÓ ENTÃO grava `COMPLETED`.

O tratador recebe um `StageContext` limitado ao que o estágio declarou: lê entradas (`read_input` falha se o conteúdo mudou desde a partida do estágio), lê saídas de dependências diretas (conferidas contra o checkpoint), e escreve saídas declaradas na raiz de saídas (`write_output`, atômico). Escrever fora do contrato é recusado.

Falha registrada (`FailureInfo`): estágio, categoria fechada (`HANDLER_ERROR`, `INPUT_UNAVAILABLE`, `INPUT_MUTATED`, `OUTPUT_CONTRACT`, `INTERRUPTED`, `SOURCE_MUTATION`), mensagem sem controle e sem caminho local (a mensagem que contém caminho tem o detalhe omitido), tentativa e estado do checkpoint. Nenhuma exceção do Python é serializada.

Falha de estágio obrigatório interrompe a execução: nenhum outro estágio roda depois, nem em ramos independentes. É deliberado ("sem continuação silenciosa após falha crítica"); o custo é que ramos independentes esperam a retomada. Falha de estágio opcional bloqueia só os seus dependentes (que também são opcionais, pela regra do grafo) e a execução segue. Exceções que não são `Exception` (queda do processo, `KeyboardInterrupt`) não são capturadas, e deixam o estágio em `RUNNING` no disco: é o caso da interrupção.

## Persistência atômica e identidade

`core/run_state.py` e `schemas/run-state.schema.json`.

O estado e os checkpoints usam `core.atomic` (Extração 1): serialização antes de tocar o disco, temporário no mesmo diretório, `fsync`, troca atômica e remoção do temporário em qualquer falha. Uma queda durante a gravação deixa a versão anterior inteira. Um teste injeta uma falha em CADA gravação atômica de uma execução completa (o estado, os checkpoints e as saídas), confere que o estado em disco sempre carrega, que nenhum temporário é abandonado e que a retomada converge para as mesmas saídas byte a byte.

Estado ilegível (truncado, com BOM, com chave duplicada, fora do contrato) levanta `RunStateError` e o arquivo é deixado como está: nunca é substituído em silêncio por um estado novo. Os invariantes (um só `RUNNING`, coerência de `current_stage`, status compatível com os estágios, `run_id` recalculável) são verificados ao carregar.

Identidade da execução: `run_id` deriva só de `workflow_id`, `software_id`, `workflow_version` e `run_key` (rótulo escolhido por quem executa, `default` por padrão). Mudar uma entrada ou o snapshot não muda o `run_id`: é a mesma execução reavaliada, o que permite retomar e invalidar. O que a execução recebeu agora aparece em `input_identity`. Um estado de outra execução (outra versão do fluxo, outro software, outra `run_key`) não é adotado (`RunMismatchError`). Metadados de geração (`started.at`, `generator`, `resume_count`) ficam fora de toda identidade; o núcleo não lê relógio, e `started.at` é fornecido por quem executa.

Mover o workspace de lugar, ou rodá-lo em outra máquina, outro processo, outra semente de hash, outro fuso ou outra codificação, produz o mesmo estado, os mesmos checkpoints e as mesmas saídas, byte a byte (testado com processos separados).

## Proteção de fontes

`core/protection.py` e `core/run_config.py`. Cada raiz é declarada por nome e recebe uma classe:

| Classe | Significa |
|---|---|
| `READ_ONLY` | o agente nunca escreve aqui. O conteúdo pode mudar ENTRE execuções (isso é mudança de entrada e invalida o que depende dela). Mudar DURANTE a execução é violação. A linha de base é nova a cada execução |
| `FROZEN` | evidência fixada. A linha de base é gravada na primeira execução e vale para sempre: qualquer diferença, em qualquer momento, é violação. Não pode ter a verificação desligada |
| `MUTABLE_OUTPUT` | a única classe em que o agente escreve (estado, checkpoints e saídas). Não tem integridade fixa nem escopo |

Verificação (`Verify`): `EACH_STAGE` (padrão das raízes protegidas: depois de cada estágio e no fim), `RUN_END` ou `NONE` (só para `READ_ONLY`). A linha de base é um `KnowledgeSnapshot` e a conferência é `core.integrity`: ausente, alterado, surgido, ilegível ou fora da lista. O escopo (`scope`) limita o que se observa: `.` é a raiz inteira; um caminho de arquivo observa só aquele arquivo (e seu surgimento); um caminho de diretório observa seus arquivos.

Violação marca a execução como `UNSAFE`, para a execução e registra raiz, tipo e caminho lógico (nunca conteúdo nem caminho local; no máximo 50 itens e o total). O estágio que estava rodando vira `FAILED` com categoria `SOURCE_MUTATION`, e o resultado dele não é selado por checkpoint. O agente NÃO reverte, apaga nem corrige nada: o que mudou pode ser dado de uma pessoa. Se a violação é achada na partida (por exemplo, uma fonte `FROZEN` editada entre execuções), nada roda e nada é reconciliado ou invalidado; quando a pessoa restaura a evidência, a execução seguinte encontra o estado como estava e pula o que vale.

Raízes não podem se sobrepor no disco. Linha de base `FROZEN` adulterada, ilegível ou de outro software é recusada.

## Fronteira do adaptador de Git

`core/gitstate.py`. Git é uma capacidade opcional do ambiente. Nenhum módulo do núcleo o importa, e `subprocess` só aparece nele. O orquestrador aceita, por injeção, qualquer objeto com `capture()` e `compare()` (`EnvironmentProbe`); sem sonda, a execução não sabe que Git existe. Um teste executa uma execução completa num processo em que `subprocess` e o adaptador foram tornados importáveis como inexistentes.

A captura é somente leitura (`--no-optional-locks`, lista de argumentos, sem shell, sem rede, com tempo limite no comando de Git): HEAD, ramo, `dirty` (alteração em arquivo rastreado), caminhos rastreados alterados (lógicos, relativos à raiz do repositório), contagem de não rastreados. Git ausente, fora de repositório, tempo esgotado ou erro viram `available=False` com um motivo estável, sem levantar. A deriva entre o início e o fim da execução é registrada como informação em `environment` e nunca como veredito. Fora do escopo: API de hospedagem, `fetch`, `push`, qualquer escrita.

## Configuração

`core/run_config.py` e `schemas/run-config.schema.json`. `RunConfig` reúne o fluxo, o grafo, as raízes por nome com a política de cada uma, a raiz de estado e a de saídas (obrigatoriamente `MUTABLE_OUTPUT`), as entradas nomeadas (cada uma ligada a um arquivo de uma raiz `READ_ONLY` ou `FROZEN`), os parâmetros por estágio e a política de interrupção. Toda a configuração é dado; `digest()` é o hash do JSON canônico e é o mesmo em qualquer máquina e pasta. Os caminhos locais das raízes e o snapshot de conhecimento são dados na hora de executar e nunca entram na configuração. Entrada declarada sem vínculo, vínculo que ninguém declara (erro de digitação), parâmetros para estágio inexistente e qualquer caminho local são recusados antes de executar.

Um parâmetro só invalida o estágio a que pertence e seus descendentes.

## Compatibilidade de runtime

| Item | Valor |
|---|---|
| Mínimo pretendido | Python 3.10 (o mesmo do projeto) |
| Auditoria de sintaxe | os módulos novos são analisados com `ast.parse(feature_version=(3, 9))`; é verificação de sintaxe, não execução |
| APIs | revisadas e varridas por nome contra APIs posteriores ao 3.9 (lista parcial); anotações são lazy (`from __future__ import annotations`) |
| Versões efetivamente testadas | somente Python 3.15.0rc2, no Windows. É uma versão candidata a lançamento (RC), a única instalada |
| Linux e macOS | a suíte não foi executada em Linux nem em macOS |
| Outras versões do Python | não havia outra instalação local; nada foi instalado. A compatibilidade com 3.10 a 3.14 não foi testada em execução |

Nenhum recurso exclusivo de uma versão candidata é usado até onde a revisão e as varreduras alcançam, mas isso só se confirma executando a suíte em versões estáveis anteriores, o que não foi feito. Os testes com um repositório Git real são pulados quando `git` não está disponível.

## Limitações conhecidas

- Há um único escritor por diretório de estado: não existe trava entre processos. Duas execuções simultâneas sobre o mesmo estado podem se atropelar.
- A proteção detecta, mas não é sandbox. O tratador roda no processo do orquestrador, sem isolamento e sem tempo limite; ele pode escrever onde o sistema operacional permitir. A API de escrita do agente só concede raízes `MUTABLE_OUTPUT`, e qualquer escrita indevida numa raiz protegida é detectada na verificação seguinte.
- A proteção não segue ligações simbólicas de diretório dentro de uma raiz, e um arquivo cujo nome não é um caminho lógico portátil impede a captura (`ProtectionError`). Caminhos que só diferem em maiúsculas e minúsculas são recusados.
- Verificar e depois ler são dois momentos. A defesa contra mudança durante o estágio é rehashear as entradas declaradas depois do tratador e conferir a leitura (`read_input`); entradas que o tratador lê por fora do contrato não são rastreadas.
- Verificação `EACH_STAGE` rehasheia o escopo inteiro a cada estágio. Para árvores grandes, restrinja o `scope` ou use `RUN_END`.
- Mudar o `scope` de uma raiz `FROZEN` depois da primeira execução exige descartar a linha de base de propósito; a mudança não é detectada sozinha.
- Sem estado, o checkpoint é ignorado: não há adoção retroativa de resultados existentes (o estágio roda de novo). É a escolha conservadora.
- A invalidação é conservadora: um descendente refaz mesmo que o estágio a montante produza saídas idênticas.
- Em `os.replace` no Windows a atomicidade não é formalmente garantida e a troca falha com `PermissionError` se outro processo mantém o destino aberto (ver `docs/AGENT_EXTRACTION_1.md`).
- Uma violação achada só no fim da execução (`RUN_END`, ou raiz `READ_ONLY` verificada no fim) marca a execução `UNSAFE`, mas não desfaz estágios já concluídos; a retomada reconfere as entradas declaradas.
- Nenhum tratador real foi escrito: os testes usam tratadores sintéticos. O comportamento com tratadores lentos, concorrentes ou que usem rede não foi exercitado.

## Fora do escopo

Por decisão, não foram implementados: executor de `AI_RECHECK`, runtime de modelo de linguagem, motor de ingestão de revisão humana, interpretação de perguntas, motor de lookup, analisador de telas, explorador de runtime, implementação do adaptador de DokuWiki, observador de mudanças (daemon) e interface web. Também ficaram para depois: adoção de resultados existentes sem checkpoint, métricas e histórico de execuções, e comandos de linha de comando para os novos recursos (ver `deferred` em `extraction/EXTRACTION_002.json`).

## Rastreabilidade

Cada capacidade extraída tem origem, classificação, invariante genérico, destino, acoplamento rejeitado e testes registrados em [`extraction/EXTRACTION_002.json`](extraction/EXTRACTION_002.json) (`docs/extraction/EXTRACTION_002.json`), junto com o que foi adiado (e para onde), o que foi rejeitado (e por quê) e a lista do que não foi extraído. Testes verificam que o arquivo é consistente com o código: os destinos e os símbolos existem, os testes citados existem, todo módulo e todo schema novo estão mapeados e o artefato não carrega caminho local.

Módulos novos: `core/stage_graph.py`, `core/stage_state.py`, `core/checkpoint.py`, `core/protection.py`, `core/run_config.py`, `core/run_state.py`, `core/resume.py`, `core/orchestrator.py` e `core/gitstate.py`. Schemas novos: `schemas/stage-graph.schema.json`, `schemas/stage-checkpoint.schema.json`, `schemas/run-config.schema.json` e `schemas/run-state.schema.json`. Schema estendido, de forma compatível: `schemas/agent-workflow.schema.json` (campo opcional `depends_on` nos passos). A versão de `core` passou de 1.1.0 para 1.2.0.

Relação com a auditoria: [`AGENT_FOUNDATION_AUDIT.md`](AGENT_FOUNDATION_AUDIT.md), seção 8, item 2 (C41, C42 e C44, com grafo de estágios configurável). Arquitetura: [`ARCHITECTURE.md`](ARCHITECTURE.md).
