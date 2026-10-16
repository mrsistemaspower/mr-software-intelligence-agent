# adapters/

Adaptadores ligam o agente ao mundo exterior. Há três tipos, e eles nunca se misturam.

| Pasta | Liga o agente a | Valida com |
|---|---|---|
| `sources/` | fontes de evidência sobre o software (capturas, arquivos, documentação, código, banco, execução, especialistas) | `schemas/source-adapter.schema.json` |
| `llm/` | ambientes de modelo de linguagem (perfis por provedor); a única camada que pode nomear um provedor | `schemas/llm-profile.schema.json` |
| `models/` | modelos de linguagem pela fronteira neutra `execute(task) -> ModelResult`: a base e adaptadores sintéticos de teste, sem SDK nem rede. Ver [`models/README.md`](models/README.md) | `schemas/model-task.schema.json`, `schemas/model-result.schema.json` |

Neste marco só existem **contratos** (e adaptadores de modelo sintéticos para provar o contrato). Nenhuma integração pesada foi implementada.

## Contrato dos adaptadores de fonte

Todo adaptador de fonte implementa as operações abaixo (independentes de linguagem). Os registros que ele emite seguem `schemas/source.schema.json` e `schemas/evidence.schema.json`.

| Operação | Entrada | Saída | Garantias |
|---|---|---|---|
| `describe()` | nada | o descritor do adaptador | estável; igual ao arquivo `*.adapter.json` |
| `inventory(workspace)` | workspace somente leitura | lista de `Source` | ordenada por `locator.path`; inclui `sha256` quando calculável; `contains_sensitive_data` fica `null` até alguém conferir |
| `fingerprint(source)` | uma fonte | hash sha256, ou ausente se a fonte não é arquivo | determinístico |
| `extract(source, scope?)` | uma fonte e, opcionalmente, um recorte | lista de `Evidence` | ordem estável; cada item com `confidence`; nenhum status e nenhuma interpretação |
| `coverage(source)` | uma fonte | o que foi examinado e o que não foi | permite raciocinar sobre ausência sem confundi-la com negação |

Garantias comuns:

1. **Somente leitura.** `read_only` é sempre `true`. Nenhum adaptador escreve, move, renomeia ou apaga nada no workspace.
2. **Determinismo.** A mesma fonte produz os mesmos registros, na mesma ordem. Carimbos de tempo vêm do chamador.
3. **Falha fechada.** Se o hash de uma fonte mudou desde o registro, a operação reporta divergência e não segue com evidência velha.
4. **Sem interpretação.** Adaptadores extraem; quem atribui status e confiança de afirmação é o agente, com a disciplina de `policies/evidence-discipline.md`.
5. **Sem segredos na saída.** Chaves, senhas e cadeias de conexão são omitidas e substituídas pelo nome da variável.
6. **Autorização.** Adaptadores com `requires_human_authorization: true` (`database-schema`, `runtime`) só operam depois de autorização humana registrada.

## Adaptadores definidos

| Adaptador | Tipo de fonte | Exige autorização humana |
|---|---|---|
| `screenshots` | screenshot | não |
| `filesystem` | filesystem | não |
| `documentation` | documentation | não |
| `dokuwiki` | wiki | não |
| `source-code` | source-code | não |
| `database-schema` | database-schema | sim |
| `runtime` | runtime | sim |
| `release-notes` | release-notes | não |
| `human-expert` | human-expert | não (a própria pessoa é a fonte) |

## Como adicionar um adaptador

1. Crie `sources/<id>.adapter.json` conforme o schema. O `source_kind` precisa existir em `common.schema.json` (`sourceKind`).
2. Liste as operações implementadas, o que o adaptador produz e as capacidades de que precisa.
3. Rode `py -m unittest discover -s tests`.

## Perfis de LLM

Um perfil descreve como o mesmo `AGENT.md` é carregado num ambiente específico e como as ferramentas abstratas (`read-file`, `search`, `write-file`, `run-command`, `fetch-web`) são obtidas ali. Só `llm/` pode nomear provedores. Ver `docs/MODEL_PORTABILITY.md`.
