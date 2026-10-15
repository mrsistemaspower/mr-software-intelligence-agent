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
