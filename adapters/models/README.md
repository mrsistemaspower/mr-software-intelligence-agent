# adapters/models/

Fronteira dos adaptadores de modelo: o que o agente exige de QUALQUER modelo, sem conhecer nenhum. Aqui mora o código neutro de provedor; nada nesta pasta importa SDK, abre rede ou guarda credencial.

## Contrato

O núcleo (`core/model_adapter.py`) exige só isto de um adaptador:

| Membro | Faz |
|---|---|
| `descriptor` | `AdapterDescriptor`: `adapter_id`, `adapter_version`, `capabilities`, `data_boundary`, `deterministic` e, se houver, `model_id`, `config_fingerprint`, `profile_id` |
| `execute(task)` | recebe uma `ModelTask` estruturada e devolve um `ModelResult`: a saída estruturada OU uma falha técnica. Nunca os dois |

O adaptador decide uma coisa: enviar a tarefa a um modelo capaz e devolver uma resposta estruturada. Quem decide o quê rechecar, com que evidência, que forma a resposta deve ter, se ela é aceitável e para onde segue é o agente. O que um adaptador devolve é validado, nunca confiado.

Contagem de tokens (`usage`) é opcional. Rede, SDK e credencial não fazem parte do contrato.

## Arquivos

| Arquivo | O que é |
|---|---|
| `base.py` | `BaseModelAdapter`: base opcional. Escreva só `_invoke` e herde a identidade validada, a conversão do retorno em `ModelResult` e a tradução de tempo esgotado e indisponibilidade em falhas repetíveis, sem copiar a mensagem da exceção |
| `scripted.py` | `ScriptedModelAdapter` (responde por um roteiro) e `RuleBasedModelAdapter` (deriva a resposta da tarefa por uma função pura). Sintéticos e determinísticos, para provar o contrato sem modelo e sem rede |

## Como cumprir o contrato

1. **Capacidades honestas.** Declare só o que o modelo faz: `structured-output` (sempre exigido pelo executor de `AI_RECHECK`), `vision` (obrigatória para evidência do tipo imagem), `long-context`, `tool-use`. Um adaptador sem a capacidade exigida não é chamado.
2. **Fronteira de dados honesta.** `LOCAL`: o dado não sai do ambiente. `PRIVATE`: vai por rede controlada por quem usa. `EXTERNAL`: vai a um terceiro. Evidência sensível, ou não conferida, nunca vai a um adaptador `EXTERNAL`; o ambiente e cada pedido podem apertar o limite.
3. **Determinismo honesto.** `deterministic: true` só para um adaptador que, com a mesma tarefa, devolve sempre a mesma saída. O resultado só afirma reprodutibilidade nesse caso.
4. **Falha é dado.** Falha técnica volta como `ModelFailure` (`TIMEOUT`, `UNAVAILABLE`, `REFUSED`, `ERROR`) com `retryable`. Não copie para a mensagem texto da exceção que possa trazer endereço, caminho ou credencial.
5. **Nada de caminho local, credencial ou chave** em descritor, configuração, resultado ou mensagem. A configuração do modelo entra só como impressão digital (`config_fingerprint`).

## Onde ficam os adaptadores de provedor

Os perfis de ambiente continuam em `adapters/llm/` (`*.profile.json`), a única camada que pode nomear um provedor. `AdapterDescriptor.profile_id` aponta para um deles. Um adaptador de provedor remoto ou de modelo local é um módulo próprio, ao lado do seu perfil em `adapters/llm/<perfil>/`, que herda `BaseModelAdapter`, traz o SDK como dependência PRÓPRIA (nunca do núcleo) e declara capacidades e fronteira reais. Nenhum foi implementado: esta pasta entrega o contrato portátil primeiro.

## Fora do escopo

Nenhuma chamada de rede, nenhuma API de provedor, nenhuma instalação de SDK e nenhum armazenamento de credencial. Ver [`docs/AGENT_EXTRACTION_3.md`](../../docs/AGENT_EXTRACTION_3.md).
