# Exemplo: instruções do projeto (ChatGPT e ambientes de agente da OpenAI)

Em modo conversa, cole nas instruções do projeto ou do assistente:

```
Siga integralmente as instruções de AGENT.md (anexado). Antes de responder sobre um software,
consulte o pacote dele (manifest.json e as coleções anexadas). Cite o status e a proveniência de
cada afirmação e diga o que não se sabe. Não use conhecimento externo ao pacote para preencher lacunas.
```

Anexe: `AGENT.md`, os arquivos de `policies/` e os arquivos do pacote consultado. Pacotes grandes devem ser anexados por coleção.

Em ambientes de agente com acesso ao repositório, use o arquivo de instruções de repositório da ferramenta, apontando para `AGENT.md`.
