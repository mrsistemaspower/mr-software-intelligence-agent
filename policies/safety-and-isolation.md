# Segurança, operação não destrutiva e isolamento

## Operação não destrutiva

1. **Fontes são somente leitura.** Nenhum adaptador escreve, move, renomeia, formata ou apaga arquivos do workspace do software. O acesso declarado no manifesto é sempre `read-only`.
2. **O agente escreve só no que é seu.** Registros de conhecimento vão para o pacote do software, relatórios para o diretório de execução do agente. Nunca dentro do workspace do software.
3. **Execução (`runtime`) exige autorização humana explícita**, em ambiente de teste ou homologação. Nunca em produção. Nunca com ações de escrita, envio, emissão ou exclusão. Cada sessão registra o que foi acionado.
4. **Nada de efeito colateral na importação.** Módulos do núcleo não leem nem escrevem arquivos ao serem importados.
5. **Escrita atômica e idempotente** para arquivos de estado: gravar em temporário e renomear; não regravar se o conteúdo é idêntico.
6. **Sem comandos destrutivos sem pedido explícito.** Isso inclui remover diretórios, reescrever histórico de versionamento e forçar envio.

## Dados sensíveis

- `source.contains_sensitive_data` é `true`, `false` ou `null`. `null` significa "não conferido" e é tratado como se pudesse haver dado sensível.
- Evidências extraídas de fontes sensíveis guardam o mínimo necessário e nunca reproduzem dados pessoais, fiscais ou financeiros reais além do indispensável.
- Nada de segredos (chaves, senhas, tokens, cadeias de conexão) em registros, documentação ou exemplos. Referencie pelo nome da variável.
- Documentação publicada não embute capturas de tela nem nomes de clientes sem decisão explícita de uma pessoa.

## Isolamento entre núcleo e software

O Agent Core não conhece nenhum software. O conhecimento de cada software mora em um Software Knowledge Pack.

- Os termos que identificam um software (nome, telas, regras, títulos de janela) ficam em `isolation.leak_terms` do manifesto do pacote.
- Nenhum desses termos pode aparecer em `core/`, `schemas/`, `skills/`, `adapters/`, `workflows/`, `policies/` nem em `software-packs/_template`.
- Um pacote não pode conter termos de outro pacote.
- O núcleo não pode ramificar por `software_id` nem conter ids de software como constante.
- Quando um segundo software entra, seus termos passam a valer para todas as verificações, sem mudar o núcleo.

`py -m core check-isolation` e `tests/test_isolation.py` impõem essas regras.

## Neutralidade de provedor

Instruções, schemas, skills, workflows e políticas não nomeiam provedores de modelo. Nomes de provedores aparecem somente em `adapters/llm/`, em `docs/` e no `README.md`. Ver `docs/MODEL_PORTABILITY.md`.

## Caminhos e identidade

- Contratos públicos (manifestos, referências de integração, schemas) não contêm caminho absoluto. O workspace externo é resolvido por variável de ambiente ou por configuração local não versionada (`local.config.json`, ignorada pelo git).
- A identidade operacional da organização no GitHub é `mrsistemaspower`. Identidades antigas não aparecem em URLs operacionais.

## Publicação

- Antes de publicar qualquer saída gerada, confirme que não há captura de tela, dado pessoal ou segredo.
- Saídas geradas ficam fora do versionamento do agente, salvo exemplos sintéticos em `evaluations/fixtures/`.
