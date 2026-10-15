# Modelo de Software Knowledge Pack

Copie este diretório para `software-packs/<software-id>/` e ajuste:

1. `manifest.json`: `software_id` (igual ao nome do diretório), `display_name`, `role` e `isolation.leak_terms` (nome do software e termos do domínio que só ele usa).
2. `software.json`: categoria, fornecedor e disponibilidade das fontes. Deixe `unknown` até conferir.
3. `external_workspace`: se o conhecimento já vive em outro diretório, use `mode: external`, `locator.resolution: env` (ou `local-config`) e defina `knowledge_ownership.canonical_location: external-workspace` com `collections: []`. Se o pacote guarda os registros, use `mode: embedded` e liste as coleções criadas.
4. Valide: `py -m core validate-pack software-packs/<software-id>`.
5. Confira o isolamento: `py -m core check-isolation`.

O contrato completo está em `software-packs/README.md`.
