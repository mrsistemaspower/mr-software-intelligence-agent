# Agent Extraction 1: primitivas determinísticas

Primeira extração de capacidades comprovadas no SYSCOM para o Agent Core, feita contra os contratos da Foundation e sem recriar a arquitetura do projeto de origem.

Escopo: hash, JSON canônico, escrita atômica, snapshot de conhecimento, identidade determinística de artefato, verificação de integridade de fonte e portabilidade de caminhos. Tudo é código determinístico, só biblioteca padrão, sem chamada a modelo de linguagem.

Fora de escopo, por decisão: ingestão de revisão humana, executor de `AI_RECHECK`, geração de perguntas, raciocínio sobre lookups, padrões de interação, orquestração de lote, extração de telas, DokuWiki, inspeção de banco, runtime de modelo e observador de mudanças. Ficam para extrações seguintes.

## Fontes estudadas

O `mr-syscom` foi tratado como somente leitura. Dois arquivos foram lidos por inteiro; nenhum foi importado nem executado de dentro do workspace de origem.

| Arquivo (commit `4c652a3`) | Blob | Componente da auditoria |
|---|---|---|
| `tools/batch/k4_hardening_lib.py` | `3923259335b80264a10d2e1e8ffca5351c4a20b0` | C43, ação `EXTRACT` |
| `tools/evidence/k41_common.py` | `5cd02e9b423a8f59f6e84fdde862ec8ca01eda68` | C23, ação `EXTRACT` |

Os dois arquivos eram idênticos ao commit de referência (sem diferença local). Chamadores diretos não foram necessários: o comportamento está inteiro nas funções lidas.

Paridade: os valores de referência (texto canônico, impressões digitais, hashes de arquivos em fronteiras de bloco, veredito de escrita idempotente) foram gerados uma única vez, a partir de uma cópia dos dois arquivos fora do workspace de origem, executada com bytecode desligado. Os testes do agente usam esses valores congelados. Eles não dependem de que o workspace de origem exista nem o tocam.

Classificação do que foi lido, em separado:

| Classe | O que é |
|---|---|
| Comportamento genérico | hash em blocos de 1 MiB; `canon`/`fingerprint` (JSON ordenado, compacto, UTF-8); escrita idempotente com temporário no mesmo diretório e troca; retrato de conhecimento por hash sem cópia e sem relógio; ausência registrada como situação; deriva entre retratos; prefixo de caminho longo do Windows |
| Acoplamento ao SYSCOM | lista fixa de arquivos de conhecimento, pasta de convenções, camada humana de uma rodada, caminhos de entrada e saída de uma fase, identificador de schema com prefixo do projeto |
| Específico do conjunto de dados | contadores por papel (padrões, rotas, respostas), hashes derivados por papel, palavras vazias em português, heurística de mojibake em nomes de captura, lista de pastas protegidas |
| Acidente histórico | caminho absoluto devolvido quando o arquivo está fora da raiz; `NaN` e `Infinity` aceitos; chaves não textuais convertidas em silêncio; temporário com nome fixo e sem limpeza; identidade do snapshot dependente da ordem de declaração; data fixa embutida para parecer determinístico; duas cópias divergentes do mesmo código |

## Invariantes genéricos extraídos

1. Identidade de conteúdo vem de bytes canônicos, nunca de relógio, máquina ou ordem de inserção.
2. Um snapshot descreve o estado de conhecimento consumido; não copia fontes.
3. Uma camada esperada e ausente é parte do estado: quando aparece, o estado muda.
4. Escrever um arquivo nunca deixa o destino pela metade; reescrever o mesmo conteúdo não toca o arquivo.
5. Afirmar "processei esta fonte" é comparar hashes, não confiar em nome ou data.
6. Caminho que entra em contrato portátil é relativo à raiz do conhecimento. A raiz local é detalhe de configuração.

## Acoplamento do SYSCOM rejeitado

Resumo; o detalhe, símbolo a símbolo, está em [`extraction/EXTRACTION_001.json`](extraction/EXTRACTION_001.json).

- Nenhum nome de tela, id, caminho, fase (`K3`, `K4`), contagem fixa, data fixa ou identidade de responsável entrou no núcleo. Um teste varre os arquivos novos atrás desses termos, dos termos de vazamento de todos os pacotes e de nomes de provedores de modelo.
- Nenhuma suposição de Firebird. Nenhum caminho do Windows como constante. A lógica específica do Windows (prefixo de caminho longo) mora em um único módulo, `core/paths.py`, e é inofensiva nos demais sistemas.
- O que depende de formato de mídia ou de estado de lote não entrou: validação estrutural de PNG fica para o adaptador de capturas; estado do controle de versão e proteção de raízes de lote ficam para a extração de estado de lote.

## Componentes novos do Agent Core

Sete módulos pequenos, por responsabilidade, e um schema. Importar qualquer um deles não imprime nem grava nada.

| Módulo | Responsabilidade |
|---|---|
| `core/serialization.py` | contrato `mr-canonical-json/1`: formas compacta e legível, leitura estrita |
| `core/hashing.py` | `Digest` (algoritmo, resumo, tamanho); hash de bytes, de arquivo e de JSON canônico; SHA-256 |
| `core/paths.py` | caminho lógico x caminho local; normalização; detecção de caminho local; prefixo de caminho longo |
| `core/atomic.py` | escrita atômica e idempotente de bytes, texto e JSON canônico |
| `core/integrity.py` | `verify_sources`, `require_intact`, achados por tipo, integração com registros `source` |
| `core/snapshot.py` | `KnowledgeSnapshot`, montagem a partir de entradas ou de um diretório, deriva, verificação em disco |
| `core/identity.py` | `artifact_identity`: id determinístico de artefato derivado |
| `schemas/knowledge-snapshot.schema.json` | contrato JSON do snapshot (`urn:mr-software-intelligence:schema:knowledge-snapshot:1`) |

Exemplo mínimo:

```python
from core.snapshot import ArtifactSpec, snapshot_directory, save_snapshot, verify_snapshot
from core.identity import artifact_identity

specs = [ArtifactSpec("docs/manual.md", "doc"), ArtifactSpec("layers/human.json", "layer")]
shot = snapshot_directory(raiz_local, specs, label="workspace de estudo", software_id="meu-software")
save_snapshot(destino, shot)

verify_snapshot(shot, raiz_local).raise_if_failed()     # levanta IntegrityError se a evidência mudou
ident = artifact_identity("doc.screen", "1", inputs={"docs/manual.md": shot.entry("docs/manual.md").sha256},
                          parameters={"nivel": 2}, snapshot=shot)
```

A versão de `core` passou de 1.0.0 para 1.1.0.

## Garantias de determinismo

Contrato `mr-canonical-json/1`. Este contrato é próprio do agente e não é o RFC 8785 (JCS); não se declara compatível com ele.

| Tema | Regra |
|---|---|
| Codificação | UTF-8 sem BOM; não ASCII sai literal; só o que o JSON obriga é escapado (`"`, `\`, controles; hexadecimal em minúsculas) |
| Chaves | sempre texto; ordenadas por ponto de código Unicode (o JCS ordena por unidade UTF-16); outro tipo de chave é recusado |
| Compacta | sem espaço, sem quebra de linha final; é a forma de hash e de identidade |
| Legível | recuo de 2, separadores `,` e `: `, LF (nunca CRLF), uma quebra de linha final; é a forma de arquivos revisados em diff |
| Números | `int` exato; `float` pela representação mais curta que reconstrói o valor; `NaN` e infinitos recusados; `1` e `1.0` são valores diferentes; `-0.0` é mantido |
| Texto | sem normalização Unicode (NFC e NFD são valores diferentes); substituto isolado recusado |
| Tipos | `dict`, `list`, `tuple` (vira array), `str`, `int`, `float`, `bool`, `None`; escalares só pelo tipo exato; qualquer outro tipo recusado |
| Limites | profundidade 100 (também barra referência circular) |

Garantias verificadas por teste:

- Mesmo valor com chaves inseridas em qualquer ordem produz os mesmos bytes (as 24 permutações de um dicionário).
- Outro processo, em outro diretório e com variáveis de ambiente diferentes (semente de hash, codificação, fuso, língua, nome de máquina e de usuário), produz o mesmo texto canônico, o mesmo snapshot e o mesmo id de artefato.
- Nenhum módulo novo importa relógio, acaso, rede, processo ou ambiente, nem lê atributos que os tragam. O único uso de acaso é o sufixo do nome do arquivo temporário, que não participa de nada persistido.
- Mover o workspace para outro caminho absoluto não muda nenhum id.
- Cada byte alterado muda o hash; cada campo de identidade alterado muda o id.
- Identidades de referência foram congeladas em testes e recalculadas, de forma independente, só com `hashlib` e `json`.

## Semântica do snapshot

```
snapshot_id = "snap:sha256:" + hash( {kind, identity_version, software_id (se houver), artifacts_digest} )
artifacts_digest = hash( entradas ordenadas por caminho lógico )
entrada = {path, role, status: PRESENT|ABSENT, sha256, byte_length}   # ABSENT não tem hash
```

- Dentro da identidade: `software_id`, e para cada artefato o caminho lógico, o papel, a situação, o hash e o tamanho.
- Fora da identidade, de propósito: `created_from`, `parent_snapshot_id` e tudo em `generation` (gerador, `created_at`, notas). Tempo, máquina e modelo de linguagem não mudam o estado de conhecimento. `created_at` é opcional e fornecido por quem chama; o núcleo não lê relógio.
- A ordem de declaração dos artefatos é irrelevante. Duas entradas para o mesmo caminho, ou que só diferem em maiúsculas e minúsculas, são recusadas (não existiriam juntas em sistemas de arquivos que ignoram caixa).
- Um snapshot não pode ser pai de si mesmo, e o pai de um software é do mesmo software.
- Ao reconstruir (`snapshot_from_dict`, `load_snapshot`) o schema, a ordem, `artifacts_digest` e `snapshot_id` são recalculados: adulteração de qualquer campo da identidade é recusada. Arquivo com chave duplicada é recusado.
- Nenhum caminho local entra no snapshot: rótulos e notas que parecem caminho de máquina são recusados.

## Garantias de integridade

`verify_sources` e `verify_snapshot` comparam o esperado com o disco, sem modificar nada, e relatam:

| Achado | Significa |
|---|---|
| `MISSING` | esperado presente, ausente |
| `MUTATED` | presente, com hash ou tamanho diferente |
| `APPEARED` | esperado ausente, e existe |
| `UNREADABLE` | diretório no lugar de arquivo, permissão, ou caminho que escapa da raiz (inclusive por ligação simbólica) |
| `UNDECLARED` | arquivo sob um escopo pedido que não consta na lista |

Duplicidade de conteúdo é informativa e fica à parte. Para evidência congelada, `require_intact` e `IntegrityReport.raise_if_failed` levantam `IntegrityError`: a operação não segue adiante em silêncio. Relatórios nunca carregam o caminho local nem o texto de erro do sistema operacional. `expected_from_source_records` liga isso aos registros `source` da Foundation e recusa fonte sem hash fixado, porque o que não tem hash não pode ser afirmado.

## Compatibilidade de runtime

| Item | Valor |
|---|---|
| Mínimo pretendido | Python 3.10 (o mesmo do projeto; ver README) |
| Auditoria de sintaxe | os módulos novos são analisados com `ast.parse(feature_version=(3, 9))`; é verificação de sintaxe, não execução |
| APIs | revisadas e varridas por nome contra APIs posteriores ao 3.9 (lista parcial); o código usa `from __future__ import annotations`, então a sintaxe moderna de tipos nunca é avaliada |
| Versões efetivamente testadas | somente Python 3.15.0rc2, no Windows |
| Linux e macOS | a suíte não foi executada em Linux nem em macOS; a lógica evita acoplamento ao Windows, mas isso não foi comprovado em execução |
| Outras versões do Python | não havia outra instalação local; nada foi instalado |

Pela revisão e pela varredura parcial acima, não se identificou uso de recurso exclusivo do 3.15, mas isso só se confirma executando a suíte em versões anteriores, o que não foi feito.

## Limitações conhecidas

- Escrita atômica: no Windows a troca usa `MoveFileEx` com substituição, sem garantia formal de atomicidade, e falha com `PermissionError` se outro processo mantém o destino aberto. O `fsync` do diretório só é tentado em POSIX. Permissões, ACLs e dono de um destino existente não são preservados. Ligação simbólica no destino é substituída.
- Verificar e depois ler são dois momentos: um arquivo pode mudar entre eles. Quem precisa de garantia estrita lê os bytes uma vez e calcula o hash desses mesmos bytes.
- JSON canônico: inteiros acima de 2^53 podem perder precisão em leitores de outras linguagens; `1` e `1.0` são valores diferentes; sem normalização Unicode.
- Caminho lógico: as restrições a nomes reservados do Windows valem em qualquer sistema, de propósito; o schema só expressa as regras estruturais.
- `created_at`: o validador de `date-time` herdado da Foundation usa `fromisoformat`, que antes do Python 3.11 só aceita fração de segundo com 3 ou 6 dígitos. Para ser aceito em qualquer versão, use 3 ou 6 dígitos, ou nenhuma. É comportamento documentado do Python e não foi verificado aqui.
- As barreiras de identidade (chaves como `timestamp` ou `provider`, caminhos locais) evitam descuido; não detectam um UUID escrito dentro de um valor de texto. A garantia forte é estrutural: o que não é argumento da API não entra no id.
- O teste de leitura concorrente é de melhor esforço; no Windows a troca pode colidir com a leitura e é tentada de novo.
- Fora desta extração: estado do controle de versão, validação de formato de imagem e comando de linha de comando para os novos recursos.

## Rastreabilidade

Cada capacidade extraída tem origem, classificação, destino, adaptação, acoplamento rejeitado e testes registrados em [`extraction/EXTRACTION_001.json`](extraction/EXTRACTION_001.json) (`docs/extraction/EXTRACTION_001.json`), junto com o que foi adiado (e para onde) e o que foi rejeitado (e por quê). Testes verificam que o arquivo é consistente com o código: destinos e símbolos existem, testes citados existem, todo módulo novo está mapeado e o artefato não carrega caminho local.

Relação com a auditoria: [`AGENT_FOUNDATION_AUDIT.md`](AGENT_FOUNDATION_AUDIT.md), seção 8, item 1 (C43 e C23). Arquitetura: [`ARCHITECTURE.md`](ARCHITECTURE.md).
