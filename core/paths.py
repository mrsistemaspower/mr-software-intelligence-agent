"""Portabilidade de caminhos: caminho lógico x caminho de integração local.

Dois conceitos que nunca se misturam:

- Caminho lógico: texto relativo à raiz de um conjunto de conhecimento, com `/` como único separador.
  É o único tipo de caminho que pode entrar em snapshot, identidade de artefato, proveniência ou qualquer
  contrato portátil. Não carrega unidade de disco, nem raiz, nem `..`, nem nome inválido em outro sistema.
- Caminho local: `pathlib.Path` que existe só nesta máquina. Entra em funções como argumento (a raiz
  local, vinda de configuração) e nunca sai delas dentro de um registro.

Não há normalização Unicode: o mesmo nome em NFC e em NFD são dois caminhos lógicos diferentes.
Nomes que só diferem em maiúsculas e minúsculas são tratados como colisão por quem monta conjuntos
(ver `core.snapshot`), porque não coexistem em sistemas de arquivos que ignoram caixa.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

_MAX_DEPTH = 100
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_CHARS = frozenset('<>"|?*')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{n}" for n in range(1, 10)} | {f"LPT{n}" for n in range(1, 10)}
)

# Letra de unidade, caminho UNC, URL de arquivo, raízes típicas de sistemas Unix e atalho de home.
# É um superconjunto do detector usado na validação de pacotes (ver tests/test_paths.py).
_LOCAL_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|^\\\\"
    r"|^file://"
    r"|^~[\\/]"
    r"|(?<![A-Za-z0-9.])/(?:home|Users|usr|var|mnt|opt|etc|tmp|root|srv|private|Volumes)/"
)

_EXTENDED_THRESHOLD = 240  # abaixo do MAX_PATH do Windows, com folga para o nome do arquivo


class PathPortabilityError(ValueError):
    """Um caminho (ou um valor que carrega caminho) não é portátil."""


# ------------------------------------------------------------------ caminho lógico
def normalize_logical_path(text: str) -> str:
    """Forma única de um caminho lógico, ou `PathPortabilityError`.

    Aceita `/` e `\\` como separadores de entrada e devolve só `/`. Descarta `.` e separadores repetidos
    ou finais. Rejeita: vazio, caractere de controle, raiz (`/x`, `\\\\srv`), unidade (`C:`), `:` em qualquer
    componente, `..`, componente terminado em espaço ou ponto, caracteres `<>"|?*` e nomes reservados do
    Windows (`CON`, `NUL`, `COM1`...), com ou sem extensão.
    """
    if not isinstance(text, str):
        raise PathPortabilityError("caminho lógico deve ser texto")
    if not text:
        raise PathPortabilityError("caminho lógico vazio")
    if _CONTROL.search(text):
        raise PathPortabilityError("caminho lógico com caractere de controle")
    unified = text.replace("\\", "/")
    if unified.startswith("/"):
        raise PathPortabilityError("caminho lógico não pode ser absoluto")
    parts: list[str] = []
    for part in unified.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise PathPortabilityError("caminho lógico não pode conter '..'")
        if ":" in part:
            raise PathPortabilityError("caminho lógico não pode conter ':' (unidade ou fluxo alternativo)")
        if part[-1] in " .":
            raise PathPortabilityError("componente terminado em espaço ou ponto não é portátil")
        if _RESERVED_CHARS & set(part):
            raise PathPortabilityError("componente com caractere reservado do Windows")
        if part.split(".")[0].upper() in _RESERVED_NAMES:
            raise PathPortabilityError("componente com nome reservado do Windows")
        parts.append(part)
    if not parts:
        raise PathPortabilityError("caminho lógico vazio após normalização")
    return "/".join(parts)


def is_logical_path(text: object) -> bool:
    """Verdadeiro se `text` já está na forma normalizada de um caminho lógico."""
    if not isinstance(text, str):
        return False
    try:
        return normalize_logical_path(text) == text
    except PathPortabilityError:
        return False


def to_logical_path(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    """Caminho lógico de `path` em relação à raiz local `root`.

    Nunca devolve o caminho absoluto como alternativa: se `path` está fora de `root` (inclusive por
    ligação simbólica que aponta para fora), levanta `PathPortabilityError`.
    """
    base = Path(root).resolve()
    target = Path(path).resolve()
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise PathPortabilityError("o caminho está fora da raiz informada") from None
    return normalize_logical_path(relative.as_posix())


def resolve_logical(root: str | os.PathLike[str], logical: str) -> Path:
    """Caminho local de um caminho lógico dentro de `root`, sem poder escapar dela.

    Segue ligações simbólicas: se o destino final cai fora de `root`, levanta `PathPortabilityError`.
    """
    normalized = normalize_logical_path(logical)
    base = Path(root).resolve()
    candidate = (base / normalized).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise PathPortabilityError("o caminho lógico resolve para fora da raiz") from None
    return candidate


# ------------------------------------------------------------------ detecção de caminho local
def comparison_key(logical: str) -> str:
    """Chave para comparar um caminho lógico com nomes de arquivos reais deste sistema.

    Sistemas de arquivos do Windows ignoram maiúsculas e minúsculas; os demais, em geral, não.
    """
    return logical.casefold() if os.name == "nt" else logical


def looks_like_local_path(text: str) -> bool:
    """Heurística: o texto parece um caminho absoluto desta ou de outra máquina."""
    return isinstance(text, str) and _LOCAL_PATH.search(text) is not None


def _walk_strings(node: object, where: str, depth: int) -> Iterator[tuple[str, str]]:
    if depth > _MAX_DEPTH:
        raise PathPortabilityError(f"{where}: profundidade excessiva")
    if isinstance(node, str):
        yield where, node
    elif isinstance(node, dict):
        for index, (key, item) in enumerate(node.items()):
            flagged = isinstance(key, str) and _LOCAL_PATH.search(key) is not None
            # Uma chave que parece caminho local nunca aparece no texto da posição.
            label = f"<chave[{index}]>" if flagged or not isinstance(key, str) else key
            if flagged:
                yield f"{where}.{label}", key
            yield from _walk_strings(item, f"{where}.{label}", depth + 1)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            yield from _walk_strings(item, f"{where}[{index}]", depth + 1)


def find_local_paths(value: object) -> list[str]:
    """Posições (nunca o conteúdo) de textos e chaves que parecem caminho local."""
    return [where for where, text in _walk_strings(value, "$", 0) if looks_like_local_path(text)]


def assert_portable(value: object, what: str = "valor") -> None:
    """Levanta `PathPortabilityError` se algum texto ou chave de `value` parece caminho local."""
    found = find_local_paths(value)
    if found:
        listed = ", ".join(found[:5]) + (" ..." if len(found) > 5 else "")
        raise PathPortabilityError(f"{what} carrega caminho local em: {listed}")


# ------------------------------------------------------------------ caminho de integração local
def _windows_extended(absolute: str, threshold: int = _EXTENDED_THRESHOLD) -> str:
    """Prefixo `\\\\?\\` (ou `\\\\?\\UNC\\`) para caminho absoluto do Windows com `threshold` caracteres ou mais.

    Função pura. Caminho que já tem o prefixo não muda.
    """
    if absolute.startswith("\\\\?\\") or len(absolute) < threshold:
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def to_fs_path(path: str | os.PathLike[str], *, tree: bool = False) -> str:
    """Texto de caminho absoluto pronto para chamadas de sistema de arquivos nesta máquina.

    No Windows, caminhos longos recebem o prefixo estendido (só quando necessário, e com suporte a
    UNC). Com `tree=True`, use em raízes de operações recursivas (varrer ou apagar uma árvore): os
    descendentes podem passar do limite mesmo que a raiz seja curta, então o prefixo é sempre aplicado.
    Nos demais sistemas é só o caminho absoluto. O resultado é local: não o grave em registros.
    """
    absolute = os.path.abspath(os.fspath(path))
    if os.name == "nt":
        return _windows_extended(absolute, 0 if tree else _EXTENDED_THRESHOLD)
    return absolute
