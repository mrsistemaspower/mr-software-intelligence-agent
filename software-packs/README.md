# software-packs/

Um **Software Knowledge Pack** é a unidade de conhecimento sobre um software. O agente (núcleo, skills, adaptadores) é genérico; cada software tem o seu pacote. Trocar de software é trocar de pacote, sem reescrever o agente.

## Layout

```
software-packs/<software-id>/
  manifest.json                contrato público do pacote (obrigatório)
  software.json                metadados do software e disponibilidade das fontes (obrigatório)
  integration.reference.json   mapa de referência ao workspace externo (quando há integração)
  local.config.example.json    exemplo de configuração local de desenvolvimento (opcional)
  README.md                    leitura humana
  sources.json                 coleções de registros, uma por arquivo, só as declaradas no manifesto
  evidence.json
  facts.json
  screens.json
  interactions.json
  lookups.json
  workflows.json
  business-rules.json
  contradictions.json
  reviews.json
  releases.json
  docs/                        documentação gerada (opcional)
```

Cada coleção é um arquivo `{ "collection": "<nome>", "records": [ ... ] }`. O schema de cada registro está em `schemas/` (ver `schemas/README.md`).

## Onde mora o conhecimento

Há dois modos, declarados em `knowledge_ownership.canonical_location`:

| Modo | `canonical_location` | O pacote contém | Coleções |
|---|---|---|---|
| Conhecimento num workspace externo | `external-workspace` | manifesto, metadados, referência de integração | nenhuma (o schema do manifesto impõe `collections: []`) |
| Conhecimento no pacote | `this-pack` | tudo, inclusive registros | as declaradas |

No modo externo, o pacote **não duplica** conhecimento. Ele só aponta: onde o workspace está (por variável de ambiente ou configuração local), quais raízes importam e quais formatos nativos existem.

## Regras

- `software_id` é igual ao nome do diretório (exceto o modelo `_template`).
- O manifesto e a referência de integração não contêm caminho absoluto. O workspace é resolvido por `locator` (variável de ambiente) ou por `local.config.json` (ignorado pelo git; só o `.example` é versionado).
- Acesso ao workspace é sempre `read-only`.
- `isolation.leak_terms` lista os termos que identificam o software. Nenhum deles pode aparecer no núcleo, nos schemas, nas skills, nos adaptadores, nos workflows, nas políticas, neste arquivo nem em outro pacote.
- Ids são tipados (`src:`, `evd:`, `fct:`, `scr:`, `int:`, `lku:`, `wfl:`, `rul:`, `ctr:`, `rev:`, `rel:`) e únicos dentro do pacote.
- Toda referência entre registros precisa existir. Hash fixado na proveniência precisa bater com o da fonte.

## Validação

```
py -m core validate-pack software-packs/<software-id>
py -m core validate-packs
py -m core check-isolation
```

Para criar um novo pacote, copie `_template/`.
