---
name: erp-reverse-engineering
description: Investiga um ERP desconhecido, da primeira fonte ao pacote de conhecimento, à documentação, ao suporte e à atualização contínua, usando múltiplas fontes e o mínimo de intervenção humana.
version: 0.1.0
status: specified
requires: [schemas/common.schema.json, schemas/source.schema.json, schemas/evidence.schema.json, schemas/screen.schema.json, schemas/human-review.schema.json, policies/evidence-discipline.md, policies/human-escalation.md, policies/safety-and-isolation.md]
---

# ERP Reverse Engineering

## Propósito

Reconstruir, a partir de evidências, o que um ERP faz: suas telas e estados, interações, lookups, fluxos e regras. O resultado é um Software Knowledge Pack confiável o bastante para documentar, dar suporte e acompanhar mudanças, com cada afirmação rotulada por status, confiança e proveniência.

A skill vale para qualquer ERP e para qualquer combinação de fontes. Ela não contém conhecimento de nenhum software específico.

## Quando usar

- Software sem documentação, ou com documentação desatualizada.
- Software legado sem código-fonte, ou com código ao qual só se tem acesso parcial.
- Primeira passada em um software novo para a MR, ou ampliação do conhecimento de um já conhecido.

Não use para: reconstruir só a interface de uma tela isolada (`ui-reverse-engineering`), só extrair afirmações de um manual (`documentation-mining`), ou só comparar versões (`change-intelligence`). Esta skill orquestra essas.

## Entradas e pré-condições

- Um pacote de software válido (`py -m core validate-pack`), mesmo vazio.
- Acesso somente leitura ao menos a uma fonte. Quanto mais tipos de fonte, menos perguntas humanas.
- Nenhuma fonte é copiada ou alterada.

## Saídas

Registros do pacote: `source`, `evidence`, `fact`, `screen`, `interaction`, `lookup`, `workflow`, `business-rule`, `contradiction`, `human-review`, e `release-change` nas atualizações. Mais um relatório de cobertura (o que foi e o que não foi examinado).

## Princípios que governam toda a investigação

1. **Evidência antes de interpretação.** Registre o que foi visto antes de dizer o que significa.
2. **Status e confiança são eixos separados.** Ver `policies/evidence-discipline.md`.
3. **Ausência de evidência não é negação.** Registre cobertura, não "não existe".
4. **Captura não é tela.** Uma tela pode ter várias capturas; o agrupamento é decisão registrada.
5. **Não adivinhe destinos.** Lookup sem destino conhecido fica `UNKNOWN`.
6. **Contradição não se resolve por sobrescrita.** Registre as duas posições e ponha-as em quarentena.
7. **Pessoa só quando necessário.** Percorra a escada de `policies/human-escalation.md` antes de perguntar.
8. **Somente leitura.** Nada é escrito no workspace do software.

## Modos por disponibilidade de fontes

A investigação se adapta ao que existe. O mesmo fluxo vale em todos os modos; muda quanta evidência cada fase encontra.

| Fontes disponíveis | Efeito |
|---|---|
| Só capturas de tela | Estrutura e rótulos com boa cobertura. Comportamento e regras ficam `INFERRED`; destinos de lookup tendem a `UNKNOWN`; muitas lacunas viram `SOURCE_GAP` ou `HUMAN_VALIDATION`. |
| Capturas e documentação | A documentação dá intenção e vocabulário. Divergências entre documento e captura abrem contradições. |
| Mais execução (`runtime`) | Interações podem ser `OBSERVED` com `runtime_tested = true`. Exige autorização e ambiente de teste. |
| Mais esquema de banco | Estrutura de dados e restrições viram evidência de regras. Metadados somente; sem ler linhas. |
| Mais código-fonte | Regras e validações podem ser lidas diretamente. O que o código faz em execução continua `INFERRED` até haver teste. |
| Mais especialista humano | Reduz lacunas, mas cada resposta vale para o que foi revisado e segue a política de validação. |

## Fluxo

```
UNKNOWN SOFTWARE
      |
SOURCE INVENTORY --> SURFACE / MODULE DISCOVERY --> SCREEN & STATE DISCOVERY
      |
INTERACTION ANALYSIS --> ENTITY / LOOKUP DISCOVERY --> WORKFLOW RECONSTRUCTION
      |
BUSINESS-RULE DISCOVERY --> EVIDENCE CLASSIFICATION --> CONTRADICTION DETECTION
      |
TARGETED HUMAN REVIEW --> KNOWLEDGE PACK --> DOCUMENTATION
      |
SUPPORT / TROUBLESHOOTING --> CONTINUOUS UPDATE (volta a SOURCE INVENTORY)
```

As fases não são estritamente lineares: a descoberta numa fase reabre a anterior (por exemplo, um lookup descoberto revela uma tela nova). O que é estrito é o critério de saída de cada uma.

### Fase 0: UNKNOWN SOFTWARE

**Objetivo.** Começar sem suposições e fixar o contexto.

**Atividades.** Confirmar que o pacote existe e valida. Preencher `software.json` com o que se sabe das fontes, deixando `unknown` onde não houver confirmação. Registrar a versão do software quando visível. Definir o escopo da passada (módulo, conjunto de fontes).

**Saídas.** Pacote validado; escopo e versão registrados.

**Critério de saída.** O pacote valida e o escopo está escrito.

**Escalonamento.** Se o escopo for ambíguo, perguntar uma vez a quem solicitou a passada.

### Fase 1: SOURCE INVENTORY

**Objetivo.** Saber exatamente o que existe, com integridade.

**Atividades.** Inventariar fontes por tipo com os adaptadores de `adapters/sources/`. Calcular o hash de cada fonte que for arquivo. Registrar `contains_sensitive_data` como `null` até alguém conferir. Detectar duplicatas por hash (sem apagá-las). Verificar a integridade dos arquivos (por exemplo, imagem corrompida) e registrar o que não pôde ser lido. Registrar a cobertura inicial.

**Saídas.** Registros `source`; relatório de cobertura; lista de fontes ilegíveis.

**Critério de saída.** Toda fonte do escopo está registrada, com hash quando possível, e nada foi alterado.

**Escalonamento.** Fonte ilegível ou ausente vira `SOURCE_GAP`.

**Erros comuns.** Confiar em nome de arquivo como significado; tratar erro de codificação de nome como nome real; processar fonte sem fixar o hash.

### Fase 2: SURFACE / MODULE DISCOVERY

**Objetivo.** Mapear as superfícies de interação e como se organizam em módulos.

**Atividades.** Derivar módulos e caminhos de navegação a partir da organização das fontes e de capturas de menus. Listar superfícies candidatas. Distinguir telas de menus, de índices e de capturas de erro. Registrar a hierarquia como `navigation_path`, com a origem de cada nível.

**Saídas.** `screen` candidatas com `navigation_path`; fatos sobre módulos.

**Critério de saída.** Cada superfície candidata tem ao menos uma captura associada e uma origem de nome.

**Escalonamento.** Nomes divergentes para a mesma superfície ficam como candidatos de agrupamento, não como decisão.

**Erros comuns.** Deduzir caminho de menu a partir do título da janela; tratar sinal fraco isolado (tamanho, nome) como prova de que algo não é uma tela.

### Fase 3: SCREEN & STATE DISCOVERY

**Objetivo.** Descrever cada tela e seus estados com fidelidade.

**Atividades.** Para cada captura, extrair evidências: título, campos, ações, abas, grades, mensagens. Registrar `observed_state` como o estado **daquela captura**, nunca como padrão. Registrar rótulo truncado ou ilegível com confiança baixa. Decidir o agrupamento de capturas em telas (`grouping`): `DECIDED` com responsável, ou `UNRESOLVED` com a razão. Mapear abas sem conteúdo capturado como lacunas de cobertura.

**Saídas.** `evidence`, `screen` com `elements`; decisões de agrupamento.

**Critério de saída.** Toda captura pertence a uma tela e todo elemento tem evidência. Agrupamentos ambíguos estão `UNRESOLVED`.

**Escalonamento.** Leitura ambígua: `AI_RECHECK`. Conteúdo não capturado: `SOURCE_GAP`. Agrupamento ambíguo: `HUMAN_DECISION`.

**Erros comuns.** Criar uma tela por captura; ler estado de captura como comportamento; preencher campo de aba não capturada.

### Fase 4: INTERACTION ANALYSIS

**Objetivo.** Descrever o que cada elemento aciona.

**Atividades.** Para cada elemento acionável, registrar gatilho e efeito esperado. Sem teste em execução o efeito é `INFERRED` com `runtime_tested = false`, e a `basis` explica o raciocínio. Reutilizar comportamento já validado em outro registro como `INFERRED` com proveniência `reused`. Se o ambiente de teste e a autorização existirem, observar o efeito e registrar `OBSERVED` com `runtime_tested = true`. Padrões repetidos (por exemplo, o mesmo comportamento de um botão padrão em muitas telas) são perguntados uma única vez.

**Saídas.** `interaction`; evidências do tipo `behavior` quando houver teste.

**Critério de saída.** Todo elemento acionável tem interação, ou uma razão registrada para não ter.

**Escalonamento.** Efeito de alto impacto sem teste: `HUMAN_VALIDATION`. Dúvida repetida: uma pergunta por família.

**Erros comuns.** Promover a `OBSERVED` um efeito que só foi deduzido da aparência.

### Fase 5: ENTITY / LOOKUP DISCOVERY

**Objetivo.** Descobrir os conceitos de negócio e como as telas se ligam.

**Atividades.** Identificar campos que abrem consultas e registrar o destino. Destino visto numa fonte: `KNOWN` e `OBSERVED` ou `VALIDATED`. Destino provável por padrão repetido: `PROBABLE` e `INFERRED`. Sem informação: `UNKNOWN`, sem referência. Registrar entidades de negócio como fatos (`about` apontando para telas e lookups), já que o registro dedicado de entidade foi adiado. Reunir rótulos equivalentes sem inventar equivalência: similaridade de nome sozinha nunca confirma destino.

**Saídas.** `lookup`; `fact` sobre entidades.

**Critério de saída.** Todo campo de consulta tem um `lookup`. Nenhum destino foi adivinhado.

**Escalonamento.** Destino desconhecido: `SOURCE_GAP` com `needed_source` (execução, banco ou especialista). Rótulos possivelmente equivalentes: `HUMAN_VALIDATION`.

### Fase 6: WORKFLOW RECONSTRUCTION

**Objetivo.** Reconstruir os fluxos de negócio.

**Atividades.** Compor fluxos a partir de telas, interações e lookups, em ordem. Marcar como `INFERRED` o que for composição. Apontar passos sem evidência de ligação. Associar regras aos passos onde se aplicam.

**Saídas.** `workflow`.

**Critério de saída.** Todo passo aponta para registros existentes. Lacunas entre passos estão registradas.

**Escalonamento.** Ordem ou ramificação ambígua: `HUMAN_VALIDATION`.

### Fase 7: BUSINESS-RULE DISCOVERY

**Objetivo.** Identificar as regras que governam o comportamento.

**Atividades.** Extrair regras de mensagens de validação observadas, restrições de banco, código, documentação e declarações de especialistas. Classificar cada regra por `kind` e por `impact` (`low`, `normal`, `high`). Regra de efeito fiscal, financeiro, de exclusão ou de transmissão é `high`.

**Saídas.** `business-rule`.

**Critério de saída.** Toda regra tem `impact` e fonte. Nenhuma regra de alto impacto sustenta resposta autoritativa sem validação.

**Escalonamento.** Regra de alto impacto `INFERRED`: `HUMAN_VALIDATION`, mesmo com confiança alta.

### Fase 8: EVIDENCE CLASSIFICATION

**Objetivo.** Garantir que cada afirmação tem o status e a confiança corretos.

**Atividades.** Revisar todos os registros: `OBSERVED` tem evidência; `INFERRED` tem `basis`; `VALIDATED` tem pessoa, data e método. Separar confiança de status. Reconferir hashes: se uma fonte mudou desde que foi observada, a afirmação precisa ser reavaliada. Aplicar a regra de reuso: o que foi validado em um registro entra como `INFERRED` em outro.

**Saídas.** Registros corrigidos; lista de afirmações que perderam a base.

**Critério de saída.** `py -m core validate-pack` passa sem problemas P040 nem P022.

### Fase 9: CONTRADICTION DETECTION

**Objetivo.** Tornar visíveis as divergências entre fontes.

**Atividades.** Comparar afirmações sobre o mesmo assunto (documentação x captura, declaração humana x observação, versão x versão). Para cada divergência real, abrir uma `contradiction` `OPEN` e pôr cada posição em `epistemic.quarantine` apontando para ela. Não resolver. Distinguir contradição de diferença de versão: diferença entre versões é mudança, tratada em `change-intelligence`.

**Saídas.** `contradiction`; quarentenas.

**Critério de saída.** Toda contradição aberta tem todas as posições em quarentena (`validate-pack` não reporta P028).

**Escalonamento.** `HUMAN_DECISION` para escolher, ou nova evidência que resolva.

### Fase 10: TARGETED HUMAN REVIEW

**Objetivo.** Gastar o tempo humano só no que é necessário.

**Atividades.** Rotear cada item aberto pela escada (`core.escalation.decide`). Registrar as tentativas anteriores em `attempted_before`. Agrupar perguntas equivalentes (`core.escalation.dedupe_questions`). Redigir perguntas com contexto, alternativas e o que se supõe. Ao receber a resposta, registrar `answer` com pessoa nomeada, trecho literal e efeitos. Respostas que contradizem a evidência abrem contradição; não sobrescrevem.

**Saídas.** `human-review`; registros validados, refinados ou contraditos; quarentenas retiradas.

**Critério de saída.** Nenhuma revisão humana sem `attempted_before`. Toda validação aponta para a revisão que a gerou.

**Métrica.** Fração de itens que chegaram a rota humana sobre o total examinado. A tendência esperada é de queda a cada passada.

### Fase 11: KNOWLEDGE PACK

**Objetivo.** Consolidar o conhecimento em um pacote íntegro.

**Atividades.** Gravar registros nas coleções declaradas no manifesto. Validar o pacote por inteiro. Registrar o estado do workspace observado (por exemplo, versão e hash). Conferir o isolamento (`py -m core check-isolation`).

**Saídas.** Pacote validado.

**Critério de saída.** Pacote sem problemas e sem termos de outros softwares.

### Fase 12: DOCUMENTATION

**Objetivo.** Produzir documentação derivada e regenerável.

**Atividades.** Usar `documentation-generation`. Documento é visão derivada, nunca fonte canônica. Todo conteúdo carrega o rótulo do status. Pendências (lacunas, contradições abertas, regras não validadas) aparecem de forma visível.

**Saídas.** Documentos em perfis compacto e detalhado.

**Critério de saída.** Nenhuma captura embutida, nenhum dado pessoal, nenhum segredo.

### Fase 13: SUPPORT / TROUBLESHOOTING

**Objetivo.** Responder perguntas de uso com o que o pacote sustenta.

**Atividades.** Usar `troubleshooting`. Responder combinando tela, fluxo, regras, erros conhecidos, dependências, versão e evidência. Não usar registros em quarentena como apoio autoritativo. Dizer o que não se sabe.

**Saídas.** Respostas com citações e incertezas explícitas.

**Critério de saída.** Nenhuma afirmação sem registro de apoio.

### Fase 14: CONTINUOUS UPDATE

**Objetivo.** Manter o conhecimento vivo sem refazer tudo.

**Atividades.** Usar `change-intelligence`. A cada nova versão ou nova fonte, detectar evidência alterada por hash, calcular o conhecimento afetado (`core.impact.plan_reanalysis`), reanalisar só as regiões afetadas, revalidar ou invalidar fatos, e atualizar a documentação. Validações humanas valem para a versão em que foram dadas.

**Saídas.** `release-change`; plano de reanálise; revisões por perda de validação.

**Critério de saída.** Todo registro afetado tem ação cumprida ou revisão aberta.

## Operação em escala

- **Determinismo.** Ordene o trabalho por critério estável (por exemplo, hash da fonte). Não use relógio nem sorteio dentro de qualquer hash ou identificador.
- **Checkpoints.** Registre o fim de cada fase por unidade de trabalho, com hash dos artefatos produzidos. Uma unidade só é considerada pronta se os hashes conferem; existir arquivo não basta.
- **Retomada.** Ao retomar, reconfira as entradas. Se o hash de uma fonte ou o conhecimento de apoio mudou, refaça a unidade; se não, pule.
- **Reuso incremental.** Aplique conhecimento validado como `INFERRED` antes de perguntar; suprima perguntas já cobertas.
- **Seleção de amostra.** Para uma primeira passada em corpus grande, selecione de forma determinística e estratificada (por exemplo, o terço mais denso por tamanho, mais o restante ordenado por hash), e registre o critério.
- **Escrita.** Atômica e idempotente. Não regravar quando o conteúdo é idêntico.

## Critérios de qualidade

1. `py -m core validate-pack <pacote>` sem problemas.
2. `py -m core check-isolation` sem achados.
3. Nenhum `VALIDATED` sem pessoa nomeada, data e método.
4. Nenhum `OBSERVED` de efeito sem teste em execução.
5. Nenhum lookup `UNKNOWN` com referência.
6. Toda contradição aberta com posições em quarentena.
7. Toda revisão de rota humana com `attempted_before`.

## O que esta skill não faz

- Não escreve no workspace do software.
- Não executa o software em produção.
- Não decide agrupamentos, contradições ou validações no lugar de uma pessoa.
- Não preenche lacunas com conhecimento externo ao pacote.
- Não define formato próprio de conhecimento: usa os schemas de `schemas/`.

## Relação com outras skills

`ui-reverse-engineering` (fases 3 e 4), `documentation-mining` (fases 1 e 7), `workflow-reconstruction` (fase 6), `business-rule-discovery` (fase 7), `database-analysis` (fases 5 e 7), `runtime-exploration` (fase 4), `change-intelligence` (fase 14), `troubleshooting` (fase 13), `documentation-generation` (fase 12).
