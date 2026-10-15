# Exemplo: steering do workspace (Kiro)

Salve em `.kiro/steering/agent.md` no workspace onde o agente vai rodar. Mantém uma única fonte de instruções.

```
---
inclusion: always
---

#[[file:AGENT.md]]
```

O arquivo `.kiro/` fica na raiz do workspace do usuário, não neste repositório: o repositório não contém configuração de nenhum provedor na raiz.
