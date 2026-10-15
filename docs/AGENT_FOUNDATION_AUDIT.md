# Agent Foundation 1.0: auditoria do mr-syscom

Data da auditoria: 2026-10-14.
Objeto: `mr-syscom` (primeiro caso de treino e validação), em modo somente leitura.
Objetivo: descobrir quais capacidades desenvolvidas no SYSCOM são reutilizáveis num agente genérico de inteligência de software, e quais devem ficar no conhecimento específico do SYSCOM.

## 1. Escopo e método

Regras seguidas:

- Nenhum arquivo do `mr-syscom` foi criado, editado, movido, formatado ou apagado.
- Nenhum script do `mr-syscom` foi executado (há `__pycache__` na árvore; executar poderia gerar arquivos).
- Nenhuma captura de tela foi aberta. A auditoria é de arquitetura, código e artefatos.
- A análise usou leitura direta e três frentes de leitura em paralelo: modelo de evidência, revisão humana e infraestrutura de pipeline.

Prova de não alteração: antes da auditoria foi gravado um baseline fora dos dois repositórios (lista de 2.242 arquivos com tamanho e data de modificação, mais a saída de `git status --porcelain`). Depois da auditoria os dois hashes eram idênticos aos do baseline, e o `HEAD` continuava em `4c652a3`. O working tree do `mr-syscom` já tinha 168 entradas não commitadas antes de começarmos; elas não foram tocadas.

Cobertura e limites:

- Foram lidos por completo ou em grande parte: os contratos de evidência e revisão (`docs/`), os schemas, `tools/pilot/` (geração e validação do pacote), `tools/review/derive_review_class.py`, o núcleo de `tools/batch/` (estado, checkpoints, hash, snapshot, cache) e parte dos motores de ingestão.
- Foram vistos só pelo nome, tamanho ou cabeçalho: `prompts/*.md`, o conteúdo dos JSONs de `knowledge/conventions/`, a maior parte de `tools/review/` (portais e QA), os scripts `k4_2_*` (exceto reuso e taxonomia), `docs/` de histórico, `site/` e `_archive/`. Esses itens estão marcados `NEEDS_REVIEW` ou têm a ressalva na coluna de notas.
- Números de linha citados vêm de busca textual. Onde a ferramenta de leitura não numerava linhas, cito função ou arquivo.
- Contagens de "componentes" neste documento são linhas do mapa da seção 4, não arquivos. Um arquivo grande pode aparecer em mais de uma linha quando mistura mecanismo genérico e regras específicas.

Correções feitas durante a verificação (achados de leitura que foram checados diretamente):

- `HUMAN_VALIDATION` não existe como classe de revisão. Existe `human_validation` (minúsculas) como origem de pergunta (`tools/batch/k4_2_review.py`, `CURATED_HUMAN`) e como `recommended_source`.
- `documentation.md` é gerado por `tools/batch/k4_generate.py` (perfil compacto). Uma das frentes tinha reportado "não encontrado" por ter buscado só em outras pastas.
- O validador `tools/schema/validate_schema.py` implementa `$ref`, `allOf`, `anyOf`, `oneOf`, `const`, `enum`, `pattern`, mas não implementa `if/then/not`.
- O gerador de perguntas de revisão exporta o schema `k4-human-review@1.0.0`, enquanto o ingestor valida `syscom-human-review-k4@1.0.0`.
- `tools/review/publish_k42_sync.py` grava fora do workspace, num diretório irmão de publicação.

## 2. Resultado em uma página

O ativo mais valioso do SYSCOM não é um script. É um método: separar o que foi visto, o que foi inferido e o que uma pessoa confirmou; manter a confiança como eixo à parte; preservar versões em conflito em vez de sobrescrever; só chamar humanos depois de reuso de evidência, nova leitura pela IA e busca de fonte; e rodar lotes de forma determinística, retomável e protegida por hash.

Esse método está implementado, e funciona, mas está fundido ao SYSCOM em três camadas:

1. Modelo de dados centrado em tela de formulário (ids `c###`, `b###`, `g###`, tipos de componente, "item crítico" = título, menu, campo chave, rodapé).
2. Tabelas de regras escritas à mão por pergunta e por rodada (arquivos de 30 a 50 KB), com contagens esperadas fixas (47 respostas, 330 perguntas, 196 famílias).
3. Caminhos, datas, ids e prefixos de versão (`K3`, `K4.2`, `syscom-*`) espalhados no código.

Conclusão: o caminho de extração é contrato primeiro, motor depois. A Foundation 1.0 fixa os contratos (schemas, invariantes, política de escalonamento) e um núcleo pequeno que os executa. A extração dos motores do SYSCOM deve ser feita contra esses contratos, e não por cópia de scripts.

Não há chamada a LLM nem a API de provedor em `tools/`. Só biblioteca padrão do Python (3.10 ou mais). O passo de IA entra por arquivos "semente" (`visual-observation.seed.json`, `screen-understanding.seed.json`). Isso é bom para portabilidade: o contrato real com o modelo já é um par de arquivos.

## 3. Capacidades confirmadas na implementação

Status: `IMPLEMENTED` (existe em código que roda), `PARTIAL` (existe com lacuna relevante), `DOCUMENTED_ONLY` (só em documento), `NOT_FOUND` (busca feita, não achado).

| ID | Capacidade | Status | Evidência no mr-syscom | Forma genérica na Foundation 1.0 |
|---|---|---|---|---|
| CAP01 | OBSERVED / INFERRED / VALIDATED | IMPLEMENTED | `schemas/syscom-screen.schema.json` (`evidenceLevel`); `tools/schema/validate_schema.py` (`EXPECTED_EVIDENCE_LEVELS`); `tools/pilot/validate_transfer_package.py` (G3, G4, R8, R9) | `schemas/common.schema.json` (`epistemic.status`) |
| CAP02 | Provenance | PARTIAL | `sources[]` com `sha256` anulável; `source_ids` por afirmação; `validated_by` e `validated_at`. O validador do pacote não confere `sha256` contra a captura | `epistemic.provenance` obrigatório, com `source_sha256` fixado e conferido por `core/pack.py` |
| CAP03 | Confidence como eixo separado | PARTIAL | Enum `high/medium/low` separado de `evidence_level` (doc seção 4 e schema). Sem cálculo em código. Não obrigatório em todos os elementos do pacote | `epistemic.confidence` numérico 0 a 1, obrigatório; faixa derivada em `core/epistemics.py` |
| CAP04 | Human review | IMPLEMENTED | `docs/HUMAN_REVIEW_POLICY.md`; `tools/review/derive_review_class.py`; motores de ingestão | `schemas/human-review.schema.json`; `policies/` |
| CAP05 | AI_RECHECK | PARTIAL | Derivação em `derive_review_class.py`; estado em `tools/batch/batch_run.py` (classes na linha 54; `pending` e `completed`); schema `ai_recheck`; re-derivação escala para decisão humana. Não há executor: `k4_2_correction.py` grava `ai_recheck_queue.executed = False` | Rota `AI_RECHECK` em `core/escalation.py`, com regra "só vale com leitura nova da evidência" |
| CAP06 | SOURCE_GAP | IMPLEMENTED | `derive_review_class.py` (`sg_crit`, `sg`); backlog de captura | Rota `SOURCE_GAP` com `needed_source` obrigatório |
| CAP07 | HUMAN_DECISION | IMPLEMENTED | `derive_review_class.py`: 5 gatilhos (conflito, quarentena, agrupamento, privacidade, contradição de padrão). O gatilho de alto impacto não é derivável por código (política seção 3.1) | Rota `HUMAN_DECISION` com `options` (2 ou mais); campo `impact` nas regras |
| CAP08 | HUMAN_VALIDATION | PARTIAL | Não existe como classe. Equivalentes: origem `human_validation` das perguntas, flag `open_questions_human_validation`, fatos `VALIDATED` com `source_type: human_review`, rotas `HUMAN_CONFIRMED` | Rota `HUMAN_VALIDATION` com `hypothesis_ref` (hipótese INFERRED a confirmar) |
| CAP09 | Contradiction handling | PARTIAL | Conflito preservado como `UNRESOLVED` e verificado (G5). Resolução com histórico existe só na rodada K4.2, escrita à mão. Sem detecção automática. O pacote não consegue representar conflito resolvido (schema com `const`) | `schemas/contradiction.schema.json` (OPEN e RESOLVED, histórico preservado); invariantes em `core/pack.py` |
| CAP10 | Quarantine | IMPLEMENTED | `tools/review/ingest_k4_human_review.py` (disposição `QUARANTINED`). Convenção fora do schema, com vocabulário extra (`NOT_PROMOTED`, `HUMAN_CLARIFICATION_REQUIRED`) | `epistemic.quarantine` no schema comum; regra: posições de contradição aberta ficam em quarentena |
| CAP11 | Screen grouping | PARTIAL | `tools/screen-grouper/` (candidatos por nome, decisões registradas); `grouping_decision` UNRESOLVED; regra cross-evidence em `semantic_cleanup.py`. Não há raciocínio positivo de fusão | `screen.grouping` (DECIDED ou UNRESOLVED) |
| CAP12 | Multi-capture | PARTIAL | `sources` sempre lista; papéis de captura. Achado MC-1 em `docs/K4_SCALE_PIPELINE_HARDENING.md`: `k4_generate` registra só a primeira evidência em `sources.json` (decisão pendente) | `screen.captures` (lista) e `source.role` |
| CAP13 | Lookup routes | IMPLEMENTED | `knowledge/conventions/syscom-lookup-routes.json`; `tools/review/build_k4_1_lookup_routes.py` (escada de status) | `schemas/lookup.schema.json`: destino `UNKNOWN` não admite referência (não adivinhar) |
| CAP14 | Interaction patterns | IMPLEMENTED | `knowledge/conventions/syscom-interaction-patterns.json`; `tools/batch/k4_2_reuse.py` | Conteúdo fica no pacote; formato é candidato a contrato futuro |
| CAP15 | Question deduplication | IMPLEMENTED | K4.1: `knowledge_key` e `DEDUP_GROUPS`. K4.2: famílias `qf:` (330 perguntas em 196 famílias). Acoplada a 8 regex em português do gerador | `human-review.family_key` e `core.escalation.dedupe_questions` |
| CAP16 | Knowledge snapshot | IMPLEMENTED | `tools/batch/k4_hardening_lib.py` (`knowledge_snapshot`, `snapshot_drift`) | Previsto para a extração do motor de lote; ver seção 8 |
| CAP17 | Deterministic resume | IMPLEMENTED | `tools/batch/k4_stage_state.py` (`resume_plan`: `SKIP_VALID`, `RETRY_INVALID`, `ADOPT_LEGACY`, `RETRY_INCOMPLETE`, `RUN_NEW`, `BLOCKED_MISSING_SEED`); `tools/pilot/test_determinism.py` | Previsto para a extração do motor de lote |
| CAP18 | Stage checkpoints | IMPLEMENTED | `k4_stage_state.py` (8 estágios fixos; sidecar com sha256 por artefato; escrita atômica e idempotente) | Previsto para a extração do motor de lote (grafo de estágios configurável) |
| CAP19 | Hash protection | PARTIAL | Forte no lote (manifest, `evidence_lock`, cache por sha256, `PROTECTED_PATHS`). Fraca no pacote: `sources[].sha256` é anulável e não é conferido | `source_sha256` fixado na proveniência e conferido na validação do pacote |
| CAP20 | Evidence integrity | IMPLEMENTED | `tools/evidence/k41_common.py` (`check_png`: assinatura, CRC, IHDR, IEND); dry-run DR04 (arquivo contra manifest e lock) | Interface de adaptador de fonte com `fingerprint` |
| CAP21 | Documentation generation | PARTIAL | `tools/batch/k4_generate.py` linha 807 gera `documentation.md` (perfil compacto). Perfil detalhado não encontrado. Trilha D6 (manual HTML) é legado | Workflow `document` e skill `documentation-generation` (contrato) |
| CAP22 | Scale selection | IMPLEMENTED | `tools/batch/k4_select.py` (estratificada e determinística por tamanho e sha256). Universo fixo no código | Previsto para a extração do motor de lote |
| CAP23 | Incremental knowledge reuse | IMPLEMENTED | `tools/batch/k4_2_reuse.py` (reuso knowledge-first, supressão de perguntas); `k4_visual_cache.py` (chave `sha256` + estágio; invalida só por `SHA_CHANGED`). Sem invalidação por versão de ferramenta ou prompt | Proveniência com `relation: reused`; `core/impact.py` |
| CAP24 | Change intelligence / comparação de versões | NOT_FOUND | Busca por release notes, changelog, comparação de versões e diff de telas: nada. `snapshot_drift` compara o conhecimento do próprio agente, não o software alvo | `schemas/release-change.schema.json`; `core/impact.py`; skill `change-intelligence` |
| CAP25 | Chamadas a LLM ou provedor | NOT_FOUND | Nenhum import de SDK de modelo em `tools/`; só biblioteca padrão. IA entra por arquivos semente | Adaptadores por provedor em `adapters/llm/`, fora do núcleo |

## 4. Mapa de componentes

Classificação: `GENERIC_AGENT_CAPABILITY`, `SYSCOM_SPECIFIC_KNOWLEDGE`, `PIPELINE_INFRASTRUCTURE`, `EXPERIMENTAL_OR_LEGACY`.
Potencial genérico: `high`, `medium`, `low`, `none`. Acoplamento ao SYSCOM: `low`, `medium`, `high`.
Ação: `EXTRACT`, `ADAPT`, `KEEP_IN_SYSCOM`, `REWRITE_GENERIC`, `IGNORE_LEGACY`, `NEEDS_REVIEW`.

| ID | Existing component | Location in mr-syscom | Classification | Generic potential | Coupling | Recommended action | Notes |
|---|---|---|---|---|---|---|---|
| C01 | Contrato de conhecimento e evidência (leitura humana) | `docs/SYSCOM_KNOWLEDGE_SCHEMA_v1.md` | GENERIC_AGENT_CAPABILITY | high | medium | ADAPT | Níveis de evidência, eixo de confiança, captura diferente de tela. Carrega `system: SYSCOM`, vocabulário de formulário e revisor nomeado |
| C02 | Política de revisão humana | `docs/HUMAN_REVIEW_POLICY.md` | GENERIC_AGENT_CAPABILITY | high | medium | ADAPT | 5 classes e árvore de decisão. "Item crítico" é específico de formulário. Taxas de amostragem 20% e 5% existem só no texto |
| C03 | Convenções de dataset | `docs/DATASET_CONVENTIONS.md` | GENERIC_AGENT_CAPABILITY | medium | high | ADAPT | Fonte intocável, visões derivadas. Regras de mojibake do Windows, idioma e caminhos fixos |
| C04 | Convenções de interação | `docs/SYSCOM_INTERACTION_CONVENTIONS.md` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | O modelo escopo, status e claim é reutilizável. O conteúdo é comportamento do SYSCOM |
| C05 | Documentos de hardening | `docs/K3_4_SCALE_HARDENING.md`, `docs/K4_SCALE_PIPELINE_HARDENING.md` | PIPELINE_INFRASTRUCTURE | medium | medium | ADAPT | Justificativas de resume, hash e drift viram registros de decisão. Só o segundo foi lido (linhas 1 a 170) |
| C06 | Histórico e relatórios do projeto | `docs/` (K3_*, K4_*, FINAL_PROJECT_AUDIT*, PROJECT_PLAN, PROJECT_PROGRESS, GIT_CHECKPOINT_PLAN, LEGACY_PIPELINE_SNAPSHOT) | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | Histórico do projeto. Não lidos nesta auditoria, exceto o que consta em C01 a C05 |
| C07 | Schema de conhecimento por tela | `schemas/syscom-screen.schema.json` | GENERIC_AGENT_CAPABILITY | high | high | ADAPT | Evidência, confiança, fonte com sha256, asserção, validação, classe de revisão. Misturado com ids de GUI. Só os `$defs` foram inspecionados |
| C08 | Schema do pacote de transferência | `schemas/syscom-transfer-package-2.1.0.schema.json` | GENERIC_AGENT_CAPABILITY | high | medium | ADAPT | Visão derivada (`canonical: false`, `regenerable: true`). Constantes `k3-transfer`. Não representa conflito resolvido |
| C09 | Schemas dos sidecars de hardening | `schemas/syscom-k4-hardening.schema.json` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | Snapshot, estado por estágio, cache, registro de execução, dry-run. `remaining_preflight` é específico |
| C10 | Schema do estado de lote | `schemas/syscom-batch-run.schema.json` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | Status de processamento, classe de revisão, objeto `ai_recheck` |
| C11 | Fixture de schema | `schemas/examples/` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | O padrão de fixture foi replicado em `evaluations/fixtures/` |
| C12 | Prompts mestres | `prompts/screen-understanding-master.md`, `prompts/visual-extraction.md` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | NEEDS_REVIEW | Não lidos. Contêm o termo do domínio e estão em português |
| C13 | Biblioteca de padrões de interação | `knowledge/conventions/syscom-interaction-patterns.json` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | Conteúdo é conhecimento do SYSCOM. O formato é candidato a contrato futuro. Estrutura vista via scripts, arquivo não lido por inteiro |
| C14 | Biblioteca de rotas de lookup | `knowledge/conventions/syscom-lookup-routes.json` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | A escada de status foi generalizada em `schemas/lookup.schema.json` |
| C15 | Steering do agente atual | `.kiro/steering/syscom-docs.md` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | REWRITE_GENERIC | Princípios viram `AGENT.md` e `policies/`. Caminhos do projeto e do ERP ficam no SYSCOM |
| C16 | Workflow de publicação | `.github/workflows/deploy-k4-review-pages.yml` | PIPELINE_INFRASTRUCTURE | low | medium | KEEP_IN_SYSCOM | Publicação manual. A ideia "nenhuma imagem no site" está em `policies/safety-and-isolation.md` |
| C17 | Dados do lote K4 | `data/k4/` (788 arquivos, 17 MB) | SYSCOM_SPECIFIC_KNOWLEDGE | none | high | KEEP_IN_SYSCOM | Conhecimento e artefatos do SYSCOM. Não duplicar |
| C18 | Dados do piloto, telas, inventário, navegação, processamento | `data/pilot`, `data/screens`, `data/inventory`, `data/navigation`, `data/processing` (398 arquivos) | SYSCOM_SPECIFIC_KNOWLEDGE | none | high | KEEP_IN_SYSCOM | Idem |
| C19 | Capturas brutas | `inputs/` (775 arquivos, 48,6 MB) | SYSCOM_SPECIFIC_KNOWLEDGE | none | high | KEEP_IN_SYSCOM | Fonte primária. Nunca copiada |
| C20 | Portais de revisão publicados | `site/` (48 arquivos) | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | Saída publicada |
| C21 | Saídas geradas e rascunhos | `output/`, `tmp/`, `_tmp_check_questions.py` | EXPERIMENTAL_OR_LEGACY | none | high | IGNORE_LEGACY | Arquivo solto na raiz e pastas de trabalho |
| C22 | PoC de exploração em runtime | `_archive/runtime-explorer-poc/` | EXPERIMENTAL_OR_LEGACY | low | high | IGNORE_LEGACY | PowerShell com UIA e Win32. Inspiração para o adaptador de runtime. Só nomes lidos |
| C23 | Utilitários de evidência | `tools/evidence/k41_common.py` | PIPELINE_INFRASTRUCTURE | high | medium | EXTRACT | `sha256_file`, `check_png`, escrita determinística, caminho longo no Windows, detecção de mojibake. `norm_key` tem stopwords em português |
| C24 | Parser de nomes de arquivo | `tools/evidence/k41_filename_parser.py` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | Heurísticas em português sobre nomes escritos por pessoas. O conceito cabe num plugin do adaptador de filesystem |
| C25 | Ingestão de evidência complementar | `tools/evidence/ingest_k4_complementary_evidence.py` | EXPERIMENTAL_OR_LEGACY | low | high | IGNORE_LEGACY | Execução única (`EXPECTED_PNG = 518`) |
| C26 | Indexador de capturas | `tools/full-capture-indexer/` | PIPELINE_INFRASTRUCTURE | medium | medium | ADAPT | sha256, IHDR, grupos duplicados por hash. Raiz fixa |
| C27 | Agrupador de telas | `tools/screen-grouper/` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | REWRITE_GENERIC | Ideia: candidatos não são decisão, decisão registrada com base. Baseado em prefixos de nome em português |
| C28 | Gerador da visão derivada do conhecimento | `tools/pilot/build_transfer_package.py` | GENERIC_AGENT_CAPABILITY | medium | high | REWRITE_GENERIC | `collect_conflicts`, `current_knowledge_status`, ids definidos e referenciados são reaproveitáveis. Caminhos K3 fixos, flag global, texto de hipótese fixo |
| C29 | Invariantes do validador de pacote | `tools/pilot/validate_transfer_package.py` (G3 a G5, R1 a R4, R8 a R10, SQ1, SQ2, SQ10, SQ13) | GENERIC_AGENT_CAPABILITY | high | medium | EXTRACT | Reimplementados no `core/pack.py` contra os novos schemas |
| C30 | Checagens de expectativa do piloto | `tools/pilot/validate_transfer_package.py` (E01 a E29) | SYSCOM_SPECIFIC_KNOWLEDGE | none | high | KEEP_IN_SYSCOM | Strings de telas específicas |
| C31 | Limpeza semântica e regra cross-evidence | `tools/pilot/semantic_cleanup.py` | GENERIC_AGENT_CAPABILITY | medium | medium | ADAPT | Afirmação INFERRED que cruza capturas com agrupamento aberto fica bloqueada para promoção. `CLAIM_SECTIONS` assume seções de GUI |
| C32 | Derivação do modelo de tela | `tools/pilot/derive_screen_model.py` | EXPERIMENTAL_OR_LEGACY | low | high | IGNORE_LEGACY | Depende da semente manual do piloto |
| C33 | Auxiliares do piloto | `tools/pilot/build_bundle.py`, `summarize.py`, `checkpoint.py` | EXPERIMENTAL_OR_LEGACY | low | high | NEEDS_REVIEW | `build_bundle` não lido. `checkpoint.py` é só atalho sobre `batch_run` |
| C34 | Testes de determinismo e de estado | `tools/pilot/test_*.py`, `tools/batch/test_batch_run.py`, `tools/review/test_derive_review_class.py` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | Abordagem boa (rodar a cadeia duas vezes e comparar sha256). Função `check()` própria, contagens presas a dados, sem runner nem CI |
| C35 | Derivação da classe de revisão | `tools/review/derive_review_class.py` | GENERIC_AGENT_CAPABILITY | high | high | REWRITE_GENERIC | Função pura e determinística. Presa ao formato do bundle de tela e a textos em português. Conceito levado a `core/escalation.py` |
| C36 | Motores de ingestão de resposta humana | `tools/review/ingest_k4_human_review.py`, `ingest_k4_2_human_review.py`, `apply_k4_2_round2_knowledge.py` | GENERIC_AGENT_CAPABILITY | high | high | REWRITE_GENERIC | Camada aditiva, quarentena, conferência de trecho literal, idempotência por `review_id`, resolução com histórico. Contagens e ids fixos |
| C37 | Tabelas de interpretação de respostas | `tools/review/k4_1_answer_rules.py`, `k4_1_lookup_route_rules.py`, `k4_2_round2_rules.py` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | Regras escritas à mão por pergunta. Só as convenções (disposições, escopo, `not_asserted`) são extraíveis |
| C38 | Construtor de perguntas de revisão | `tools/review/k4_human_review_questions.py` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | ADAPT | Esqueleto genérico (modos de resposta, tags, filtro por `requires`, grupos de deduplicação). Conteúdo do SYSCOM. Schema de exportação difere do esperado pelo ingestor |
| C39 | Construtor de rotas de lookup | `tools/review/build_k4_1_lookup_routes.py` | GENERIC_AGENT_CAPABILITY | medium | high | ADAPT | Agrupamento por chave normalizada, escada de status, herança de âncora. Stopwords e marcas de nome de arquivo em português |
| C40 | Portais de revisão e QA | `tools/review/` (portais, `publish_k42_sync.py`, `md_to_html.py`, `qa_*`, assets; cerca de 25 arquivos) | PIPELINE_INFRASTRUCTURE | medium | high | NEEDS_REVIEW | Páginas estáticas offline. Não lidos. `publish_k42_sync.py` grava fora do workspace |
| C41 | Máquina de estado do lote | `tools/batch/batch_run.py` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | `pending`, `processing`, `completed`, `failed`; `claim`, `requeue`, `reopen`; recusa manifest congelado |
| C42 | Checkpoints por estágio e plano de retomada | `tools/batch/k4_stage_state.py` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | 8 estágios fixos. Estágio só é confiável se o sha256 de cada artefato confere |
| C43 | Biblioteca de hash, snapshot e IO atômico | `tools/batch/k4_hardening_lib.py` | PIPELINE_INFRASTRUCTURE | high | medium | EXTRACT | Hash, JSON canônico, escrita atômica, `git_state`, proteção de raiz, snapshot, drift. Listas de arquivos protegidos fixas |
| C44 | Registro de execução | `tools/batch/k4_execution_record.py` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | Épocas de snapshot e bloqueio por drift de conhecimento |
| C45 | Cache de inspeção visual | `tools/batch/k4_visual_cache.py` | PIPELINE_INFRASTRUCTURE | medium | medium | ADAPT | Chave `sha256` + estágio. Invalida só por mudança de hash |
| C46 | Dry-run de pré-voo | `tools/batch/k4_dry_run.py` | PIPELINE_INFRASTRUCTURE | high | medium | ADAPT | 18 checagens (DR01 a DR18); as genéricas são DR01, 02, 04, 05, 10, 12, 14, 15, 18 |
| C47 | Seleção estratificada determinística | `tools/batch/k4_select.py` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | REWRITE_GENERIC | Técnica genérica. Universo, constantes e caminho do ERP fixos no código |
| C48 | Triagem de candidatas | `tools/batch/k4_remaining_preflight.py`, `k4_candidate_filter.py`, `k4_screen_check.py` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | NEEDS_REVIEW | Não lidos. Princípio documentado: sinal fraco nunca exclui sozinho |
| C49 | Gerador de artefatos a partir de sementes | `tools/batch/k4_generate.py` | SYSCOM_SPECIFIC_KNOWLEDGE | medium | high | REWRITE_GENERIC | Gera bundle e `documentation.md`. Flag global `REUSE`; regras DSF do SYSCOM |
| C50 | Reuso knowledge-first | `tools/batch/k4_2_reuse.py` | GENERIC_AGENT_CAPABILITY | high | high | ADAPT | Mecanismo genérico (aplica padrão como INFERRED, suprime pergunta já coberta). Portões usam rótulos em português |
| C51 | Taxonomia de perguntas e normalização em famílias | `tools/batch/k4_2_review.py`, `tools/batch/k4_2_correction.py` | GENERIC_AGENT_CAPABILITY | medium | high | ADAPT | Cinco classes de pergunta (`MUST_ASK_HUMAN` a `LOW_VALUE`). O dicionário `CURATED_HUMAN` é julgamento manual do SYSCOM |
| C52 | Scripts da rodada K4.2 | `tools/batch/k4_2_prepare.py`, `k4_2_metrics.py`, `k4_2_post_human_rescore.py`, `k4_2_round2_baseline.py`, `measure_k4_2_round2.py`, `qa_k4_2_*.py` | EXPERIMENTAL_OR_LEGACY | low | high | KEEP_IN_SYSCOM | Específicos da rodada. Não lidos |
| C53 | Orquestrador do lote canário | `tools/batch/run_k4_canary.py` | PIPELINE_INFRASTRUCTURE | high | high | REWRITE_GENERIC | Orquestra tudo, importa 12 módulos, manipula `sys.path` |
| C54 | QA e relatório do hardening | `tools/batch/qa_k4_hardening.py`, `build_k4_hardening_report.py` | PIPELINE_INFRASTRUCTURE | medium | medium | NEEDS_REVIEW | 85 checagens segundo a documentação. Não lidos |
| C55 | Métricas de eficiência | `tools/batch/k4_efficiency.py` | PIPELINE_INFRASTRUCTURE | medium | medium | ADAPT | `HUMAN_DEPENDENCY_RATE` mede o princípio "humano só quando necessário" |
| C56 | Painel offline do lote | `tools/batch/k4_dashboard.py` e assets | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | KEEP_IN_SYSCOM | Rodapé fixo e biblioteca de padrões do SYSCOM |
| C57 | Orquestrador legado D1 a D7 | `tools/run_pipeline.py` | EXPERIMENTAL_OR_LEGACY | none | high | IGNORE_LEGACY | Recusa rodar sem flag explícita de legado |
| C58 | Construtores D4 a D7 | `tools/consolidate/`, `tools/manual-builder/`, `tools/qa/` | EXPERIMENTAL_OR_LEGACY | low | high | IGNORE_LEGACY | Trilha congelada. Princípios reaproveitáveis: não embutir captura nem razão social sem decisão explícita |
| C59 | Navegação menu e tela (D5) | `tools/navigation/build_navigation.py` | SYSCOM_SPECIFIC_KNOWLEDGE | low | high | NEEDS_REVIEW | Trilha legada. Não lido por inteiro; casa tokens com stopwords em português |
| C60 | Validador de JSON Schema | `tools/schema/validate_schema.py` | PIPELINE_INFRASTRUCTURE | high | medium | REWRITE_GENERIC | Subconjunto em biblioteca padrão. Sem `if/then/not`; caminho de schema fixo. Substituído por `core/schema_lite.py` |
| C61 | Validadores do piloto | `tools/schema/validate_pilot_artifacts.py`, `validate_pilot_manifest.py` | EXPERIMENTAL_OR_LEGACY | none | high | IGNORE_LEGACY | Não lidos. Manifesto do piloto K3 |

## 5. Contagens

Contagens derivadas da tabela da seção 4 (61 linhas). O teste `tests/test_audit_document.py` recalcula e compara.

| Classificação | Componentes |
|---|---|
| GENERIC_AGENT_CAPABILITY | 13 |
| SYSCOM_SPECIFIC_KNOWLEDGE | 21 |
| PIPELINE_INFRASTRUCTURE | 18 |
| EXPERIMENTAL_OR_LEGACY | 9 |
| Total | 61 |

| Ação recomendada | Componentes |
|---|---|
| EXTRACT | 3 |
| ADAPT | 21 |
| KEEP_IN_SYSCOM | 15 |
| REWRITE_GENERIC | 9 |
| IGNORE_LEGACY | 7 |
| NEEDS_REVIEW | 6 |
| Total | 61 |

Status das 25 capacidades da seção 3 (contagem recalculada pelo mesmo teste):

| Status | Capacidades |
|---|---|
| IMPLEMENTED | 14 |
| PARTIAL | 9 |
| DOCUMENTED_ONLY | 0 |
| NOT_FOUND | 2 |
| Total | 25 |

## 6. Achados arquiteturais

1. **Modelo centrado em tela de formulário.** Ids `c###`, `b###`, `g###`, tipos de componente e "item crítico" assumem GUI. Para código-fonte, banco e API só existem tipos de fonte e perguntas abertas. A Foundation 1.0 mantém `screen` como conceito, mas com `surface_kind` aberto, e representa regras e fatos de fontes não visuais por `fact` e `business-rule`. Isso ainda é uma limitação: entidades e esquemas de banco não têm registro próprio (adiado, ver seção 7).
2. **Dois eixos de revisão que não se falam.** A classe por tela (`review_class`) e a classe por pergunta (`MUST_ASK_HUMAN` etc.) são independentes. Na rodada K4.2 houve zero telas em `HUMAN_DECISION` e, ainda assim, 17 perguntas obrigatórias ao gestor, escolhidas à mão. A Foundation 1.0 usa um único registro de revisão com rota.
3. **Definição de VALIDATED inconsistente.** O documento aceita runtime, banco e wiki como fonte; o schema do pacote aceita só `human_review`; as rotas de lookup usam `HUMAN_CONFIRMED` para fonte humana sem nome. A Foundation 1.0 fixa três métodos (`human_review`, `runtime_test`, `authoritative_source`) e exige sempre uma pessoa responsável nomeada e uma data.
4. **Conflito resolvido não cabe no contrato.** O schema do pacote fixa `resolved = false`. A resolução da rodada K4.2 foi para um campo lateral. Quarentena é convenção fora do schema, com vocabulário próprio. A Foundation 1.0 tem `contradiction` com estados `OPEN` e `RESOLVED` (histórico sempre preservado) e `epistemic.quarantine` no schema comum.
5. **Hash da fonte não é conferido onde importa.** No lote há proteção forte. No pacote, `sources[].sha256` é anulável e o validador não o confere. A Foundation 1.0 fixa o hash na proveniência e confere contra a fonte.
6. **AI_RECHECK sem executor e gatilho de alto impacto sem código.** Existem fila, estado e re-derivação, mas nenhum executor. O gatilho "comportamento de alto impacto" ficou como pendência de automação. A Foundation 1.0 torna o impacto um campo explícito (`business-rule.impact`) e uma regra da escada de escalonamento.
7. **Interpretação de respostas escrita à mão, por pergunta e por rodada.** Arquivos de 30 a 50 KB de regras e contagens esperadas fixas. A integridade é boa (falha se regras e respostas desalinham), mas não escala. A Foundation 1.0 move os efeitos da resposta para dentro do registro (`answer.effects`).
8. **Caminhos, datas e ids fixos.** Inclui o caminho de instalação do ERP numa lista de proibição, datas constantes e um script que publica fora do workspace. A Foundation 1.0 proíbe caminho absoluto nos contratos públicos e resolve o workspace por configuração local.
9. **Duplicação e monólitos.** Pelo menos oito implementações de sha256; `slugify`, `fold` e `norm` repetidos; arquivos de até 73 KB; `sys.path.insert` na maioria dos scripts; módulos que carregam dados no import (`k4_2_review.py`). O núcleo novo tem módulos pequenos e sem efeito colateral no import.
10. **Versões espalhadas.** Pelo menos 17 identificadores de versão com prefixos `syscom-`, `k3-`, `k4-` e nomes de fase no nome do arquivo. A Foundation 1.0 usa um esquema único (`urn:mr-software-intelligence:schema:<nome>:<versão>`) e nenhum nome de fase.
11. **Contratos desalinhados.** O exemplo concreto verificado é o schema de exportação das perguntas versus o esperado pelo ingestor. Mostra a necessidade de teste de contrato com um único ponto de verdade.
12. **Testes sem runner.** Scripts com `check()` próprio, contagens presas a dados reais, sem CI. Há `__pycache__` na árvore de trabalho. A Foundation 1.0 usa `unittest` da biblioteca padrão e dados sintéticos.
13. **Sem change intelligence.** Nada para comparar versões do software alvo ou ler notas de versão. É capacidade nova, não extração.
14. **Portabilidade de modelo já é boa no código.** O contrato com o modelo são arquivos semente. O ponto a revisar são os prompts mestres (não lidos), ajustados ao SYSCOM e ao português.

## 7. O que a Foundation 1.0 fez com cada achado

| Achado | Artefato na Foundation 1.0 |
|---|---|
| 1 | `schemas/screen.schema.json`, `fact`, `business-rule`; entidades adiadas |
| 2 | `schemas/human-review.schema.json`, `core/escalation.py`, `policies/escalation.policy.json` |
| 3 | `schemas/common.schema.json` (`epistemic.validation`), `policies/evidence-discipline.md` |
| 4 | `schemas/contradiction.schema.json`, `epistemic.quarantine`, invariantes em `core/pack.py` |
| 5 | `epistemic.provenance[].source_sha256`, checagem em `core/pack.py` |
| 6 | `business-rule.impact`, regra de alto impacto em `core/escalation.py` |
| 7 | `human-review.answer.effects` |
| 8 | `software-pack-manifest.schema.json` (workspace por configuração local), teste de caminhos absolutos |
| 9 | `core/` com módulos pequenos e sem efeito colateral no import |
| 10 | Esquema de ids de schema único |
| 11 | `tests/` com testes de contrato |
| 12 | `tests/` em `unittest`, dados sintéticos em `evaluations/fixtures/` |
| 13 | `schemas/release-change.schema.json`, `core/impact.py`, skill `change-intelligence` |
| 14 | `adapters/llm/` separado do núcleo, teste de neutralidade de provedor |

Decisão de escopo: a entidade (`Entity`) foi avaliada e adiada. No SYSCOM ela aparece de forma implícita (assunto de fatos, destino de lookups). Criar o schema agora seria especulativo. Será reavaliada na primeira extração que precise de esquema de banco.

## 8. Recomendação para a extração

Ordem sugerida para o próximo marco (Agent Extraction 1), sempre contra os contratos da Foundation:

1. Biblioteca de hash, JSON canônico, escrita atômica e snapshot (C43, C23). Menor acoplamento, maior reuso.
2. Estado de lote e checkpoints (C41, C42, C44) com grafo de estágios configurável.
3. Derivação de rota de revisão (C35) sobre o schema de `human-review`, incluindo o executor de `AI_RECHECK` que o SYSCOM nunca teve.
4. Motor de ingestão de respostas guiado por `answer.effects` (C36), sem tabelas por pergunta.
5. Adaptadores de fonte para capturas e filesystem (C26) com `fingerprint`.

Antes disso, resolver os itens `NEEDS_REVIEW` (C12, C33, C40, C48, C54, C59) que podem mudar a classificação, em especial os prompts mestres (C12).
