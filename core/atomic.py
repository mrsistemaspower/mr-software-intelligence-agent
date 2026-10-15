"""Escrita atômica de arquivos: bytes, texto e JSON canônico.

Garantias:

- O destino nunca fica parcialmente escrito. Os bytes vão para um arquivo temporário no mesmo
  diretório, são gravados em disco (`fsync`) e só então substituem o destino com `os.replace`.
- Se qualquer etapa falhar (inclusive interrupção do usuário), o temporário é removido e o conteúdo
  anterior do destino continua intacto.
- A serialização acontece antes de tocar o disco: um valor inválido não cria arquivo nem temporário.
- Escrita idempotente: se o destino já tem exatamente os mesmos bytes, nada é regravado.
- Os bytes escritos são exatamente os calculados, sem tradução de quebra de linha pelo sistema.

Limites conhecidos (ver docs/AGENT_EXTRACTION_1.md):

- `os.replace` é atômico em POSIX no mesmo sistema de arquivos. No Windows usa `MoveFileEx` com
  substituição; é o melhor que a plataforma oferece, mas a atomicidade não é formalmente garantida,
  e o destino aberto por outro processo pode fazer a troca falhar com `PermissionError`.
- A durabilidade do diretório (`fsync` do diretório) só é tentada em POSIX.
- Permissões, ACLs e dono de um destino que existia não são preservados: o novo arquivo nasce com
  as permissões padrão do processo. Ligação simbólica no destino é substituída, não seguida.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from .hashing import Digest, hash_bytes
from .paths import to_fs_path
from .serialization import canonical_bytes, canonical_pretty_bytes

CREATED = "CREATED"
UPDATED = "UPDATED"
NO_CHANGE = "NO_CHANGE"

_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
_NAME_ROOM = 60  # parte do nome do destino reaproveitada no nome do temporário


@dataclass(frozen=True)
class WriteResult:
    """Resultado de uma escrita: `CREATED`, `UPDATED` ou `NO_CHANGE`, e o hash dos bytes que ficaram no destino."""

    status: str
    digest: Digest


def _create_temp(directory: str, base: str) -> tuple[int, str]:
    """Cria, com exclusividade, um temporário no mesmo diretório do destino."""
    for _ in range(100):
        name = f".{base[:_NAME_ROOM]}.{secrets.token_hex(6)}.tmp"
        candidate = os.path.join(directory, name)
        try:
            return os.open(candidate, _OPEN_FLAGS, 0o666), candidate
        except FileExistsError:
            continue
    raise FileExistsError("não foi possível reservar um nome de arquivo temporário")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("escrita interrompida sem progresso")
        view = view[written:]


def _sync_directory(directory: str) -> None:
    """Melhor esforço: grava a entrada de diretório em disco. Só em POSIX; falha aqui não desfaz a troca."""
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _same_content(target: str, payload: bytes) -> bool:
    try:
        if os.stat(target).st_size != len(payload):
            return False
        with open(target, "rb") as handle:
            return handle.read() == payload
    except OSError:
        return False


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes, *, skip_unchanged: bool = True) -> WriteResult:
    """Grava `data` em `path` de forma atômica. Cria os diretórios que faltarem."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("atomic_write_bytes exige bytes")
    payload = bytes(data)
    digest = hash_bytes(payload)
    target = to_fs_path(path)
    directory = os.path.dirname(target)
    if os.path.isdir(target):
        raise IsADirectoryError("o destino é um diretório")
    existed = os.path.exists(target)
    if existed and skip_unchanged and _same_content(target, payload):
        return WriteResult(NO_CHANGE, digest)

    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = _create_temp(directory, os.path.basename(target))
    try:
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _sync_directory(directory)
    return WriteResult(UPDATED if existed else CREATED, digest)


def atomic_write_text(
    path: str | os.PathLike[str], text: str, *, normalize_newlines: bool = True, skip_unchanged: bool = True
) -> WriteResult:
    """Grava texto em UTF-8 (sem BOM). Por padrão converte CRLF e CR isolado em LF, para que o mesmo
    texto produza os mesmos bytes em qualquer sistema."""
    if not isinstance(text, str):
        raise TypeError("atomic_write_text exige str")
    if normalize_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("o texto contém substituto Unicode isolado e não é UTF-8 válido") from exc
    return atomic_write_bytes(path, payload, skip_unchanged=skip_unchanged)


def atomic_write_json(
    path: str | os.PathLike[str], value: object, *, pretty: bool = True, skip_unchanged: bool = True
) -> WriteResult:
    """Grava o valor em JSON canônico (`core.serialization`).

    `pretty=True` (padrão): forma legível, com quebra de linha final, boa para revisão em diff.
    `pretty=False`: exatamente a forma compacta, sem quebra de linha final; nesse caso o hash do
    arquivo é igual a `hash_canonical_json(value)`.
    """
    payload = canonical_pretty_bytes(value) if pretty else canonical_bytes(value)
    return atomic_write_bytes(path, payload, skip_unchanged=skip_unchanged)
