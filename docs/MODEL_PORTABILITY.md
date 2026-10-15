# Portabilidade entre modelos

O agente é **agnóstico de modelo**. O modelo de linguagem é o motor cognitivo. A propriedade intelectual da MR fica em metodologia, skills, workflows, schemas, ferramentas, avaliação, políticas e contratos de conhecimento, e não em um provedor, um prompt gigante ou uma API.

O mesmo Foundation deve rodar, com adaptadores apropriados, em Claude, ChatGPT/OpenAI, ambientes compatíveis com Kiro, modelos locais e modelos futuros.

## Princípio: separar instruções de adaptação

| Camada | Pode nomear provedor? | Onde |
|---|---|---|
| Instruções do agente | não | `AGENT.md`, `policies/`, `skills/`, `workflows/` |
| Contratos de dados | não | `schemas/` |
| Núcleo | não | `core/` |
| Perfil de ambiente | sim | `adapters/llm/` |
| Documentação | sim | `docs/`, `README.md` |

`tests/test_isolation.py` impõe isso: os nomes de provedores (lista em `adapters/llm/known-providers.json`) não podem aparecer nas três primeiras linhas da tabela.

## O que o agente assume do ambiente

### Ferramentas abstratas

| Ferramenta | Para quê |
|---|---|
| `read-file` | ler arquivos do pacote e do workspace do software |
| `search` | buscar texto em arquivos |
| `write-file` | gravar registros do pacote e relatórios (nunca no workspace do software) |
| `run-command` | executar `py -m core ...` e os testes |
| `fetch-web` | consultar documentação pública, quando permitido |

Cada perfil de `adapters/llm/` diz como o ambiente fornece cada uma.

### Capacidades

`vision`, `file-read`, `file-write`, `shell`, `web`, `long-context`, `structured-output`. O perfil declara as que exige. Se o ambiente não tem uma capacidade, o agente diz o que não consegue fazer e propõe o caminho alternativo; não finge.

## Perfis

| Perfil | Tipo de ambiente | Como carrega `AGENT.md` |
|---|---|---|
| `claude` | ambiente de agente | arquivo de memória de projeto apontando para `AGENT.md` |
| `openai` | conversa ou agente | instruções do projeto/assistente com `AGENT.md` anexado |
| `kiro` | IDE com agente | steering do workspace que inclui `AGENT.md` por referência |
| `local` | runtime local | instrução de sistema com o conteúdo de `AGENT.md` e um laço de ferramentas simples |

Todos os perfis estão em `draft`: foram escritos a partir do contrato, não verificados em campo. Um perfil só vira `verified` depois de rodar os casos de `evaluations/` no ambiente real.

## Regras de portabilidade

1. **Uma fonte de instruções.** Todo perfil aponta para `AGENT.md`. Nunca copie as instruções para dentro de um perfil.
2. **Dados em JSON neutro.** O contrato com o modelo é o JSON dos schemas. Nada de formatos de saída proprietários.
3. **Sem dependência de tamanho de contexto.** O pacote é consultado por registro. Modelos de contexto curto trabalham por coleção e por resumos derivados; nada exige carregar o pacote inteiro.
4. **Sem dependência de visão obrigatória.** A leitura de capturas pode vir de uma etapa externa. O resultado entra como evidência com `extracted_by.actor` `tool` ou `ai` e confiança que reflete a qualidade da etapa.
5. **Determinismo no que for código.** Roteamento, validação, impacto e isolamento são código, não texto. Dois modelos diferentes chegam ao mesmo resultado nessas partes.
6. **Ferramentas por capacidade, não por nome.** Skills e workflows falam em `read-file`, não no nome da ferramenta de um ambiente.
7. **Falha explícita.** Capacidade ausente é dita, não contornada em silêncio.

## Como verificar a portabilidade

1. `py -m unittest discover -s tests` passa sem nenhum provedor configurado (os testes são código, não chamam modelo).
2. Para um novo ambiente: criar `adapters/llm/<id>.profile.json`, validar pelo schema, escrever o exemplo de carga e rodar os casos de `evaluations/` com o modelo real.
3. Comparar o comportamento em um conjunto fixo de casos: roteamento correto, rótulos de status, "não sei" quando falta evidência, quarentena respeitada.

## O que não fazer

- Não colocar conhecimento de software no perfil de um modelo.
- Não nomear provedor ou ferramenta de ambiente em skills, workflows, políticas ou schemas.
- Não assumir que o modelo "lembra" de uma sessão anterior: o estado é o pacote.
- Não depender de um comportamento específico de um modelo para a correção: quem garante a correção são os schemas, `core/` e os testes.
