# Exemplo: runtime local

Passe o conteúdo de `AGENT.md` como instrução de sistema e registre quatro funções de ferramenta:

| Ferramenta abstrata | Implementação mínima |
|---|---|
| `read-file` | lê um arquivo dentro do workspace do software ou do repositório do agente |
| `search` | busca texto em arquivos |
| `write-file` | grava apenas no diretório de execução do agente |
| `run-command` | executa só comandos de uma lista permitida (por exemplo, `py -m core validate-pack`) |

Para modelos sem visão, a leitura de capturas entra por uma etapa externa. O resultado dela é registrado como evidência com `extracted_by.actor` igual a `tool` ou `ai`, e a confiança reflete a qualidade da etapa.
