---
name: documentation-generation
description: Gera documentação operacional derivada do pacote, rotulando status, confiança e pendências, sem tornar o documento fonte canônica.
version: 0.1.0
status: contract
requires: [schemas/fact.schema.json, schemas/screen.schema.json, policies/evidence-discipline.md, policies/safety-and-isolation.md]
---

# Documentation Generation

> Status: contrato estrutural.

## Intenção

Transformar o conhecimento do pacote em documentos legíveis por quem não conhece a arquitetura, mantendo a honestidade epistêmica.

## Entradas

- Pacote validado e um alvo (funcionalidade, tela, fluxo).
- Perfil: `compact` (uso) ou `detailed` (auditoria, com evidências).

## Saídas

- Documento derivado e regenerável.
- Lista de pendências que impedem declarar a documentação completa.

## Regras

- Todo conteúdo carrega o rótulo do seu status. Texto sem rótulo não é permitido.
- Registros em quarentena e contradições abertas aparecem como pendências, nunca como fatos.
- Nenhuma captura embutida, nenhum dado pessoal, nenhum segredo, nenhum nome de cliente sem decisão explícita.
- O documento nunca é fonte: alterar o documento não altera o conhecimento.

## Fronteiras

- Não cria conhecimento novo. Se faltar informação, a lacuna entra como pendência.
- Não escolhe lado em contradição aberta.

## Depende de

`policies/evidence-discipline.md`; `policies/safety-and-isolation.md`.

## Perguntas em aberto

- Formatos de saída além de Markdown.
- Estratégia de regeneração parcial após mudanças de versão.
- Convenção para indicar documentação desatualizada.
