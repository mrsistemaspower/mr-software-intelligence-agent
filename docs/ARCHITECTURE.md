# Arquitetura

O MR Software Intelligence Agent é uma pilha de camadas independentes. Cada camada só conhece a de baixo por um contrato.

```
        +---------------------------+
        |        LLM / Runtime      |   motor cognitivo (qualquer modelo)
        +---------------------------+
                     |
        +---------------------------+
        |           Agent           |   AGENT.md + políticas + workflows (6 verbos)
        +---------------------------+
                     |
        +---------------------------+
        |      Skills + Tools       |   skills/, core/, adapters/
        +---------------------------+
                     |
        +---------------------------+
        |   Software Knowledge Pack |   software-packs/<id>/  (+ workspace externo)
        +---------------------------+
                     |
        +---------------------------+
        |         Software          |   o ERP ou sistema legado estudado
        +---------------------------+
```

A seta aponta no sentido da dependência: o modelo executa o agente; o agente usa skills e ferramentas; estas leem e gravam no pacote; o pacote descreve o software. Nada abaixo conhece o que está acima. O pacote não sabe qual modelo o lê; o núcleo não sabe qual software o pacote descreve.

## Componentes

### LLM Runtime

O modelo de linguagem é o motor cognitivo. Pode ser qualquer um. O que o liga ao agente é um perfil em `adapters/llm/` (como carregar `AGENT.md` e como obter as ferramentas abstratas). Ver `docs/MODEL_PORTABILITY.md`.

### Agent

`AGENT.md` define identidade, missão, disciplina de evidência, limites de raciocínio, escalonamento, isolamento, ciclo de vida, portabilidade e segurança. `workflows/` define a interface: seis verbos (`learn`, `explain`, `ask`, `find-gaps`, `compare`, `document`). `policies/` dá o detalhe normativo.

### Skills

Métodos reutilizáveis em `skills/`. `erp-reverse-engineering` é a principal e está especificada de ponta a ponta (15 fases). As outras nove são contratos estruturais. Uma skill diz **como** investigar; nunca **o quê** um software faz.

### Agent Core

Núcleo pequeno em `core/`, só biblioteca padrão, sem efeito colateral no import:

| Módulo | Responsabilidade |
|---|---|
| `schema_lite` | validação de JSON Schema (subconjunto fechado, `$ref` entre arquivos, `if/then/else`) |
| `epistemics` | regras de status e confiança que os schemas não expressam |
| `pack` | carga e validação de pacotes: referências, unicidade, hash fixado, quarentena, validações ligadas a revisões |
| `escalation` | roteamento de itens para `AI_RECHECK`, `SOURCE_GAP`, `HUMAN_DECISION`, `HUMAN_VALIDATION`; deduplicação de perguntas |
| `impact` | inteligência de mudança: quais registros uma mudança afeta |
| `isolation` | verificação de que o núcleo e as camadas genéricas não conhecem nenhum software nem provedor |
| `serialization` | JSON canônico (`mr-canonical-json/1`): mesmo valor, mesmos bytes; leitura estrita |
| `hashing` | `Digest` e SHA-256 de bytes, de arquivos e de JSON canônico |
| `paths` | caminho lógico portátil x caminho local; detecção de caminho local; caminho longo do Windows |
| `atomic` | escrita atômica e idempotente de bytes, texto e JSON canônico |
| `integrity` | verificação de fonte: "processei esta fonte exata"; ausente, alterada, surgida, ilegível, não declarada |
| `snapshot` | `KnowledgeSnapshot`: estado de conhecimento consumido, com identidade que não depende de relógio |
| `identity` | identidade determinística de artefato derivado (tipo, versão, hashes de fonte, parâmetros, snapshot) |

Os sete últimos módulos vêm da primeira extração do SYSCOM (ver [`AGENT_EXTRACTION_1.md`](AGENT_EXTRACTION_1.md)). Capacidades planejadas e ainda não implementadas (estado de lote, checkpoints, cache, seleção de amostra) são extração do SYSCOM, prevista para os próximos marcos (ver auditoria, seção 8).

### Schemas

Contratos de dados em `schemas/`. Doze conceitos de conhecimento (Software, Source, Evidence, Fact, Screen, Interaction, Lookup, Workflow, BusinessRule, Contradiction, HumanReview, ReleaseChange) mais definições comuns, seis contratos de configuração e um contrato de estado derivado (`knowledge-snapshot`). O bloco `epistemic` é o coração: `status`, `confidence` (separados) e `provenance` (obrigatória).

### Adapters

Dois tipos, nunca misturados:

- `adapters/sources/`: nove adaptadores de fonte (capturas, filesystem, documentação, wiki, código, banco, execução, notas de versão, especialista). Contrato: somente leitura, determinístico, sem interpretação. Hoje só descritores.
- `adapters/llm/`: perfis de ambientes de modelo.

### Workflows

Procedimentos do agente (não confundir com o registro `workflow`, que descreve fluxos do software alvo). Um arquivo por verbo.

### Software Knowledge Pack

Unidade de conhecimento de um software. Dois modos:

- **Externo**: o conhecimento mora num workspace do software. O pacote tem manifesto, metadados e referência de integração. O schema do manifesto impede coleções neste modo, para não duplicar conhecimento.
- **Embutido**: o pacote guarda os registros em coleções.

O contrato está em `software-packs/README.md`.

### Fontes externas

Capturas, arquivos, documentação, wiki, código, banco, execução, notas de versão e especialistas. São sempre somente leitura. Cada uma é registrada como `source` com hash e entra no pacote por proveniência. Nunca são copiadas.

## Fluxo de dados

```
Fonte --(adaptador)--> Source + Evidence
                          |
                  skill interpreta (INFERRED / OBSERVED)
                          |
           Fact / Screen / Interaction / Lookup / Workflow / BusinessRule
                          |
        detecção de contradição --> Contradiction (posições em quarentena)
                          |
        core.escalation --> HumanReview (AI_RECHECK | SOURCE_GAP | HUMAN_*)
                          |
        resposta de pessoa nomeada --> VALIDATED (aponta para a revisão)
                          |
                 validate_pack (invariantes) --> pacote íntegro
                          |
        document / explain / ask  <-- usam só o que o pacote sustenta
                          |
        nova versão --> ReleaseChange --> core.impact --> reanálise do afetado
```

## Invariantes (impostas por `validate_pack` e pelos schemas)

- Ids tipados e únicos; toda referência existe.
- Hash fixado na proveniência confere com o da fonte.
- `OBSERVED` tem evidência; `INFERRED` tem base; `VALIDATED` tem pessoa, data e método.
- Contradição aberta tem todas as posições em quarentena; contradição resolvida não deixa quarentena e preserva as posições.
- Validação humana aponta para uma revisão respondida pela mesma pessoa.
- Lookup `UNKNOWN` não tem referência. Interação não testada em execução nunca é `OBSERVED`.
- Arquivos públicos de pacote não têm caminho absoluto.

## Isolamento

Impõe-se por testes, não por convenção. Cada pacote declara os termos que o identificam; esses termos não podem aparecer nas camadas genéricas nem em outros pacotes. O núcleo não pode ramificar por `software_id`. Provedores de modelo só aparecem em `adapters/llm/` e na documentação. Adicionar um pacote amplia a verificação sem mudar código.

## Como adicionar um software

1. Copiar `software-packs/_template/` para `software-packs/<software-id>/`.
2. Ajustar manifesto (inclusive `isolation.leak_terms`) e metadados.
3. `py -m core validate-pack software-packs/<software-id>` e `py -m core check-isolation`.
4. Rodar as skills pelos verbos de `workflows/`.

Nenhum arquivo em `core/`, `skills/`, `schemas/`, `adapters/`, `workflows/` ou `policies/` muda.

## O que esta fundação ainda não faz

Não há chat, interface web, banco vetorial, servidor de RAG, runtime autônomo, implantação em nuvem, observador contínuo, rastreador de DokuWiki nem integração com o ERP Dolphins. Os adaptadores são contratos. Tudo isso virá em marcos próprios, sobre estes contratos.
