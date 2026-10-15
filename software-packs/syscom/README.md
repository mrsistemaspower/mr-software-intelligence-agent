# Pacote SYSCOM

Primeiro caso de treino e validação (`role: first_validation_case`) do MR Software Intelligence Agent.

Este pacote é só uma **referência**. O conhecimento real continua no workspace do projeto SYSCOM, que não é copiado para cá.

| Arquivo | Para quê |
|---|---|
| `manifest.json` | contrato público: papel do pacote, propriedade do conhecimento, estado da integração, termos de isolamento |
| `software.json` | metadados do software e disponibilidade das fontes |
| `integration.reference.json` | raízes do workspace (caminhos relativos), formatos nativos e commit observado na auditoria |
| `local.config.example.json` | exemplo da configuração local de desenvolvimento |

## Como o agente encontra o workspace

1. Variável de ambiente `MR_SYSCOM_WORKSPACE`.
2. Se não existir, o arquivo `local.config.json` deste diretório (não versionado). Para criá-lo, copie `local.config.example.json` e ajuste `workspace_path`.

O caminho do workspace nunca aparece no manifesto nem na referência de integração.

## Regras desta fase

- Acesso somente leitura ao workspace.
- Nenhum conhecimento duplicado aqui (`duplication_policy: forbidden`). O schema do manifesto impede coleções neste pacote enquanto o conhecimento canônico estiver fora.
- `integration.status` é `reference-only`: ainda não há leitura nem escrita automática.

## O que já se sabe

A auditoria do workspace está em `docs/AGENT_FOUNDATION_AUDIT.md`: mapa de componentes, capacidades confirmadas e ordem sugerida de extração.
