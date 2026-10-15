# skills/

Uma skill é um método reutilizável: descreve **como** investigar, sem saber **qual** software. O conhecimento sobre cada software mora nos pacotes de `software-packs/`.

Cada skill tem um diretório com um `SKILL.md`. O arquivo começa com um cabeçalho simples:

```
---
name: <igual ao nome do diretório>
description: <uma frase>
version: <semver>
status: specified | contract
requires: [<caminhos de contratos de que depende>]
---
```

| Status | Significado |
|---|---|
| `specified` | método descrito de ponta a ponta, com fases, critérios de saída e escalonamento |
| `contract` | contrato estrutural: intenção, entradas, saídas e fronteiras. O método detalhado vem depois |

## Skills

| Skill | Status | Papel |
|---|---|---|
| `erp-reverse-engineering` | specified | investigar um ERP desconhecido, da fonte ao pacote de conhecimento e à atualização contínua |
| `ui-reverse-engineering` | contract | reconstruir superfícies de interação, estados e elementos |
| `documentation-mining` | contract | extrair afirmações de documentação existente |
| `workflow-reconstruction` | contract | reconstruir fluxos de negócio a partir de telas, interações e lookups |
| `business-rule-discovery` | contract | descobrir regras de negócio e classificar o impacto |
| `database-analysis` | contract | analisar esquema de banco, quando permitido |
| `runtime-exploration` | contract | observar o software em execução, com autorização e sem efeito colateral |
| `change-intelligence` | contract | comparar versões e reanalisar só o que foi afetado |
| `troubleshooting` | contract | responder "estou na tela X e aconteceu Y" sem inventar |
| `documentation-generation` | contract | gerar documentação derivada do pacote |

## Regras para toda skill

1. Nenhuma skill nomeia um software, um provedor de modelo ou um caminho absoluto. O isolamento é verificado por `tests/test_isolation.py`.
2. Toda skill obedece `policies/evidence-discipline.md`, `policies/human-escalation.md` e `policies/safety-and-isolation.md`.
3. Skill produz registros conformes aos schemas de `schemas/`. Nenhuma skill define formato próprio de conhecimento.
4. Skill declara o que **não** faz. Fronteiras claras evitam sobreposição entre skills.
